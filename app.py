import os
import time
import json
import subprocess
import threading
import requests
import hashlib
import logging
import re
import shutil
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv

load_dotenv()

# ===================================================
# CONFIGURATION
# ===================================================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
TASKS_FILE = "conf/tasks.json"
NTFY_BASE_URL = os.getenv("NTFY_BASE_URL")
NTFY_TOPIC = f"{NTFY_BASE_URL}/video-compressor"

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
PROFILE = "medium"
CHECK_INTERVAL = 4

# Resolution folders
RESOLUTION_FOLDERS = ["480", "720", "1080"]

ROTATION_THRESHOLD = 100  # Minimum entries before rotation is considered
ROTATION_SCAN_WAIT = 5     # Number of scan cycles to wait before rotating
rotation_scan_counter = 0  # Track scan cycles

# Lock to prevent threads from corrupting the JSON file
data_lock = threading.Lock()

X265_PROFILES = {
    "slow": {
        "preset": "slow",
        "params": "aq-mode=3:bframes=8:ref=6:psy-rd=2:psy-rdoq=1.5:rd=4:no-sao=0",
        "crf": "24"
    },
    "medium": {
        "preset": "medium",
        "params": "aq-mode=3:bframes=6:ref=4:psy-rd=1.5:rd=3",
        "crf": "26"
    },
    "fast": {
        "preset": "fast",
        "params": "aq-mode=2:bframes=4:ref=3",
        "crf": "28"
    }
}

