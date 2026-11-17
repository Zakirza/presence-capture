# 📹 RTSP Video Streamer and Retriever (RVSR)

An intelligent, real-time RTSP/CCTV video surveillance and clip retrieval application built with **Python**, **OpenCV**, **Streamlit**, and **SQLite**. 

The system continuously ingests live RTSP camera feeds (or local webcams), detects movement in real time using background subtraction, records video clips automatically, and manages stored footage using an automated **30-day FIFO (First-In, First-Out) calendar-day retention and retrieval pipeline**.

---

## 📌 Table of Contents
- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Database & 30-Day FIFO Strategy](#-database--30-day-fifo-strategy)
- [Video Recording & Web Playback Pipeline](#-video-recording--web-playback-pipeline)
- [Project Structure](#-project-structure)
- [Prerequisites & Installation](#-prerequisites--installation)
- [How to Run](#-how-to-run)
- [User Interface Guide](#-user-interface-guide)
- [Configuration & Tuning](#-configuration--tuning)
- [Troubleshooting & FAQ](#-troubleshooting--faq)

---

## 🚀 Key Features

* **Multi-Source Video Ingestion**: Supports both network **RTSP Streams** (IP cameras, CCTV, NVR feeds via TCP transport) and **Local Webcams** (integrated or USB cameras).
* **On-Demand Start/Stop Recording**: Manual video recording control via Streamlit buttons. Hit **▶️ Start** to start streaming and recording, and **⏹️ Stop** to finalize, transcode, and store the clip.
* **Live Stream Monitoring**: Real-time camera feed preview directly inside the Streamlit dashboard with active recording timer and badges.
* **30-Day FIFO Day-Based Retention**: Stores clips grouped by calendar day. When clips arrive on Day 31, all video files and database records from the oldest day are purged automatically.
* **Native Browser Video Playback**: Automatically encodes captured clips to **H.264 MP4 (`yuv420p`, `+faststart`)** via bundled FFmpeg, guaranteeing smooth playback across Chrome, Edge, Safari, Firefox, and mobile devices.
* **Streamlit Web Dashboard & Retrieval Gallery**:
  * **Live Stream Tab**: Real-time preview with live recording indicator and elapsed duration.
  * **Saved Clips Tab**: Grouped day-by-day with video thumbnails, recording timestamp, duration, file size, inline video player, and manual delete controls.
* **Multithreaded Engine**: Streaming capture and FFmpeg video transcoding run in a background thread to ensure the web dashboard remains fluid and responsive.

---

## 🏛️ System Architecture

```
                      +-------------------+
                      |   RTSP / Webcam   |
                      +---------+---------+
                                |
                                v
               +---------------------------------+
               |   Background Streaming Thread   |
               |   (OpenCV TCP RTSP / DirectShow)|
               +----------------+----------------+
                                |
          +---------------------+---------------------+
          |                                           |
          v                                           v
+-------------------+                       +-------------------+
| Live Frame Buffer |                       |  Active Recording |
| (Streamlit Feed)  |                       |  (OpenCV Writer)  |
+-------------------+                       +---------+---------+
                                                      |
                                                      v  [User Hits Stop]
                                            +-------------------+
                                            |  FFmpeg H.264     |
                                            |  Web Conversion   |
                                            +---------+---------+
                                                      |
                                                      v
                                            +-------------------+
                                            |  SQLite Database  |
                                            |  (FIFO Evaluation)|
                                            +---------+---------+
                                                      |
                                      +---------------+---------------+
                                      |                               |
                                      v                               v
                            [Under 30 Days]                   [New Day >= 31]
                             Save new clip                     Delete oldest day's
                                                               files & DB rows
```

---

## 🗄️ Database & 30-Day FIFO Strategy

The system uses **SQLite** (`burglar.db`) for lightweight, serverless metadata storage.

### 1. Database Schema

#### `motion_events`
| Column | Type | Description |
|---|---|---|
| `event_id` | `INTEGER PRIMARY KEY` | Auto-incrementing unique event identifier |
| `detected_at` | `DATETIME` | Timestamp when motion was first flagged |
| `ended_at` | `DATETIME` | Timestamp when motion event concluded |
| `status` | `TEXT` | Event status (`saved`, `processing`) |

#### `video_clips`
| Column | Type | Description |
|---|---|---|
| `clip_id` | `INTEGER PRIMARY KEY` | Auto-incrementing unique clip identifier |
| `event_id` | `INTEGER` | Foreign key referencing `motion_events(event_id)` |
| `file_name` | `TEXT` | Filename (e.g., `clip_20261116_100000.mp4`) |
| `file_path` | `TEXT` | Absolute or relative path on disk |
| `thumbnail_path` | `TEXT` | Path to preview JPEG image |
| `duration_seconds`| `INTEGER` | Length of recorded clip in seconds |
| `file_size_kb` | `INTEGER` | Size of video file on disk in kilobytes |
| `recorded_at` | `DATETIME` | Full timestamp of recording |
| `clip_date` | `DATE` | Calendar date (`YYYY-MM-DD`) used for FIFO retention |

---

### 2. How the 30-Day FIFO Retention Works

Unlike simple count-based queues that cap at an arbitrary number of clips (which can lose footage quickly on active days), this system manages retention by **distinct calendar days**:

1. Up to **30 distinct days** of security clips are retained.
2. If multiple clips occur on the same day, **all** clips for that day are preserved.
3. When motion occurs on a **brand-new 31st calendar day**:
   * The database identifies the **oldest registered date** (`MIN(clip_date)`).
   * All associated video files (`.mp4`) and thumbnails (`.jpg`) are physically deleted from the `clips/` directory.
   * The corresponding rows in `video_clips` and `motion_events` are deleted.
   * The new clip is then safely committed.

---

## 🎬 Video Recording & Web Playback Pipeline

Standard OpenCV video output (`mp4v` or `XVID`) cannot be decoded by modern web browsers in HTML5 `<video>` tags. This project implements a dedicated encoding pipeline:

1. **Capture**: OpenCV records raw video frames into an intermediate file.
2. **Buffer Cooldown**: A 3.0-second inactivity timer ensures the subject has left the frame before the recording closes.
3. **Thumbnail Generation**: A 320x240 preview frame is saved at the onset of detection.
4. **H.264 Transcoding**: Uses `imageio-ffmpeg` to transcode the video using:
   * **Codec**: `libx264` (H.264 Baseline/Main profile)
   * **Pixel Format**: `yuv420p` (mandatory for HTML5 browser compatibility)
   * **Container Flags**: `+faststart` (relocates the `moov` atom to the front for instant streaming without full download)
   * **Preset**: `ultrafast` (minimal CPU overhead during background conversion)

---

## 📂 Project Structure

```
burgler presence/
│
├── app.py                 # Streamlit frontend dashboard (Live feed & Clips gallery)
├── motion_detector.py     # Background thread for OpenCV motion tracking & recording
├── database.py            # SQLite schema initialization, queries, and FIFO day-pruning
├── requirements.txt       # Python package dependencies
├── run.bat                # Windows quick-launcher script
│
├── burglar.db             # SQLite database file (created automatically on startup)
└── clips/                 # Video storage directory (created automatically)
    ├── clip_20261116_090000.mp4
    ├── clip_20261116_090000_thumb.jpg
    └── ...
```

---

## ⚙️ Prerequisites & Installation

### Requirements
* **Operating System**: Windows 10/11, Linux, or macOS
* **Python**: Python 3.10 to 3.12 (Anaconda or standard Python)
* **Camera**: Integrated webcam, external USB camera, or RTSP IP Camera / CCTV / NVR stream

### Setup Instructions

1. **Clone or open the project folder**:
   ```bash
   cd "burgler presence"
   ```

2. **Install required dependencies**:
   Using Anaconda Python:
   ```bash
   C:\Users\<username>\anaconda3\python.exe -m pip install -r requirements.txt
   ```
   Or using standard Python virtual environment:
   ```bash
   python -m pip install -r requirements.txt
   ```

---

## 🚦 How to Run

### Option 1: Double-Click Launcher (Windows)
Double-click [`run.bat`](file:///c:/Users/mohdz/OneDrive/Documents/burgler%20presence/run.bat). It automatically initializes Streamlit with the configured Anaconda environment.

### Option 2: Terminal / Command Prompt
```bash
cd "c:\Users\mohdz\OneDrive\Documents\burgler presence"
C:\Users\mohdz\anaconda3\python.exe -m streamlit run app.py
```

Once started, open your web browser at:
* **Local URL**: `http://localhost:8501`
* **Network URL**: `http://<your-lan-ip>:8501` (accessible from your smartphone on the same Wi-Fi)

---

## 🖥️ User Interface Guide

### Sidebar Controls
* **📡 Input Source**:
  * **Local Webcam**: Select input device index (`0` for built-in webcam, `1` or `2` for external USB cameras).
  * **RTSP Stream**: Enter an IP camera / CCTV / NVR stream URL (e.g. `rtsp://admin:password@192.168.1.100:554/stream1`).
* **▶️ Start / ⏹️ Stop**:
  * **▶️ Start**: Begins streaming and immediately starts recording the live video.
  * **⏹️ Stop**: Halts capture, automatically transcodes the recorded clip to H.264 MP4, and stores it in the database.
* **📊 Status Card**: Displays real-time status (`Recording... (MM:SS)` or `Idle / Ready`), plus notification of the last saved clip.
* **Storage Metric**: Live progress bar showing active days stored out of `MAX_DAYS (30)`.

### Main Panel Tabs
1. **📡 Live Stream**: Shows the real-time camera view with recording indicator and live elapsed timer.
2. **📁 Saved Clips**:
   * Organized by date in collapsible day cards (latest day expanded by default).
   * Displays thumbnail, timestamp, duration, and file size.
   * **▶️ Play / Hide**: Plays video smoothly within the browser with full playback and seek controls.
   * **🗑️ Delete**: Allows manual removal of individual clips.

---

## 🔧 Configuration & Tuning

Key parameters can be adjusted in the source files:

| Setting | File | Variable | Default | Description |
|---|---|---|---|---|
| Retention Limit | `database.py` | `MAX_DAYS` | `30` | Maximum calendar days retained before purging oldest day |
| Buffer Timeout | `motion_detector.py` | `motion_timeout` | `3.0` | Seconds to record after movement stops |
| Sensitivity | `app.py` / `motion_detector.py` | `sensitivity` | `5` | Background subtractor threshold level |
| Min Contour Size | `app.py` / `motion_detector.py` | `min_area` | `1500` | Minimum movement contour area in pixels |

---

## ❓ Troubleshooting & FAQ

#### 1. Why won't clips play in my browser?
* Modern browsers require the H.264 video codec. This project uses `imageio-ffmpeg` to automatically encode all clips to H.264 with `yuv420p` pixel format.
* If an older clip fails to play, clicking **▶️ Play / Hide** triggers an automatic background repair that converts the clip on the fly.

#### 2. "Cannot open camera index 0"
* Ensure no other application (Zoom, Teams, Skype, Windows Camera app) is using the webcam.
* If using an external USB camera, change the camera dropdown in the sidebar from `0` to `1`.

#### 3. Where are the recorded files stored?
* All video recordings (`.mp4`) and previews (`_thumb.jpg`) are stored in the local [`clips/`](file:///c:/Users/mohdz/OneDrive/Documents/burgler%20presence/clips) folder.

#### 4. Can I access the feed from my mobile phone?
* Yes! Ensure your phone is connected to the same Wi-Fi network as the host computer. Open your phone's browser and navigate to the **Network URL** displayed in the terminal (e.g., `http://192.168.1.X:8501`).

---

## 📄 License
This project is for personal security and educational surveillance use. Feel free to customize and extend it for your specific monitoring requirements.
