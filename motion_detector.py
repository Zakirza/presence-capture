"""
motion_detector.py — OpenCV motion detection + video recording
Runs in a background thread; communicates via a shared state dict.
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
    "running":        False,
    "motion":         False,
    "recording":      False,
    "frame":          None,       # latest raw frame (numpy array BGR)
    "clip_saved":     0,          # incremented each time a clip is saved
    "error":          None,
    "lock":           threading.Lock(),
}

_thread: threading.Thread | None = None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _save_thumbnail(frame, base_name: str) -> str:
    """Save a JPEG thumbnail and return its path."""
    thumb_path = os.path.join(CLIPS_DIR, base_name + "_thumb.jpg")
    small = cv2.resize(frame, (320, 240))
    cv2.imwrite(thumb_path, small)
    return thumb_path


def _convert_to_h264(raw_path: str, final_path: str):
    """
    Convert raw OpenCV video to web-compatible H.264 (yuv420p) with faststart.
    Guarantees seamless playback in all modern browsers and Streamlit.
    """
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


def _record_clip(cap, base_name: str, fps: float, width: int, height: int,
                 motion_timeout: float = 3.0, min_area: int = 1500) -> tuple[str, int, int]:
    """
    Record until motion stops for `motion_timeout` seconds.
    Encodes to web-standard H.264 MP4.
    Returns (file_path, duration_seconds, size_kb).
    """
    raw_path   = os.path.join(CLIPS_DIR, base_name + "_raw.mp4")
    final_path = os.path.join(CLIPS_DIR, base_name + ".mp4")

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out    = cv2.VideoWriter(raw_path, fourcc, fps, (width, height))

    start       = time.time()
    last_motion = time.time()

    bg_sub = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=50)

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        out.write(frame)

        # Keep live frame feed updated even while recording
        with state["lock"]:
            state["frame"]     = frame.copy()
            state["recording"] = True

        fg = bg_sub.apply(frame)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if any(cv2.contourArea(c) > min_area for c in contours):
            last_motion = time.time()

        if time.time() - last_motion > motion_timeout:
            break

    out.release()

    with state["lock"]:
        state["recording"] = False

    # Convert to web-standard H.264
    _convert_to_h264(raw_path, final_path)

    target_file = final_path if os.path.exists(final_path) else raw_path
    duration    = max(1, int(time.time() - start))
    size_kb     = int(os.path.getsize(target_file) / 1024) if os.path.exists(target_file) else 0

    return target_file, duration, size_kb


# ── Main detector loop ────────────────────────────────────────────────────────

def _detector_loop(camera_source: int | str, min_area: int, sensitivity: int):
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
            state["error"]   = f"Cannot open camera source: {camera_source}"
            state["running"] = False
        return

    # Warm-up / read sample frame to guarantee valid frame dimensions
    ok, test_frame = cap.read()
    if ok and test_frame is not None:
        height, width = test_frame.shape[:2]
    else:
        width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480

    raw_fps = cap.get(cv2.CAP_PROP_FPS)
    fps = raw_fps if (raw_fps and raw_fps > 0 and not np.isnan(raw_fps)) else 20.0

    var_threshold = max(10, 110 - sensitivity * 10)   # sensitivity 1–10
    bg_sub = cv2.createBackgroundSubtractorMOG2(
        history=500, varThreshold=var_threshold, detectShadows=False
    )

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    while True:
        with state["lock"]:
            if not state["running"]:
                break

        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue

        # Update live frame for Streamlit preview
        with state["lock"]:
            state["frame"] = frame.copy()

        # Motion detection
        fg = bg_sub.apply(frame)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        motion_detected = any(cv2.contourArea(c) > min_area for c in contours)

        with state["lock"]:
            state["motion"] = motion_detected

        if motion_detected and not state["recording"]:
            # Base name from timestamp
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = f"clip_{ts}"

            # Thumbnail from first frame
            thumb_path = _save_thumbnail(frame, base_name)

            # Record clip (blocking — in this same thread)
            clip_path, duration, size_kb = _record_clip(
                cap, base_name, fps, width, height,
                motion_timeout=3.0, min_area=min_area
            )

            # Save to DB (FIFO)
            save_clip_fifo(
                file_name      = base_name + ".mp4",
                file_path      = clip_path,
                thumbnail_path = thumb_path,
                duration       = duration,
                size_kb        = size_kb,
            )

            with state["lock"]:
                state["clip_saved"] += 1

        time.sleep(0.03)

    cap.release()
    with state["lock"]:
        state["running"] = False


# ── Public API ────────────────────────────────────────────────────────────────

def start(camera_source: int | str = 0, min_area: int = 1500, sensitivity: int = 5):
    global _thread
    with state["lock"]:
        if state["running"]:
            return
        state["running"] = True
        state["error"]   = None

    _thread = threading.Thread(
        target=_detector_loop,
        args=(camera_source, min_area, sensitivity),
        daemon=True,
    )
    _thread.start()


def stop():
    with state["lock"]:
        state["running"] = False


def get_frame_jpeg() -> bytes | None:
    """Return current camera frame as JPEG bytes for Streamlit."""
    with state["lock"]:
        frame = state["frame"]
    if frame is None:
        return None
    _, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes()
