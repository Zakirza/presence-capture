"""
motion_detector.py — Video streaming and recording engine (RTSP & Webcam)
Runs in a background thread; records continuously between Start and Stop.
Guarantees 1:1 real-world recording duration and normal playback speed.
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


def _convert_to_h264(raw_path: str, final_path: str, real_duration: float, frames_written: int, writer_fps: float):
    """
    Convert raw OpenCV video to web-compatible H.264 (yuv420p) with faststart.
    Adjusts presentation timestamps (setpts) so playback duration matches real elapsed recording time.
    """
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        return

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        # Compute speed adjustment ratio:
        # container_duration = frames / writer_fps
        # If container_duration < real_duration, video would play fast-forward unless stretched.
        container_duration = max(0.1, frames_written / max(1.0, writer_fps))
        speed_ratio = real_duration / container_duration if container_duration > 0 else 1.0

        # Enforce exact real-world duration and standardize to 30fps web video
        vf_filter = f"setpts={speed_ratio:.6f}*PTS,fps=30"

        cmd = [
            ffmpeg_exe, "-y",
            "-i", raw_path,
            "-vf", vf_filter,
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
        # Fallback: if conversion failed, preserve raw file
        if os.path.exists(raw_path) and not os.path.exists(final_path):
            os.replace(raw_path, final_path)


# ── Main streaming & recording loop ───────────────────────────────────────────

def _stream_and_record_loop(camera_source: int | str):
    # Enforce TCP transport & low-latency buffering for RTSP
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|max_delay;500000"

    is_network_stream = isinstance(camera_source, str) and (
        camera_source.startswith("rtsp://") or 
        camera_source.startswith("http://") or 
        camera_source.startswith("https://")
    )

    if is_network_stream:
        cap = cv2.VideoCapture(camera_source)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        try:
            cam_idx = int(camera_source)
        except (ValueError, TypeError):
            cam_idx = 0
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        # Enable hardware MJPEG on Windows webcams for full 30 FPS USB throughput
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FPS, 30)

    if not cap.isOpened():
        with state["lock"]:
            state["error"]     = f"Cannot open camera source: {camera_source}"
            state["running"]   = False
            state["recording"] = False
        return

    # Warm-up / sample frames to determine actual stream resolution
    test_frame = None
    for _ in range(50):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            test_frame = frame
            break
        time.sleep(0.04)

    if test_frame is None:
        with state["lock"]:
            state["error"]     = f"Connected to {camera_source}, but no video frames received."
            state["running"]   = False
            state["recording"] = False
        cap.release()
        return

    height, width = test_frame.shape[:2]

    # Resolve frame rate (default 30.0 for smooth capture)
    raw_fps = cap.get(cv2.CAP_PROP_FPS)
    fps = raw_fps if (raw_fps and raw_fps > 0 and not np.isnan(raw_fps) and raw_fps <= 60) else 30.0

    # Prepare clip filenames
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name  = f"clip_{ts}"
    # Use MJPG in AVI for fast, low-CPU writing without encoding delay
    raw_path   = os.path.join(CLIPS_DIR, base_name + "_raw.avi")
    final_path = os.path.join(CLIPS_DIR, base_name + ".mp4")

    # Generate thumbnail from first frame
    thumb_path = _save_thumbnail(test_frame, base_name)

    # Initialize video writer with fast MJPG codec
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    out    = cv2.VideoWriter(raw_path, fourcc, fps, (width, height))

    out.write(test_frame)
    frames_written = 1

    rec_start = time.time()
    with state["lock"]:
        state["recording"]  = True
        state["start_time"] = rec_start
        state["frame"]      = test_frame.copy()

    # Active recording loop
    while True:
        with state["lock"]:
            if not state["running"]:
                break

        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0:
            time.sleep(0.01)
            continue

        # Guarantee dimensions match VideoWriter
        if frame.shape[:2] != (height, width):
            frame = cv2.resize(frame, (width, height))

        out.write(frame)
        frames_written += 1

        with state["lock"]:
            state["frame"] = frame.copy()

    # ── Finalize recording ────────────────────────────────────────────────────
    rec_end = time.time()
    real_duration = max(1.0, rec_end - rec_start)

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

    # Transcode to web-standard H.264 with exact real-world duration synchronization
    _convert_to_h264(raw_path, final_path, real_duration, frames_written, fps)

    target_file = final_path if os.path.exists(final_path) else raw_path
    duration_sec = max(1, int(real_duration))
    size_kb      = int(os.path.getsize(target_file) / 1024) if os.path.exists(target_file) else 0

    # Save to SQLite database with 30-day FIFO policy
    save_clip_fifo(
        file_name      = base_name + ".mp4",
        file_path      = target_file,
        thumbnail_path = thumb_path or "",
        duration       = duration_sec,
        size_kb        = size_kb,
    )

    with state["lock"]:
        state["recording"]  = False
        state["clip_saved"] += 1
        state["last_saved_info"] = {
            "file_name": base_name + ".mp4",
            "duration": duration_sec,
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


def stop(wait: bool = True, timeout: float = 20.0):
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
