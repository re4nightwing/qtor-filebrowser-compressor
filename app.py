import os
import time
import json
import subprocess
import threading
import requests
import hashlib
import logging
import re
import select
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ===================================================
# CONFIGURATION
# ===================================================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
TASKS_FILE = "conf/tasks.json"
NTFY_BASE_URL = os.getenv("NTFY_BASE_URL", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_URL = f"{NTFY_BASE_URL}/{NTFY_TOPIC}" if NTFY_BASE_URL and NTFY_TOPIC else None

VIDEO_EXT = set(ext for ext in os.getenv("VIDEO_EXT", "").split(",") if ext)
IMAGE_EXT = set(ext for ext in os.getenv("IMAGE_EXT", "").split(",") if ext)
PROFILE = os.getenv("PROFILE", "medium")
try:
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 4))
except ValueError:
    CHECK_INTERVAL = 4

print(
    f"Starting with: {NTFY_URL} -- {VIDEO_EXT} -- {IMAGE_EXT} -- {PROFILE} -- {CHECK_INTERVAL}"
)

# Resolution folders
RESOLUTION_FOLDERS = ["480", "720", "1080"]

ROTATION_THRESHOLD = 100
ROTATION_SCAN_WAIT = 5
rotation_scan_counter = 0

# Lock to prevent threads from corrupting the JSON file
data_lock = threading.RLock()

X265_PROFILES = {
    "slow": {
        "preset": "slow",
        "params": "aq-mode=3:bframes=8:ref=6:psy-rd=2:psy-rdoq=1.5:rd=4:no-sao=0",
        "crf": "24",
    },
    "medium": {
        "preset": "medium",
        "params": "aq-mode=3:bframes=6:ref=4:psy-rd=1.5:rd=3",
        "crf": "26",
    },
    "fast": {"preset": "fast", "params": "aq-mode=2:bframes=4:ref=3", "crf": "28"},
}

# Image compression quality settings
IMAGE_QUALITY = {"slow": 92, "medium": 85, "fast": 80}

