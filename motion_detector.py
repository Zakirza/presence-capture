"""
motion_detector.py — Video streaming and recording engine (RTSP & Webcam)
Runs in a background thread; records continuously between Start and Stop.
"""

import os
import time
import threading
import subprocess
import cv2
import numpy as np
from datetime import datetime
from PIL import Image
import imageio_ffmpeg
from database import save_clip_fifo, CLIPS_DIR


# ── Shared state (read by Streamlit app) ──────────────────────────────────────
state = {
    "running":         False,      # thread active
    "recording":       False,      # actively capturing & recording video
    "frame":           None,       # latest raw frame (numpy array BGR)
    "clip_saved":      0,          # incremented each time a clip is saved
    "error":           None,       # connection/runtime error string
    "start_time":      0.0,        # timestamp when current recording started
    "last_saved_info": None,       # dict of last completed clip (name, duration, size)
    "lock":            threading.Lock(),
}

_thread: threading.Thread | None = None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _save_thumbnail(frame, base_name: str) -> str:
    """Save a JPEG thumbnail and return its path."""
    thumb_path = os.path.join(CLIPS_DIR, base_name + "_thumb.jpg")
    try:
        small = cv2.resize(frame, (320, 240))
        cv2.imwrite(thumb_path, small)
        return thumb_path
    except Exception as e:
        print(f"Thumbnail warning: {e}")
        return ""


def _convert_to_h264(raw_path: str, final_path: str):
    """
    Convert raw OpenCV video to web-compatible H.264 (yuv420p) with faststart.
    Guarantees seamless playback in all modern browsers and Streamlit.
    """
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        return

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-y",
            "-i", raw_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            final_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(raw_path) and os.path.abspath(raw_path) != os.path.abspath(final_path):
            os.remove(raw_path)
    except Exception as e:
        print(f"H.264 conversion warning: {e}")
        # Fallback: keep raw file if conversion failed
        if os.path.exists(raw_path) and not os.path.exists(final_path):
            os.replace(raw_path, final_path)


# ── Main streaming & recording loop ───────────────────────────────────────────

def _stream_and_record_loop(camera_source: int | str):
    # Enforce TCP transport for RTSP streams to avoid packet drops / frame corruption
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    is_network_stream = isinstance(camera_source, str) and (
        camera_source.startswith("rtsp://") or 
        camera_source.startswith("http://") or 
        camera_source.startswith("https://")
    )

    if is_network_stream:
        cap = cv2.VideoCapture(camera_source)
    else:
        try:
            cam_idx = int(camera_source)
        except (ValueError, TypeError):
            cam_idx = 0
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)

    if not cap.isOpened():
        with state["lock"]:
            state["error"]     = f"Cannot open camera source: {camera_source}"
            state["running"]   = False
            state["recording"] = False
        return

    # Warm-up / sample frames to determine actual stream resolution
    # RTSP streams often need 0.5 - 2 seconds to deliver the first keyframe (I-frame)
    test_frame = None
    for _ in range(50):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            test_frame = frame
            break
        time.sleep(0.05)

    if test_frame is None:
        with state["lock"]:
            state["error"]     = f"Connected to {camera_source}, but no video frames received."
            state["running"]   = False
            state["recording"] = False
        cap.release()
        return

    height, width = test_frame.shape[:2]

    # Resolve frame rate
    raw_fps = cap.get(cv2.CAP_PROP_FPS)
    fps = raw_fps if (raw_fps and raw_fps > 0 and not np.isnan(raw_fps) and raw_fps <= 60) else 20.0

    # Prepare clip filenames
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name  = f"clip_{ts}"
    raw_path   = os.path.join(CLIPS_DIR, base_name + "_raw.mp4")
    final_path = os.path.join(CLIPS_DIR, base_name + ".mp4")

    # Generate thumbnail from first frame
    thumb_path = _save_thumbnail(test_frame, base_name)

    # Initialize video writer with EXACT frame dimensions
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out    = cv2.VideoWriter(raw_path, fourcc, fps, (width, height))

    out.write(test_frame)
    frames_written = 1

    rec_start = time.time()
    with state["lock"]:
        state["recording"]  = True
        state["start_time"] = rec_start
        state["frame"]      = test_frame.copy()

    frame_interval = 1.0 / fps

    # Active recording loop
    while True:
        with state["lock"]:
            if not state["running"]:
                break

        loop_start = time.time()
        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0:
            time.sleep(0.02)
            continue

        # Guarantee dimensions match VideoWriter to prevent OpenCV dropping frames
        if frame.shape[:2] != (height, width):
            frame = cv2.resize(frame, (width, height))

        out.write(frame)
        frames_written += 1

        with state["lock"]:
            state["frame"] = frame.copy()

        # Prevent CPU spinning
        elapsed = time.time() - loop_start
        sleep_dur = frame_interval - elapsed
        if sleep_dur > 0.002:
            time.sleep(sleep_dur)

    # ── Finalize recording ────────────────────────────────────────────────────
    out.release()
    cap.release()

    if frames_written == 0 or not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        with state["lock"]:
            state["recording"] = False
            state["error"] = "Recording ended but no frames were captured."
        return

    # Fallback thumbnail if first frame failed
    if not thumb_path:
        with state["lock"]:
            last_frame = state["frame"]
        if last_frame is not None:
            thumb_path = _save_thumbnail(last_frame, base_name)

    # Transcode to web-standard H.264
    _convert_to_h264(raw_path, final_path)

    target_file = final_path if os.path.exists(final_path) else raw_path
    duration    = max(1, int(time.time() - rec_start))
    size_kb     = int(os.path.getsize(target_file) / 1024) if os.path.exists(target_file) else 0

    # Save to SQLite database with 30-day FIFO policy
    save_clip_fifo(
        file_name      = base_name + ".mp4",
        file_path      = target_file,
        thumbnail_path = thumb_path or "",
        duration       = duration,
        size_kb        = size_kb,
    )

    with state["lock"]:
        state["recording"]  = False
        state["clip_saved"] += 1
        state["last_saved_info"] = {
            "file_name": base_name + ".mp4",
            "duration": duration,
            "size_kb": size_kb,
        }


# ── Public API ────────────────────────────────────────────────────────────────

def start(camera_source: int | str = 0, **kwargs):
    """Start streaming from camera/RTSP and begin recording."""
    global _thread
    with state["lock"]:
        if state["running"]:
            return
        state["running"]         = True
        state["error"]           = None
        state["last_saved_info"] = None

    _thread = threading.Thread(
        target=_stream_and_record_loop,
        args=(camera_source,),
        daemon=True,
    )
    _thread.start()


def stop(wait: bool = True, timeout: float = 15.0):
    """Stop recording, wait for encoding/DB save to complete, and return."""
    global _thread
    with state["lock"]:
        state["running"] = False
    if wait and _thread is not None and _thread.is_alive():
        _thread.join(timeout=timeout)


def get_frame_jpeg() -> bytes | None:
    """Return current camera frame as JPEG bytes for Streamlit preview."""
    with state["lock"]:
        frame = state["frame"]
    if frame is None:
        return None
    _, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes()
