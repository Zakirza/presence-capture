"""
app.py — Streamlit frontend for the Burglar Presence Detection System
Run with:  streamlit run app.py
"""

import time
import os
import streamlit as st
from PIL import Image
import io

import database as db
import motion_detector as md

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Burglar Presence Detection",
    page_icon="🔐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Init DB ───────────────────────────────────────────────────────────────────
db.init_db()

# ── Sidebar — Settings ────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🔐 Burglar Detector")
    st.markdown("---")

    source_type = st.radio("📡 Input Source", ["Local Webcam", "RTSP Stream"], horizontal=True)

    if source_type == "Local Webcam":
        camera_source = st.selectbox(
            "📷 Select Camera Index",
            [0, 1, 2],
            index=0,
            help="0 = built-in webcam, 1 or 2 = external USB camera"
        )
    else:
        camera_source = st.text_input(
            "🌐 RTSP URL",
            value="rtsp://admin:password@192.168.1.100:554/stream1",
            help="RTSP URL for your IP Camera, CCTV, or NVR"
        )

    sensitivity = st.slider("🎚️ Sensitivity", 1, 10, 5,
                            help="Higher = detects smaller movements")
    min_area    = st.slider("📐 Min Motion Area (px²)", 500, 5000, 1500, step=100,
                            help="Minimum contour area to count as motion")

    st.markdown("---")
    col1, col2 = st.columns(2)
    start_btn  = col1.button("▶️ Start", use_container_width=True, type="primary")
    stop_btn   = col2.button("⏹️ Stop",  use_container_width=True)

    if start_btn:
        md.start(camera_source=camera_source, min_area=min_area, sensitivity=sensitivity)
        st.success("Detector started!")

    if stop_btn:
        md.stop()
        st.warning("Detector stopped.")

    st.markdown("---")

    # Status indicators
    with md.state["lock"]:
        is_running   = md.state["running"]
        is_motion    = md.state["motion"]
        is_recording = md.state["recording"]
        err          = md.state["error"]

    st.markdown("### Status")
    st.markdown(f"{'🟢 Running' if is_running else '🔴 Stopped'}")
    if is_motion:
        st.warning("🚨 Motion Detected!")
    if is_recording:
        st.error("🔴 Recording...")
    if err:
        st.error(f"Error: {err}")

    clip_count = db.get_clip_count()
    day_count  = db.get_day_count()
    st.markdown("---")
    st.metric("📅 Days Stored",  f"{day_count} / {db.MAX_DAYS}")
    st.progress(day_count / db.MAX_DAYS)
    st.caption(f"📼 Total clips: {clip_count}")


# ── Main area ─────────────────────────────────────────────────────────────────
st.title("🔐 Burglar Presence Detection System")

tab_live, tab_clips = st.tabs(["📡 Live Camera", "📁 Saved Clips"])

# ═══════════════════════════════════════════════════════════
# TAB 1 — Live Camera Feed
# ═══════════════════════════════════════════════════════════
with tab_live:

    with md.state["lock"]:
        running = md.state["running"]

    if not running:
        st.info("👆 Press **▶️ Start** in the sidebar to begin monitoring.")
    else:
        feed_placeholder  = st.empty()
        badge_placeholder = st.empty()

        # Motion / recording badge
        with md.state["lock"]:
            motion    = md.state["motion"]
            recording = md.state["recording"]

        if recording:
            badge_placeholder.error("🔴 RECORDING CLIP...")
        elif motion:
            badge_placeholder.warning("🚨 MOTION DETECTED")
        else:
            badge_placeholder.success("✅ All clear — monitoring")

        # Live frame
        frame_bytes = md.get_frame_jpeg()
        if frame_bytes:
            feed_placeholder.image(frame_bytes, channels="BGR",
                                   caption="Live Feed", use_column_width=True)
        else:
            feed_placeholder.info("Waiting for camera frame...")

        # Auto-refresh every second
        time.sleep(1)
        st.rerun()


# ═══════════════════════════════════════════════════════════
# TAB 2 — Saved Clips
# ═══════════════════════════════════════════════════════════
with tab_clips:

    clips_by_day = db.get_clips_by_day()   # dict: {date_str: [clips]}

    if not clips_by_day:
        st.info("No clips saved yet. Motion needs to be detected first.")
    else:
        total_clips = db.get_clip_count()
        day_count   = db.get_day_count()
        st.markdown(f"### 📅 {day_count} day(s) stored &nbsp; · &nbsp; 📼 {total_clips} total clip(s)  *(newest day first)*")

        for day, clips in clips_by_day.items():
            # Day header
            with st.expander(f"📅  {day}  —  {len(clips)} clip(s)", expanded=(list(clips_by_day.keys()).index(day) == 0)):

                hcols = st.columns([1, 2, 2, 1, 1, 1])
                hcols[0].markdown("**#**")
                hcols[1].markdown("**Thumbnail**")
                hcols[2].markdown("**Time**")
                hcols[3].markdown("**Duration**")
                hcols[4].markdown("**Size**")
                hcols[5].markdown("**Delete**")
                st.markdown("---")

                for i, clip in enumerate(clips, 1):
                    cols = st.columns([1, 2, 2, 1, 1, 1])

                    cols[0].markdown(f"**{i}**")

                    thumb = clip.get("thumbnail_path")
                    if thumb and os.path.exists(thumb):
                        cols[1].image(thumb, width=120)
                    else:
                        cols[1].markdown("*(no thumb)*")

                    # Show only time (HH:MM:SS) since day is the header
                    time_str = clip["recorded_at"].split(" ")[-1][:8] if clip["recorded_at"] else "—"
                    cols[2].markdown(time_str)

                    cols[3].markdown(f"{clip['duration_seconds']}s")

                    size_mb = clip["file_size_kb"] / 1024
                    cols[4].markdown(f"{size_mb:.1f} MB")

                    if cols[5].button("🗑️", key=f"del_{clip['clip_id']}", help="Delete this clip"):
                        db.delete_clip(clip["clip_id"])
                        st.success(f"Deleted: {clip['file_name']}")
                        st.rerun()

                    # Toggle video player via session_state (no nested expander)
                    play_key = f"play_{clip['clip_id']}"
                    if play_key not in st.session_state:
                        st.session_state[play_key] = False

                    if st.button(f"▶️ Play / Hide", key=f"btn_{clip['clip_id']}"):
                        st.session_state[play_key] = not st.session_state[play_key]

                    if st.session_state[play_key]:
                        if os.path.exists(clip["file_path"]):
                            # Ensure file is H.264 web-compatible
                            file_to_play = clip["file_path"]
                            try:
                                import imageio_ffmpeg, subprocess
                                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                                converted_path = os.path.splitext(clip["file_path"])[0] + "_h264.mp4"
                                if not os.path.exists(converted_path):
                                    cmd = [
                                        ffmpeg_exe, "-y",
                                        "-i", clip["file_path"],
                                        "-c:v", "libx264",
                                        "-preset", "ultrafast",
                                        "-pix_fmt", "yuv420p",
                                        "-movflags", "+faststart",
                                        converted_path
                                    ]
                                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                if os.path.exists(converted_path):
                                    file_to_play = converted_path
                            except Exception:
                                pass

                            with open(file_to_play, "rb") as f:
                                video_bytes = f.read()
                            st.video(video_bytes, format="video/mp4")
                        else:
                            st.warning("Video file not found on disk.")

                    st.markdown("---")