# ===================================================
# LOGGING SETUP
# ===================================================
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("logs/app.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

if PROFILE not in X265_PROFILES:
    PROFILE = "medium"
    logger.warning("Invalid PROFILE in env, defaulting to 'medium'")


# ===================================================
# UTILITIES
# ===================================================
def now():
    return datetime.utcnow().isoformat()


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def send_ntfy(msg):
    if not NTFY_URL:
        return
    try:
        requests.post(
            f"{NTFY_URL}",
            data=msg.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=5,
        )
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


def load_tasks():
    with data_lock:
        if not os.path.exists(TASKS_FILE):
            return []
        try:
            with open(TASKS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.error("JSON file corrupted or unreadable. Returning empty list.")
            return []


def save_tasks(tasks):
    with data_lock:
        os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
        temp_file = TASKS_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(tasks, f, indent=4)
            os.replace(temp_file, TASKS_FILE)
        except OSError as e:
            logger.error(f"Failed to save tasks: {e}")


def fast_hash(path):
    try:
        h = hashlib.md5()
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            chunk = f.read(65536)
            h.update(chunk)
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                chunk = f.read(65536)
                h.update(chunk)
        h.update(str(size).encode())
        return h.hexdigest()
    except Exception:
        return ""


def detect_resolution_from_path(rel_path):
    parts = rel_path.split(os.sep)
    if len(parts) > 0 and parts[0] in RESOLUTION_FOLDERS:
        return parts[0]
    return None


def get_file_type(rel_path):
    """Determine if file is video or image"""
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    elif ext in IMAGE_EXT:
        return "image"
    return None


# ===================================================
# TASK ROTATION
# ===================================================
def should_rotate_tasks(tasks):
    global rotation_scan_counter

    if len(tasks) < ROTATION_THRESHOLD:
        rotation_scan_counter = 0
        return False

    active_statuses = ["queued", "processing", "waiting_for_resolution"]
    has_active = any(t.get("status") in active_statuses for t in tasks)

    if has_active:
        rotation_scan_counter = 0
        return False

    rotation_scan_counter += 1

    if rotation_scan_counter >= ROTATION_SCAN_WAIT:
        rotation_scan_counter = 0
        return True

    return False


def rotate_tasks(tasks):
    with data_lock:
        if not tasks:
            logger.info("[ROTATION] No tasks to rotate")
            return

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        error_statuses = [
            "error_missing_input",
            "error_exception",
            "error_no_resolution",
            "failed",
        ]

        error_tasks = [t for t in tasks if t.get("status") in error_statuses]
        processed_tasks = [t for t in tasks if t.get("status") == "processed"]

        if error_tasks:
            error_file = f"conf/tasks-err.json.{timestamp}"
            error_temp = error_file + ".tmp"
            try:
                with open(error_temp, "w") as f:
                    json.dump(error_tasks, f, indent=4)
                os.replace(error_temp, error_file)
                logger.info(
                    f"[ROTATION] Archived {len(error_tasks)} error tasks to {error_file}"
                )
                send_ntfy(f"📦 Archived {len(error_tasks)} error tasks")
            except OSError as e:
                logger.error(f"[ROTATION] Failed to save error archive: {e}")
                if os.path.exists(error_temp):
                    os.remove(error_temp)

        if processed_tasks:
            processed_file = f"conf/tasks.json.{timestamp}"
            processed_temp = processed_file + ".tmp"
            try:
                with open(processed_temp, "w") as f:
                    json.dump(processed_tasks, f, indent=4)
                os.replace(processed_temp, processed_file)
                logger.info(
                    f"[ROTATION] Archived {len(processed_tasks)} processed tasks to {processed_file}"
                )
                send_ntfy(f"📦 Archived {len(processed_tasks)} processed tasks")
            except OSError as e:
                logger.error(f"[ROTATION] Failed to save processed archive: {e}")
                if os.path.exists(processed_temp):
                    os.remove(processed_temp)

        tasks_clear = []
        os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
        temp_file = TASKS_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(tasks_clear, f, indent=4)
            os.replace(temp_file, TASKS_FILE)
            logger.info(f"[ROTATION] Cleared tasks.json - Total archived: {len(tasks)}")
            send_ntfy(f"🔄 Task rotation complete: {len(tasks)} total tasks archived")
        except OSError as e:
            logger.error(f"Failed to reset tasks file: {e}")
            if os.path.exists(temp_file):
                os.remove(temp_file)
            send_ntfy(f"🔄 Task rotation Failed: {len(tasks)} total tasks.")


def check_and_rotate():
    tasks = load_tasks()
    if should_rotate_tasks(tasks):
        logger.info(
            f"[ROTATION] Conditions met: {len(tasks)} entries, no active tasks, {ROTATION_SCAN_WAIT} scans completed"
        )
        rotate_tasks(tasks)


# ===================================================
# WATCHER LOGIC
# ===================================================
def wait_for_file_transfer(filepath):
    last_size = -1
    stable_count = 0

    while stable_count < 3:
        try:
            current_size = os.path.getsize(filepath)
        except FileNotFoundError:
            return False

        if current_size == last_size and current_size > 0:
            stable_count += 1
        else:
            last_size = current_size
            stable_count = 0

        if stable_count < 3:
            time.sleep(1)

    return True


def add_task(rel_path):
    abs_path = os.path.join(INPUT_DIR, rel_path)

    if not wait_for_file_transfer(abs_path):
        logger.warning(f"File vanished or empty: {rel_path}")
        return

    resolution = detect_resolution_from_path(rel_path)

    if resolution is None:
        logger.warning(
            f"[SKIP] File not in resolution folder (480/720/1080): {rel_path}"
        )
        send_ntfy(f"⚠️ File skipped (not in resolution folder): {rel_path}")
        return

    file_type = get_file_type(rel_path)
    if file_type is None:
        logger.warning(f"[SKIP] Unsupported file type: {rel_path}")
        return

    with data_lock:
        tasks = load_tasks()
        f_hash = fast_hash(abs_path)
        size_before = file_size(abs_path)

        if any(t.get("md5") == f_hash for t in tasks):
            logger.info(f"[SKIP] Duplicate file content detected: {rel_path}")
            return

        new_task = {
            "path": rel_path,
            "md5": f_hash,
            "type": file_type,
            "resolution": resolution,
            "status": "queued",
            "added_time": now(),
            "start_time": "",
            "end_time": "",
            "file_size_before": size_before,
            "file_size_after": 0,
            "time_taken_seconds": 0,
        }

        tasks.append(new_task)
        save_tasks(tasks)

    logger.info(
        f"[TASK ADDED] {rel_path} (Type: {file_type}, Resolution: {resolution}p)"
    )
    send_ntfy(f"📁 New {file_type.title()} Queued: {rel_path} ({resolution}p)")


class Handler(FileSystemEventHandler):
    def process(self, src_path):
        if os.path.basename(src_path).startswith("."):
            return

        if os.path.isdir(src_path):
            for root, _, files in os.walk(src_path):
                for f in files:
                    if f.startswith("."):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext in VIDEO_EXT or ext in IMAGE_EXT:
                        rel = os.path.relpath(os.path.join(root, f), INPUT_DIR)
                        add_task(rel)
        else:
            ext = os.path.splitext(src_path)[1].lower()
            if ext in VIDEO_EXT or ext in IMAGE_EXT:
                rel = os.path.relpath(src_path, INPUT_DIR)
                add_task(rel)

    def on_created(self, event):
        self.process(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.process(event.dest_path)


def initial_scan():
    logger.info("[INIT] Scanning existing files...")
    h = Handler()
    h.process(INPUT_DIR)


def start_watcher():
    observer = Observer()
    observer.schedule(Handler(), INPUT_DIR, recursive=True)
    observer.start()
    logger.info("[WATCHER] Service started")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ===================================================
# IMAGE PROCESSOR
# ===================================================
def process_image(task):
    in_path = os.path.join(INPUT_DIR, task["path"])
    out_path = os.path.join(OUTPUT_DIR, task["path"])

    if not os.path.exists(in_path):
        logger.error(f"Input file missing: {in_path}")
        task["status"] = "error_missing_input"
        send_ntfy(f"🔴 Image missing: {task['path']}")
        return

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    resolution = int(task.get("resolution") or 1080)
    quality = IMAGE_QUALITY.get(PROFILE, 85)

    send_ntfy(f"🖼️ Processing Image: {task['path']} ({resolution}p)")
    logger.info(f"[STARTED] Image: {task['path']} (Resolution: {resolution}p)")

    start_ts = time.time()

    try:
        with Image.open(in_path) as img:
            # Convert RGBA to RGB if needed
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Get current dimensions
            width, height = img.size

            # Only resize if height exceeds target resolution
            if height > resolution:
                # Calculate new dimensions maintaining aspect ratio
                aspect_ratio = width / height
                new_height = resolution
                new_width = int(resolution * aspect_ratio)

                # Ensure width is even (required for some video encoders)
                if new_width % 2 != 0:
                    new_width -= 1

                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                logger.info(
                    f"[IMAGE] Resized from {width}x{height} to {new_width}x{new_height}"
                )
            else:
                logger.info(
                    f"[IMAGE] No resize needed ({width}x{height} already <= {resolution}p)"
                )

            # Determine output format and extension from original path
            orig_ext = os.path.splitext(task["path"])[1].lower()
            out_ext = orig_ext if orig_ext in (".png", ".jpg", ".jpeg") else ".jpg"
            out_path_final = os.path.splitext(out_path)[0] + out_ext

            if orig_ext == ".png":
                # PNG: optimize with compression
                img.save(out_path_final, "PNG", optimize=True, compress_level=9)
            elif orig_ext in (".jpg", ".jpeg"):
                # JPG/JPEG: use quality setting, preserve extension
                img.save(out_path_final, "JPEG", quality=quality, optimize=True)
            else:
                # Unsupported extension: save as JPG
                img.save(out_path_final, "JPEG", quality=quality, optimize=True)

        end_ts = time.time()

        task["status"] = "processed"
        task["end_time"] = now()
        task["file_size_after"] = file_size(out_path)
        task["time_taken_seconds"] = round(end_ts - start_ts, 2)

        size_reduction = (
            (task["file_size_before"] - task["file_size_after"])
            / task["file_size_before"]
            * 100
        )

        # Remove input file after successful processing
        try:
            if os.path.exists(in_path):
                os.remove(in_path)
                logger.info(f"[CLEANUP] Removed input file: {in_path}")
        except OSError as e:
            logger.error(f"[CLEANUP] Failed to remove input file {in_path}: {e}")

        send_ntfy(
            f"🟢 Image Done: {task['path']} ({resolution}p, {size_reduction:.1f}% smaller)"
        )
        logger.info(
            f"[FINISHED] Image: {task['path']} - {size_reduction:.1f}% size reduction"
        )

    except Exception as e:
        logger.error(f"Exception during image processing: {e}")
        task["status"] = "error_exception"
        send_ntfy(f"🔴 Image Failed: {task['path']}")


# ===================================================
# VIDEO PROCESSOR
# ===================================================
def process_video(task):
    x = X265_PROFILES[PROFILE]
    in_path = os.path.join(INPUT_DIR, task["path"])
    out_path = os.path.join(OUTPUT_DIR, os.path.splitext(task["path"])[0] + ".mp4")

    if not os.path.exists(in_path):
        logger.error(f"Input file missing: {in_path}")
        task["status"] = "error_missing_input"
        send_ntfy(f"🔴 Video missing: {task['path']}")
        return

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    resolution = task.get("resolution") or "1080"

    send_ntfy(f"🔵 Processing Video: {task['path']} ({resolution}p)")
    logger.info(f"[STARTED] Video: {task['path']} (Resolution: {resolution}p)")

    start_ts = time.time()

    duration = 0
    try:
        probe = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                in_path,
            ]
        )
        duration = float(probe.strip())
    except Exception as e:
        logger.warning(f"Could not probe duration: {e}")

    cmd = [
        "ffmpeg",
        "-i",
        in_path,
        "-vf",
        f"scale=-2:{resolution}",
        "-c:v",
        "libx265",
        "-preset",
        x["preset"],
        "-x265-params",
        x["params"],
        "-crf",
        x["crf"],
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        out_path,
        "-y",
    ]

    try:
        process = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True
        )

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break

            if duration > 0:
                match = re.search(r"time=(\d+:\d+:\d+\.\d+)", line)
                if match:
                    t = match.group(1)
                    try:
                        h, m, s = t.split(":")
                        elapsed = int(h) * 3600 + int(m) * 60 + float(s)
                        pct = min(100, (elapsed / duration) * 100)
                        print(f"\r[FFMPEG] {pct:5.1f}% - {task['path']}", end="")
                    except (ValueError, AttributeError):
                        pass
            else:
                # No duration known; use select timeout to avoid blocking
                ready, _, _ = select.select([process.stderr], [], [], 1.0)
                if not ready:
                    if process.poll() is not None:
                        break

        print()

        return_code = process.poll()
        end_ts = time.time()

        if return_code == 0:
            task["status"] = "processed"
            task["end_time"] = now()
            task["file_size_after"] = file_size(out_path)
            task["time_taken_seconds"] = round(end_ts - start_ts, 2)

            # Remove input file after successful processing
            try:
                if os.path.exists(in_path):
                    os.remove(in_path)
                    logger.info(f"[CLEANUP] Removed input file: {in_path}")
            except OSError as e:
                logger.error(f"[CLEANUP] Failed to remove input file {in_path}: {e}")

            send_ntfy(f"🟢 Video Done: {task['path']} ({resolution}p)")
            logger.info(f"[FINISHED] Video: {task['path']}")
        else:
            task["status"] = "failed"
            logger.error(
                f"[FAILED] FFmpeg return code {return_code} for {task['path']}"
            )
            send_ntfy(f"🔴 Video Failed: {task['path']}")

    except Exception as e:
        logger.error(f"Exception during video processing: {e}")
        task["status"] = "error_exception"
        send_ntfy(f"🔴 Video Failed: {task['path']}")