# ===================================================
# LOGGING SETUP
# ===================================================
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
    try:
        requests.post(
            f"{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=5
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
            # Atomic move prevents corruption if power fails during write
            os.replace(temp_file, TASKS_FILE)
        except OSError as e:
            logger.error(f"Failed to save tasks: {e}")

def fast_hash(path):
    """ Hashes first 64KB, last 64KB and file size for speed """
    try:
        h = hashlib.md5()
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            # Read first 64KB
            chunk = f.read(65536)
            h.update(chunk)
            # Jump to end - 64KB
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                chunk = f.read(65536)
                h.update(chunk)
        h.update(str(size).encode())
        return h.hexdigest()
    except Exception:
        return ""

def detect_resolution_from_path(rel_path):
    """
    Detects resolution from folder structure.
    Expected structure: input/480/video.mp4 or input/720/subfolder/video.mp4
    Returns resolution string (480, 720, 1080) or None if not in a resolution folder
    """
    parts = rel_path.split(os.sep)
    
    # Check if the first folder is a resolution folder
    if len(parts) > 0 and parts[0] in RESOLUTION_FOLDERS:
        return parts[0]
    
    return None

# ===================================================
# TASK ROTATION
# ===================================================
def should_rotate_tasks(tasks):
    """
    Determines if tasks should be rotated based on:
    1. Total entries above threshold
    2. All active tasks are completed (no queued/processing)
    3. Sufficient scan cycles have passed
    """
    global rotation_scan_counter
    
    # Check if we have enough entries
    if len(tasks) < ROTATION_THRESHOLD:
        rotation_scan_counter = 0
        return False
    
    # Check if there are any active tasks
    active_statuses = ["queued", "processing", "waiting_for_resolution"]
    has_active = any(t.get("status") in active_statuses for t in tasks)
    
    if has_active:
        rotation_scan_counter = 0
        return False
    
    # Increment counter and check if we've waited enough
    rotation_scan_counter += 1
    
    if rotation_scan_counter >= ROTATION_SCAN_WAIT:
        rotation_scan_counter = 0
        return True
    
    return False

def rotate_tasks():
    """
    Archives old tasks when rotation conditions are met:
    - Error tasks go to conf/tasks-err.json.<date>
    - Processed tasks go to conf/tasks.json.<date>
    - Clears the main tasks.json file
    """
    with data_lock:
        tasks = load_tasks()
        
        if not tasks:
            logger.info("[ROTATION] No tasks to rotate")
            return
        
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        error_statuses = ["error_missing_input", "error_exception", "error_no_resolution", "failed"]
        
        # Separate tasks by status
        error_tasks = [t for t in tasks if t.get("status") in error_statuses]
        processed_tasks = [t for t in tasks if t.get("status") == "processed"]
        
        # Archive error tasks
        if error_tasks:
            error_file = f"conf/tasks-err.json.{timestamp}"
            try:
                with open(error_file, "w") as f:
                    json.dump(error_tasks, f, indent=4)
                logger.info(f"[ROTATION] Archived {len(error_tasks)} error tasks to {error_file}")
                send_ntfy(f"📦 Archived {len(error_tasks)} error tasks")
            except OSError as e:
                logger.error(f"[ROTATION] Failed to save error archive: {e}")
        
        # Archive processed tasks
        if processed_tasks:
            processed_file = f"conf/tasks.json.{timestamp}"
            try:
                with open(processed_file, "w") as f:
                    json.dump(processed_tasks, f, indent=4)
                logger.info(f"[ROTATION] Archived {len(processed_tasks)} processed tasks to {processed_file}")
                send_ntfy(f"📦 Archived {len(processed_tasks)} processed tasks")
            except OSError as e:
                logger.error(f"[ROTATION] Failed to save processed archive: {e}")
        
        # Clear main tasks file
        save_tasks([])
        logger.info(f"[ROTATION] Cleared tasks.json - Total archived: {len(tasks)}")
        send_ntfy(f"🔄 Task rotation complete: {len(tasks)} total tasks archived")

def check_and_rotate():
    """
    Checks if rotation should happen and performs it if conditions are met
    Called periodically by the processor loop
    """
    tasks = load_tasks()
    
    if should_rotate_tasks(tasks):
        logger.info(f"[ROTATION] Conditions met: {len(tasks)} entries, no active tasks, {ROTATION_SCAN_WAIT} scans completed")
        rotate_tasks()

# ===================================================
# WATCHER LOGIC
# ===================================================
def wait_for_file_transfer(filepath):
    """ Waits until file size stops changing (transfer complete) """
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
    
    # 1. Wait for copy to finish
    if not wait_for_file_transfer(abs_path):
        logger.warning(f"File vanished or empty: {rel_path}")
        return

    # 2. Detect resolution from folder path
    resolution = detect_resolution_from_path(rel_path)
    
    if resolution is None:
        logger.warning(f"[SKIP] File not in resolution folder (480/720/1080): {rel_path}")
        send_ntfy(f"⚠️ File skipped (not in resolution folder): {rel_path}")
        return

    # 3. Check if already exists in memory or disk
    tasks = load_tasks()
    
    # Calculate hash safely now that file is stable
    f_hash = fast_hash(abs_path)
    size_before = file_size(abs_path)

    # Skip if we have seen this specific file content before
    if any(t.get("md5") == f_hash for t in tasks):
        logger.info(f"[SKIP] Duplicate file content detected: {rel_path}")
        return

    # Add new task
    new_task = {
        "path": rel_path,
        "md5": f_hash,
        "resolution": resolution,
        "status": "queued",
        "added_time": now(),
        "start_time": "",
        "end_time": "",
        "file_size_before": size_before,
        "file_size_after": 0,
        "time_taken_seconds": 0
    }

    tasks.append(new_task)
    save_tasks(tasks)
    logger.info(f"[TASK ADDED] {rel_path} (Resolution: {resolution}p)")
    send_ntfy(f"📁 New File Queued: {rel_path} ({resolution}p)")

class Handler(FileSystemEventHandler):
    def process(self, src_path):
        # Ignore temp files or hidden files
        if os.path.basename(src_path).startswith("."):
            return

        if os.path.isdir(src_path):
            for root, _, files in os.walk(src_path):
                for f in files:
                    if f.startswith("."): continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext in VIDEO_EXT:
                        rel = os.path.relpath(os.path.join(root, f), INPUT_DIR)
                        add_task(rel)
        else:
            ext = os.path.splitext(src_path)[1].lower()
            if ext in VIDEO_EXT:
                rel = os.path.relpath(src_path, INPUT_DIR)
                add_task(rel)

    def on_created(self, event):
        self.process(event.src_path)
    
    def on_moved(self, event):
        # Handle files moved into the folder
        if not event.is_directory:
            self.process(event.dest_path)

def initial_scan():
    logger.info("[INIT] Scanning existing files...")
    # Manual trigger of process logic for existing files
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
# PROCESSOR LOGIC
# ===================================================
def process_video(task):
    x = X265_PROFILES[PROFILE]
    in_path = os.path.join(INPUT_DIR, task["path"])
    out_path = os.path.join(OUTPUT_DIR, task["path"])
    
    if not os.path.exists(in_path):
        logger.error(f"Input file missing: {in_path}")
        task["status"] = "error_missing_input"
        return

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    resolution = task.get("resolution", "1080")

    send_ntfy(f"🔵 Processing Started: {task['path']} ({resolution}p)")
    logger.info(f"[STARTED] {task['path']} (Resolution: {resolution}p)")

    start_ts = time.time()

    # Get duration for progress calculation
    duration = 0
    try:
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", in_path]
        )
        duration = float(probe.strip())
    except Exception as e:
        logger.warning(f"Could not probe duration: {e}")

    cmd = [
        "ffmpeg", "-i", in_path,
        "-vf", f"scale=-2:{resolution}",
        "-c:v", "libx265",
        "-preset", x["preset"],
        "-x265-params", x["params"],
        "-crf", x["crf"],
        "-c:a", "aac",
        "-b:a", "128k",
        out_path,
        "-y"
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True
        )

        # Read progress
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
                        elapsed = int(h)*3600 + int(m)*60 + float(s)
                        pct = min(100, (elapsed / duration) * 100)
                        # Print carriage return only to update line in place
                        print(f"\r[FFMPEG] {pct:5.1f}% - {task['path']}", end="")
                    except:
                        pass
        
        print() # Newline after progress bar
        
        return_code = process.poll()
        end_ts = time.time()

        if return_code == 0:
            task["status"] = "processed"
            task["end_time"] = now()
            task["file_size_after"] = file_size(out_path)
            task["time_taken_seconds"] = round(end_ts - start_ts, 2)
            send_ntfy(f"🟢 Finished: {task['path']} ({resolution}p)")
            logger.info(f"[FINISHED] {task['path']}")
        else:
            task["status"] = "failed"
            logger.error(f"[FAILED] FFmpeg return code {return_code} for {task['path']}")
            send_ntfy(f"🔴 Failed: {task['path']}")

    except Exception as e:
        logger.error(f"Exception during processing: {e}")
        task["status"] = "error_exception"

