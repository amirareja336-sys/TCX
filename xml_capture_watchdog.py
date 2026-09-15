import json
import os
import shutil
import time
from pathlib import Path


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "xml_capture_config.json")
DEFAULT_CONFIG = {
    "source_folder": r"C:\OrbitHealth\Common\POSInterface\Maraki\Receipts",
    "local_capture_folder": r"C:\enam-xml-3\pending",
    "poll_interval_seconds": 0.02,
}


def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, sort_keys=True)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def unique_target(folder, filename):
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    return folder / ("%s_%s%s" % (stem, int(time.time() * 1000), suffix))


def file_signature(path):
    try:
        stat = path.stat()
        return "%s|%s|%s" % (path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return ""


def copy_xml(source, capture_folder):
    target = unique_target(capture_folder, source.name)
    temp_target = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(str(source), str(temp_target))
        shutil.copystat(str(source), str(temp_target), follow_symlinks=True)
        os.replace(str(temp_target), str(target))
        print("Captured %s" % source.name)
        return True
    except Exception as exc:
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass
        print("XML capture failed for %s: %s" % (source.name, exc))
        return False


def capture_once(source_folder, capture_folder, seen):
    if not source_folder.is_dir():
        return
    for source in source_folder.glob("*.xml"):
        sig = file_signature(source)
        if not sig or sig in seen:
            continue
        if copy_xml(source, capture_folder):
            seen.add(sig)


def main():
    cfg = load_config()
    source_folder = Path(cfg["source_folder"])
    capture_folder = Path(cfg["local_capture_folder"])
    capture_folder.mkdir(parents=True, exist_ok=True)
    seen = set()
    print("XML capture watchdog watching: %s" % source_folder)
    print("Copying XMLs to: %s" % capture_folder)
    while True:
        capture_once(source_folder, capture_folder, seen)
        time.sleep(float(cfg.get("poll_interval_seconds", 0.10)))


if __name__ == "__main__":
    main()
