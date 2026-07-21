import os
import time
import shutil
from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler

SOURCE_DIR = "/Volumes/Active Recording"
DEST_DIR = "./data/live_audio"

class FileCopyHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        
        src_path = os.fsdecode(event.src_path)
        
        # Only process files after they have been transcoded to mp3
        if src_path.lower().endswith(".mp3"):
            time.sleep(1) 
            
            # Preserve the channel subfolder structure
            rel_path = os.path.relpath(src_path, SOURCE_DIR)
            dest_path = os.path.join(DEST_DIR, rel_path)
            
            # Create the specific channel folder if it does not exist locally
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            
            try:
                shutil.copy2(src_path, dest_path)
                print(f"Copied: {rel_path}")
            except Exception as e:
                print(f"Error copying {rel_path}: {e}")

if __name__ == "__main__":
    os.makedirs(DEST_DIR, exist_ok=True)
    observer = Observer()
    observer.schedule(FileCopyHandler(), SOURCE_DIR, recursive=True)
    observer.start()
    print(f"Watching '{SOURCE_DIR}' for new files via polling...")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()