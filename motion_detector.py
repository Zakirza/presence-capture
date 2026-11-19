"""
motion_detector.py — Video streaming and recording engine (RTSP & Webcam)
Runs in a background thread; records continuously between Start and Stop.
Adjusts playback duration to match the captured footage.
"""

import os
import sys
import time
import threading
import subprocess
import cv2
from datetime import datetime
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
FRAME_TIMEOUT = 3.0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _save_thumbnail(frame, base_name: str) -> str:
    """Save a JPEG thumbnail and return its path."""
    thumb_path = os.path.join(CLIPS_DIR, base_name + "_thumb.jpg")
    try:
        small = cv2.resize(frame, (320, 240))
        return thumb_path if cv2.imwrite(thumb_path, small) else ""
    except Exception as e:
        print(f"Thumbnail warning: {e}")
        return ""


def _convert_to_h264(raw_path: str, final_path: str, real_duration: float, frames_written: int, writer_fps: float):
    """
    Convert raw OpenCV video to web-compatible H.264 (yuv420p) with faststart.
    Adjusts presentation timestamps (setpts) so playback duration matches real elapsed recording time.
    """
    pending_path = final_path.removesuffix(".mp4") + "_encoding.mp4"
    container_duration = frames_written / writer_fps
    speed_ratio = real_duration / container_duration
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y",
        "-i", raw_path, "-vf", f"setpts={speed_ratio:.6f}*PTS,fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", pending_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        if os.path.getsize(pending_path) == 0:
            raise RuntimeError("FFmpeg produced an empty video.")
        os.replace(pending_path, final_path)
    finally:
        _remove_intermediate(pending_path)


def _remove_intermediate(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"Could not remove intermediate file {path}: {exc}")


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
        cap = cv2.VideoCapture(camera_source, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(FRAME_TIMEOUT * 1000),
        ])
    else:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(camera_source), backend)

    out = None
    try:
        if not cap.isOpened():
            raise RuntimeError("Cannot open the selected camera.")
        if not is_network_stream:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FPS, 30)

        deadline = time.monotonic() + FRAME_TIMEOUT
        while time.monotonic() < deadline:
            with state["lock"]:
                if not state["running"]:
                    return
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                break
            time.sleep(0.04)
        else:
            raise RuntimeError("The camera opened, but no video frames arrived.")

        with state["lock"]:
            if not state["running"]:
                return

        height, width = frame.shape[:2]
        raw_fps = cap.get(cv2.CAP_PROP_FPS)
        fps = raw_fps if 0 < raw_fps <= 60 else 30.0

        recorded_at = datetime.now()
        base_name = recorded_at.strftime("clip_%Y%m%d_%H%M%S_%f")
        raw_path = os.path.join(CLIPS_DIR, base_name + "_raw.avi")
        final_name = base_name + ".mp4"
        final_path = os.path.join(CLIPS_DIR, final_name)
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        out = cv2.VideoWriter(raw_path, fourcc, fps, (width, height))
        if not out.isOpened():
            raise RuntimeError("Could not open the recording file for writing.")

        out.write(frame)
        frames_written = 1
        started = last_frame_at = time.monotonic()
        thumb_path = _save_thumbnail(frame, base_name)
        with state["lock"]:
            state["recording"] = True
            state["start_time"] = recorded_at.timestamp()
            state["frame"] = frame

        while True:
            with state["lock"]:
                if not state["running"]:
                    break

            try:
                ok, frame = cap.read()
            except cv2.error:
                with state["lock"]:
                    if state["running"]:
                        state["error"] = "Camera read failed. Recording ended early."
                break  # Finalize the frames already written.
            received_at = time.monotonic()
            with state["lock"]:
                if not state["running"]:
                    break
            if not ok or frame is None or not frame.size:
                if received_at - last_frame_at >= FRAME_TIMEOUT:
                    with state["lock"]:
                        state["error"] = "Camera feed stopped responding. Recording ended early."
                    break
                time.sleep(0.01)
                continue

            if frame.shape[:2] != (height, width):
                frame = cv2.resize(frame, (width, height))

            out.write(frame)
            frames_written += 1
            last_frame_at = received_at
            with state["lock"]:
                state["frame"] = frame
    finally:
        with state["lock"]:
            state["running"] = False
            state["recording"] = False
            state["frame"] = None
        if out is not None:
            out.release()
        cap.release()

    if not os.path.isfile(raw_path) or not os.path.getsize(raw_path):
        raise RuntimeError("Recording ended without a video file.")

    real_duration = max(1.0, last_frame_at - started + 1 / fps)
    try:
        _convert_to_h264(raw_path, final_path, real_duration, frames_written, fps)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.decode(errors="replace").strip())
        raise RuntimeError(f"Could not convert the recording. Raw footage was kept at {raw_path}.") from exc

    duration_sec = int(real_duration)
    size_kb = os.path.getsize(final_path) // 1024
    try:
        save_clip_fifo(final_name, final_path, thumb_path, duration_sec, size_kb, recorded_at)
    except Exception as exc:
        raise RuntimeError(f"Could not add the recording to Saved Clips. The MP4 was kept at {final_path}.") from exc
    _remove_intermediate(raw_path)

    with state["lock"]:
        state["clip_saved"] += 1
        state["last_saved_info"] = {
            "file_name": final_name,
            "duration": duration_sec,
            "size_kb": size_kb,
        }


def _record(camera_source: int | str):
    try:
        _stream_and_record_loop(camera_source)
    except Exception as exc:
        print(f"Recording failed: {exc!r}")
        if exc.__cause__:
            print(f"Cause: {exc.__cause__!r}")
        with state["lock"]:
            state["running"] = False
            state["recording"] = False
            state["frame"] = None
            state["error"] = str(exc) if isinstance(exc, RuntimeError) else "Recording failed. See the server log."


# ── Public API ────────────────────────────────────────────────────────────────

def is_finalizing() -> bool:
    with state["lock"]:
        return not state["running"] and _thread is not None and _thread.is_alive()


def start(camera_source: int | str = 0, **kwargs):
    """Start streaming from camera/RTSP and begin recording."""
    global _thread
    with state["lock"]:
        if state["running"] or (_thread is not None and _thread.is_alive()):
            return
        state["running"]         = True
        state["error"]           = None
        state["last_saved_info"] = None

        _thread = threading.Thread(
            target=_record,
            args=(camera_source,),
            daemon=True,
        )
        _thread.start()


def stop(wait: bool = True, timeout: float = 20.0):
    """Stop recording, wait for encoding/DB save to complete, and return."""
    with state["lock"]:
        state["running"] = False
        worker = _thread
    if wait and worker is not None:
        worker.join(timeout=timeout)


def get_frame_jpeg() -> bytes | None:
    """Return current camera frame as JPEG bytes for Streamlit preview."""
    with state["lock"]:
        frame = state["frame"]
    if frame is None:
        return None
    ok, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes() if ok else None
