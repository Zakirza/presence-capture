"""
app.py — Streamlit frontend for RTSP Video Streamer and Retriever
Run with:  streamlit run app.py
"""

import base64
import os
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import database as db
import motion_detector as md

live_preview = components.declare_component(
    "live_preview", path=str(Path(__file__).with_name("live_preview"))
)

def format_duration(seconds: int) -> str:
    """Format total seconds into standard HH:MM:SS."""
    secs = max(0, int(seconds or 0))
    hours, rem = divmod(secs, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="RTSP Video Streamer and Retriever",
    page_icon="📹",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Init DB ───────────────────────────────────────────────────────────────────
db.init_db()

# ── Read current background state ─────────────────────────────────────────────
with md.state["lock"]:
    is_running   = md.state["running"]
    is_recording = md.state["recording"]
    recording_started_at = md.state["start_time"]
    err          = md.state["error"]
    last_saved   = md.state["last_saved_info"]
is_finalizing = md.is_finalizing()

# ── Sidebar — Controls & Settings ─────────────────────────────────────────────
with st.sidebar:
    st.title("📹 RTSP Streamer & Retriever")
    st.markdown("---")

    source_type = st.radio("📡 Input Source", ["Local Webcam", "RTSP Stream"], horizontal=True, disabled=is_running)

    if source_type == "Local Webcam":
        camera_source = st.selectbox(
            "📷 Select Camera Index",
            [0, 1, 2],
            index=0,
            disabled=is_running,
            help="0 = built-in webcam, 1 or 2 = external USB camera"
        )
    else:
        camera_source = st.text_input(
            "🌐 RTSP URL",
            value="rtsp://admin:password@192.168.1.100:554/stream1",
            disabled=is_running,
            help="RTSP URL for your IP Camera, CCTV, or NVR"
        )

    st.markdown("---")
    st.markdown("### 🎬 Recording Controls")
    col1, col2 = st.columns(2)
    start_btn  = col1.button("▶️ Start", use_container_width=True, type="primary", disabled=is_running or is_finalizing)
    stop_btn   = col2.button("⏹️ Stop",  use_container_width=True, disabled=not is_running)

    if start_btn:
        md.start(camera_source=camera_source)
        st.rerun()

    if stop_btn:
        with st.spinner("💾 Finalizing and saving recorded clip..."):
            md.stop(wait=True)
        st.rerun()

    st.markdown("---")

    # Status indicators
    status_heading = "### 📊 Status"
    if is_recording:
        # Streamlit 1.37 shows its script activity indicator on every fragment tick.
        status_heading += '\n\n<style>[data-testid="stStatusWidget"] { visibility: hidden; }</style>'
    st.markdown(status_heading, unsafe_allow_html=True)
    if is_running and is_recording:
        st.error("🔴 **Recording...**")
    elif is_running:
        st.warning("🟡 Initializing stream...")
    elif is_finalizing:
        st.info("Finalizing recording...")
    else:
        st.info("⚪ **Idle / Ready**")

    if last_saved:
        size_mb = last_saved['size_kb'] / 1024
        st.success(f"💾 **Saved:** `{last_saved['file_name']}`\n\n⏱️ Duration: `{format_duration(last_saved['duration'])}` · 📦 {size_mb:.1f} MB")

    if err:
        st.error(f"⚠️ {err}")

    clip_count = db.get_clip_count()
    day_count  = db.get_day_count()
    st.markdown("---")
    st.metric("📅 Days Stored",  f"{day_count} / {db.MAX_DAYS}")
    st.progress(min(day_count / db.MAX_DAYS, 1.0))
    st.caption(f"📼 Total clips: {clip_count}")


# ── Main area ─────────────────────────────────────────────────────────────────
st.title("📹 RTSP Video Streamer and Retriever")

tab_live, tab_clips = st.tabs(["📡 Live Stream", "📁 Saved Clips"])

# ═══════════════════════════════════════════════════════════
# TAB 1 — Live Stream Feed & Recording Monitor
# ═══════════════════════════════════════════════════════════
@st.fragment(run_every=0.2 if is_recording else 1)
def render_live_stream():
    with md.state["lock"]:
        running    = md.state["running"]
        recording  = md.state["recording"]

    if (running, recording, md.is_finalizing()) != (is_running, is_recording, is_finalizing):
        st.rerun()

    if not running:
        st.info("👆 Select your source in the sidebar and click **▶️ Start** to begin streaming and recording.")
    elif not recording:
        st.info("Connecting to stream...")
    else:
        frame_bytes = md.get_frame_jpeg()
        if frame_bytes:
            jpeg = base64.b64encode(frame_bytes).decode("ascii")
            st.html(f'<span id="live-camera-frame" data-jpeg="{jpeg}" hidden></span>')

with tab_live:
    if is_running and is_recording:
        live_preview(elapsed_seconds=max(0, time.time() - recording_started_at), key="live-camera-preview")
    render_live_stream()


# ═══════════════════════════════════════════════════════════
# TAB 2 — Saved Clips & Retrieval Gallery
# ═══════════════════════════════════════════════════════════
with tab_clips:
    col_head, col_ref = st.columns([5, 1])
    col_head.subheader("📁 Saved Video Archive")
    with col_ref:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    clips_by_day = db.get_clips_by_day()   # dict: {date_str: [clips]}

    if not clips_by_day:
        st.info("No clips recorded yet. Click **▶️ Start** in the sidebar to record your first clip.")
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

                    cols[3].markdown(f"`{format_duration(clip['duration_seconds'])}`")

                    size_mb = clip["file_size_kb"] / 1024
                    cols[4].markdown(f"{size_mb:.1f} MB")

                    if cols[5].button("🗑️", key=f"del_{clip['clip_id']}", help="Delete this clip"):
                        try:
                            db.delete_clip(clip["clip_id"])
                        except OSError as exc:
                            st.error(f"Could not delete {clip['file_name']}. {exc}. You can retry Delete.")
                        else:
                            st.rerun()

                    # Toggle video player via session_state
                    play_key = f"play_{clip['clip_id']}"
                    if play_key not in st.session_state:
                        st.session_state[play_key] = False

                    if st.button(f"▶️ Play / Hide", key=f"btn_{clip['clip_id']}"):
                        st.session_state[play_key] = not st.session_state[play_key]

                    if st.session_state[play_key]:
                        fpath = clip["file_path"]
                        if os.path.exists(fpath):
                            st.video(fpath)
                        else:
                            st.warning(f"Video file not found on disk: {fpath}")

                    st.markdown("---")
