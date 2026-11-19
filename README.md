# RTSP Video Streamer and Retriever (RVSR)

A Python camera recording and footage retrieval application built with Streamlit, OpenCV, SQLite, and FFmpeg. Connect a webcam or network camera, monitor its live feed, record with Start and Stop, and browse saved clips by recording date.

## Features

- Webcam capture from device indexes 0, 1, or 2.
- RTSP capture over TCP, plus HTTP and HTTPS camera streams supported by OpenCV.
- Continuous recording between Start and Stop.
- Live preview with a recording banner and elapsed timer.
- H.264 MP4 export with thumbnails and playback through the dashboard.
- A date-grouped archive showing recording time, duration, and file size.
- Automatic retention of up to 30 distinct recording dates.
- Manual clip deletion with a retry message when a file is locked.
- Recovery of captured footage when the camera read fails during recording.

## Install

Use Python 3.10 or newer with a webcam or an accessible network camera.

Clone the project branch and install its dependencies from PowerShell.

```powershell
git clone --branch RTSP_STREAM --single-branch https://github.com/Dishant-01/Fire-Detection-System.git
Set-Location Fire-Detection-System
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The dependencies include Streamlit, OpenCV, NumPy, Pillow, and imageio-ffmpeg. The imageio-ffmpeg package provides the FFmpeg executable used for video conversion.

## Run

### Using Python

From the project folder, run the following command in PowerShell.

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

With dependencies installed in an active Python environment, you can also use the following command.

```powershell
python -m streamlit run app.py
```

Open the Local URL printed in the terminal, usually [http://localhost:8501](http://localhost:8501). Keep the terminal open while using the dashboard.

### Using run.bat on Windows

The current [run.bat](run.bat) contains machine-specific paths. Before using it on a different machine or in a new clone, edit its project directory and Python executable to match your installation.

For a virtual environment inside the project folder, the launcher can use the following contents.

```bat
@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m streamlit run app.py
pause
```

After configuring it, double-click run.bat or execute the following command from PowerShell.

```powershell
.\run.bat
```

## Record and play footage

1. Select Local Webcam and choose the camera index, or select RTSP Stream and enter your camera URL.
2. Click Start. Recording begins once the first camera frame arrives.
3. Open Live Stream to watch the feed and recording timer.
4. Click Stop to end capture. Wait for conversion and saving to finish.
5. Open Saved Clips, expand a recording date, and click Play / Hide to view a clip.
6. Use Delete to remove a clip. If a file is locked, close applications using it and retry.

A typical RTSP URL has the form `rtsp://username:password@camera-address:554/stream1`. The RTSP Stream input also accepts HTTP or HTTPS video stream URLs that OpenCV can decode.

The capture thread and controls are shared across browser sessions connected to the same running server. A Stop action from another session ends the active recording.

## Live preview

[app.py](app.py) loads [live_preview/index.html](live_preview/index.html) as a Streamlit component. Launching the dashboard starts everything needed for the preview.

The preview receives JPEG frames from the capture thread roughly every 0.2 seconds while recording. A persistent canvas holds the displayed image while the next frame decodes. The timer runs in the browser, and decoding is serialized so a slow browser can continue displaying completed frames.

Recording runs in a background thread independently of browser preview updates. The preview refresh rate is separate from the recorded frame rate.

## Recording pipeline

1. OpenCV opens the selected camera and waits for the first frame.
2. Frames are written to an intermediate MJPEG AVI file, and a 320 by 240 JPEG thumbnail is created.
3. Stop, a feed timeout, or an OpenCV camera read error ends capture.
4. FFmpeg converts captured footage to H.264 MP4 using `libx264`, `yuv420p`, the `ultrafast` preset, and `+faststart`.
5. Playback timing is adjusted to match the elapsed captured footage.
6. SQLite stores the clip metadata, and the intermediate AVI is removed after a successful save.

If conversion fails, the raw AVI is kept and the app reports its path. If database insertion fails after conversion, the MP4 is kept and the app reports its path.

## Storage and retention

The app creates [clips/](clips/) and `burglar.db` automatically beside the Python files. Clip metadata includes absolute file paths, the recording start timestamp, duration, file size, thumbnail path, and recording date.

The recording start time uses the host computer's local clock. A recording crossing midnight belongs to its start date.

SQLite contains three tables.

- `motion_events` stores the recording start timestamp and saved status. Its name is retained from the earlier schema.
- `video_clips` stores file metadata and references the corresponding event.
- `pending_file_deletions` tracks files awaiting retention cleanup.

The default limit is 30 distinct dates with stored recordings. Every clip from a retained date can be kept. A date with no recordings consumes no slot.

When saving would exceed the date limit, the app retires the oldest other dates until the archive fits the limit. The new clip, retired metadata, and pending file deletions are committed together. File cleanup follows the commit.

Locked files remain queued for another cleanup attempt on subsequent saves or database initialization. Pending files can continue occupying disk space until deletion succeeds.

Manual deletion keeps the database entry until file removal succeeds, allowing a failed operation to be retried.

Recordings, thumbnails, databases, and virtual environments are excluded from Git by [.gitignore](.gitignore).

## Project files

```text
app.py                    Streamlit dashboard and camera controls
motion_detector.py        Capture thread, recording, thumbnails, and conversion
database.py               SQLite queries, migration, and retention cleanup
live_preview/index.html   Browser canvas, frame decoding, and elapsed timer
requirements.txt          Python dependencies
run.bat                   Windows launcher
clips/                    Generated videos and thumbnails
burglar.db                Generated SQLite database
```

## Configuration

- Change `MAX_DAYS` in [database.py](database.py) to adjust the distinct recording date limit. Use a positive integer.
- Change `FRAME_TIMEOUT` in [motion_detector.py](motion_detector.py) to adjust the network read timeout and the wait for frames. The default is 3 seconds.
- Change `run_every` in the live Streamlit fragment in [app.py](app.py) to adjust preview refresh frequency. The recording default is 0.2 seconds.
- Webcam capture uses DirectShow on Windows and OpenCV's automatic backend selection on other operating systems.

## Troubleshooting

### Camera cannot open

Close other applications using the webcam, check operating system camera permissions, or try another device index. For network cameras, verify the URL, credentials, reachability, and stream support.

### Camera feed stops responding

The app ends capture and attempts to save the frames already recorded. Check the camera connection and inspect the error displayed in the sidebar.

### Saving takes time after Stop

FFmpeg converts the complete recording before it appears in Saved Clips. Large recordings can take longer to process. Wait for the app to return to Idle / Ready.

### Video fails to play

Check that the saved MP4 exists and that conversion completed successfully. Inspect the terminal for FFmpeg errors. Older files may need manual conversion to browser-compatible H.264 MP4.

### Delete fails

Close players or other applications holding the file open, then retry Delete. Automatic retention cleanup retries queued files on later saves or initialization.

### Opening from another device

Use the Network URL printed by Streamlit when it is available. The client must be able to reach the host and port through the local network and firewall.

### Launching from a new clone opens the old folder

Update the paths in run.bat or use the project-relative launcher shown above. Its current hard-coded paths can point to the original development folder.