# ===================================================
# UNIFIED PROCESSOR LOGIC
# ===================================================
def process_media(task):
    """Route to appropriate processor based on file type"""
    file_type = task.get("type", "video")

    if file_type == "image":
        process_image(task)
    else:
        process_video(task)


def processor_loop():
    logger.info("[PROCESSOR] Service started")
    scan_counter = 0
    while True:
        try:
            with data_lock:
                tasks = load_tasks()
                task_to_run = None

                for task in tasks:
                    if task["status"] == "queued":
                        if not task.get("resolution"):
                            resolution = detect_resolution_from_path(task["path"])
                            if resolution:
                                task["resolution"] = resolution
                            else:
                                logger.error(
                                    f"Cannot determine resolution for {task['path']}"
                                )
                                task["status"] = "error_no_resolution"
                                send_ntfy(f"⚠️ Resolution error: {task['path']}")
                                save_tasks(tasks)
                                continue

                        task_to_run = task
                        break

                if task_to_run:
                    task_to_run["status"] = "processing"
                    task_to_run["start_time"] = now()
                    save_tasks(tasks)

            if task_to_run:
                process_media(task_to_run)

                with data_lock:
                    current_tasks = load_tasks()
                    for t in current_tasks:
                        if (
                            t["path"] == task_to_run["path"]
                            and t["md5"] == task_to_run["md5"]
                        ):
                            t.update(task_to_run)
                            break
                    save_tasks(current_tasks)
            else:
                scan_counter += 1
                if scan_counter % CHECK_INTERVAL == 0:
                    check_and_rotate()
                time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"Processor Loop Error: {e}")
            time.sleep(CHECK_INTERVAL)


def start_processor():
    processor_loop()


# ===================================================
# MAIN
# ===================================================
if __name__ == "__main__":
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs("conf", exist_ok=True)

    for res in RESOLUTION_FOLDERS:
        os.makedirs(os.path.join(INPUT_DIR, res), exist_ok=True)
        logger.info(f"[INIT] Created/verified folder: {INPUT_DIR}/{res}")

    initial_scan()

    t1 = threading.Thread(target=start_watcher, daemon=True)
    t2 = threading.Thread(target=start_processor, daemon=True)

    t1.start()
    t2.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping services...")