def processor_loop():
    logger.info("[PROCESSOR] Service started")
    scan_counter = 0
    while True:
        try:
            tasks = load_tasks()
            task_to_run = None
            
            # Find next task safely
            for task in tasks:
                if task["status"] in ["queued", "waiting_for_resolution"]:
                    # Ensure resolution is set
                    if not task.get("resolution"):
                        # Try to detect from path
                        resolution = detect_resolution_from_path(task["path"])
                        if resolution:
                            task["resolution"] = resolution
                        else:
                            logger.error(f"Cannot determine resolution for {task['path']}")
                            task["status"] = "error_no_resolution"
                            continue
                    
                    task_to_run = task
                    break
            
            if task_to_run:
                # Mark as processing IMMEDIATELY inside the lock to prevent re-selection
                task_to_run["status"] = "processing"
                task_to_run["start_time"] = now()
                save_tasks(tasks) # Save state before starting work
                
                # Do the heavy work outside the lock
                process_video(task_to_run)
                
                # Re-load tasks to save the result (in case new tasks came in while processing)
                # We need to find the specific task object again in the fresh list
                current_tasks = load_tasks()
                for t in current_tasks:
                    if t["path"] == task_to_run["path"] and t["md5"] == task_to_run["md5"]:
                        t.update(task_to_run) # Update fields
                        break
                save_tasks(current_tasks)
            else:
                # No active tasks - increment scan counter and check rotation
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
    
    # Create resolution subfolders
    for res in RESOLUTION_FOLDERS:
        os.makedirs(os.path.join(INPUT_DIR, res), exist_ok=True)
        logger.info(f"[INIT] Created/verified folder: {INPUT_DIR}/{res}")

    initial_scan()

    t1 = threading.Thread(target=start_watcher, daemon=True)
    t2 = threading.Thread(target=start_processor, daemon=True)

    t1.start()
    t2.start()

    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping services...")
