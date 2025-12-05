# Video Compressor Service

An automated video compression service that watches directories for new video files, compresses them using H.265/HEVC encoding, and sends real-time notifications via ntfy.

## Overview

This Python service provides automated batch video compression with intelligent queue management, duplicate detection, and resolution-aware processing. It's designed to run continuously as a background service, monitoring input directories and processing videos as they arrive.

## Key Features

### 🎬 Video Processing
- **H.265/HEVC Encoding**: High-quality compression with significantly reduced file sizes
- **Multi-Resolution Support**: Automatic detection and processing for 480p, 720p, and 1080p videos
- **Quality Profiles**: Three preset profiles (slow, medium, fast) balancing quality and speed
- **Audio Optimization**: AAC encoding at 128kbps for consistent audio quality

### 📁 Smart File Management
- **Automatic Folder Monitoring**: Watches input directories recursively for new video files
- **Duplicate Detection**: Fast hashing algorithm prevents reprocessing identical files
- **Queue Management**: JSON-based task queue with persistent state
- **Atomic Operations**: Safe file operations prevent data corruption during power failures

### 🔄 Task Rotation & Archival
- **Automatic Archival**: Archives completed tasks when queue grows large
- **Separate Error Tracking**: Failed tasks archived separately for troubleshooting
- **Configurable Thresholds**: Control when archival happens based on queue size and idle time
- **Smart Triggers**: Only archives when all active processing is complete

### 📊 Real-Time Monitoring
- **Progress Tracking**: Live percentage updates during encoding
- **ntfy Notifications**: Push notifications for queue events, completion, and errors
- **Detailed Logging**: Comprehensive logs for debugging and auditing
- **Status Tracking**: Each task tracked through its lifecycle (queued → processing → processed/failed)

### 🔒 Thread-Safe Operations
- **Concurrent Processing**: Separate threads for watching and processing
- **File Locks**: Prevents corruption when multiple processes access task data
- **Transfer Detection**: Waits for file transfers to complete before processing

## Directory Structure

```
video-compressor/
├── input/                    # Drop videos here
│   ├── 480/                 # 480p videos
│   ├── 720/                 # 720p videos
│   └── 1080/                # 1080p videos
├── output/                   # Compressed videos (mirrors input structure)
│   ├── 480/
│   ├── 720/
│   └── 1080/
├── conf/
│   ├── tasks.json           # Active task queue
│   ├── tasks.json.<date>    # Archived processed tasks
│   └── tasks-err.json.<date> # Archived failed tasks
├── logs/
│   └── app.log              # Application logs
└── compressor.py            # Main service script
```

## Installation

### Prerequisites

```bash
# Python 3.7+
python3 --version

# FFmpeg with libx265 support
ffmpeg -version | grep libx265

# Install Python dependencies
pip install watchdog requests
```

### FFmpeg Installation

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH.

## Configuration

Edit the configuration section in `compressor.py`:

```python
# Directories
INPUT_DIR = "input"
OUTPUT_DIR = "output"

# Notification endpoint
NTFY_TOPIC = "http://161.118.241.80/ntfy/video-compressor"

# Video file extensions to process
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

# Encoding profile: "slow", "medium", or "fast"
PROFILE = "medium"

# Rotation settings
ROTATION_THRESHOLD = 100      # Min entries before rotation
ROTATION_SCAN_WAIT = 5        # Scan cycles to wait before rotating
```

### Encoding Profiles

| Profile | Preset | CRF | Speed | Quality | Use Case |
|---------|--------|-----|-------|---------|----------|
| **slow** | slow | 24 | Slowest | Highest | Archival, maximum quality |
| **medium** | medium | 26 | Moderate | Good | General use, balanced |
| **fast** | fast | 28 | Fastest | Acceptable | Quick processing, previews |

**CRF (Constant Rate Factor)**: Lower = better quality, larger files. Range: 0-51.

## Usage

### Starting the Service

```bash
python3 compressor.py
```

The service will:
1. Create necessary directories (input/480, input/720, input/1080, output, conf, logs)
2. Scan for existing videos in input folders
3. Start the file watcher and processor threads
4. Begin processing queued tasks

### Adding Videos

Simply copy video files to the appropriate resolution folder:

```bash
# For 720p videos
cp my-video.mp4 input/720/

# For 1080p videos in a subfolder
cp vacation.mov input/1080/vacation/

# The service will automatically detect and queue them
```

### Monitoring Progress

**Via Logs:**
```bash
tail -f logs/app.log
```

**Via ntfy Notifications:**
- 📁 New file queued
- 🔵 Processing started
- 🟢 Processing completed
- 🔴 Processing failed
- 📦 Tasks archived
- 🔄 Rotation complete

### Task Status

Tasks progress through these states:
- `queued` - Waiting for processing
- `processing` - Currently being encoded
- `processed` - Successfully completed
- `failed` - FFmpeg encoding failed
- `error_missing_input` - Input file not found
- `error_no_resolution` - Could not determine resolution
- `error_exception` - Python exception occurred

## Task Rotation

The service automatically archives old tasks when:
1. Total task count exceeds `ROTATION_THRESHOLD` (default: 100)
2. No active tasks are queued or processing
3. System has been idle for `ROTATION_SCAN_WAIT` cycles (default: 5)

**Archive Files:**
- `conf/tasks.json.YYYYMMDD-HHMMSS` - Successfully processed tasks
- `conf/tasks-err.json.YYYYMMDD-HHMMSS` - Failed/error tasks

This keeps the active queue manageable and separates errors for analysis.

## Task File Format

`conf/tasks.json` stores task metadata:

```json
[
  {
    "path": "720/video.mp4",
    "md5": "a1b2c3d4...",
    "resolution": "720",
    "status": "processed",
    "added_time": "2024-01-15T10:30:00.000000",
    "start_time": "2024-01-15T10:30:05.000000",
    "end_time": "2024-01-15T10:45:30.000000",
    "file_size_before": 524288000,
    "file_size_after": 157286400,
    "time_taken_seconds": 925.5
  }
]
```

## Performance Optimization

### Fast Hashing
Uses partial file hashing (first 64KB + last 64KB + size) for quick duplicate detection without reading entire files.

### Resolution Detection
Automatically detects target resolution from folder structure, eliminating manual configuration.

### Atomic Writes
Task state changes are written atomically to prevent corruption during crashes or power failures.

### Transfer Detection
Waits for file copy operations to complete before processing, preventing corruption from partial files.

## Troubleshooting

### Videos Not Being Detected

**Check directory structure:**
```bash
ls -la input/480/
ls -la input/720/
ls -la input/1080/
```

Videos must be inside a resolution folder.

### Processing Stuck

**Check logs:**
```bash
tail -n 50 logs/app.log
```

**Check task status:**
```bash
cat conf/tasks.json | grep status
```

**Manually reset stuck task:**
Edit `conf/tasks.json` and change `"status": "processing"` to `"status": "queued"`.

### FFmpeg Errors

**Test FFmpeg manually:**
```bash
ffmpeg -i input/720/test.mp4 -c:v libx265 -preset medium -crf 26 -c:a aac output/test.mp4
```

**Common issues:**
- Missing libx265: Reinstall FFmpeg with x265 support
- Corrupted input file: File may be damaged or incomplete
- Insufficient disk space: Check `df -h`

### High CPU Usage

This is normal during encoding. To reduce:
1. Change profile to "fast"
2. Limit concurrent processing (code modification needed)
3. Increase CRF value (lower quality, faster encoding)

## System Requirements

- **CPU**: Multi-core recommended (encoding is CPU-intensive)
- **RAM**: 2GB minimum, 4GB+ recommended
- **Disk**: Sufficient space for input + output videos
- **OS**: Linux, macOS, or Windows with Python 3.7+

## Running as a Service

### Linux (systemd)

Create `/etc/systemd/system/video-compressor.service`:

```ini
[Unit]
Description=Video Compressor Service
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/video-compressor
ExecStart=/usr/bin/python3 /path/to/video-compressor/compressor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable video-compressor
sudo systemctl start video-compressor
sudo systemctl status video-compressor
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.user.video-compressor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.video-compressor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/path/to/compressor.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/video-compressor</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Load the service:
```bash
launchctl load ~/Library/LaunchAgents/com.user.video-compressor.plist
```

## Advanced Usage

### Custom Resolution Folders

Edit `RESOLUTION_FOLDERS` in the configuration:

```python
RESOLUTION_FOLDERS = ["480", "720", "1080", "1440", "2160"]
```

Then update the FFmpeg command in `process_video()` to handle new resolutions.

### Batch Processing Existing Files

Place all files in appropriate folders and restart the service. The initial scan will queue everything.

### Notification Customization

Replace `NTFY_TOPIC` with your own ntfy server or topic:

```python
NTFY_TOPIC = "https://ntfy.sh/your-unique-topic"
```

Or disable notifications by modifying `send_ntfy()` to return immediately.

## License

This is open source software. Modify and distribute as needed.

## Support

For issues, feature requests, or questions:
1. Check logs: `logs/app.log`
2. Review task status: `conf/tasks.json`
3. Test FFmpeg independently
4. Verify directory permissions

## Version History

- **v1.0** - Initial release with basic compression
- **v1.1** - Added multi-resolution support
- **v1.2** - Implemented task rotation and archival
- **v1.3** - Enhanced duplicate detection and transfer waiting

---

**Note**: This service is designed for personal/internal use. For production deployments, consider additional error handling, monitoring, and security measures.
