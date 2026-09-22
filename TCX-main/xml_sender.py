import json
import mimetypes
import os
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "xml_sender_config.json")
DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8765",
    "watch_folder": r"C:\enam-xml-3\pending",
    "sent_archive_folder": r"C:\enam-xml-3\sent",
    "poll_interval_seconds": 0.25,
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


def build_multipart(path):
    boundary = "----CredEntryV6%s" % uuid.uuid4().hex
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "text/xml"
    with open(path, "rb") as f:
        data = f.read()
    head = (
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
        "Content-Type: %s\r\n\r\n"
    ) % (boundary, filename, content_type)
    tail = "\r\n--%s--\r\n" % boundary
    body = head.encode("utf-8") + data + tail.encode("utf-8")
    return boundary, body


def post_xml(cfg, path):
    boundary, body = build_multipart(path)
    headers = {
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
        "X-Source-PC": socket.gethostname(),
    }
    if cfg.get("client_token"):
        headers["X-Cred-Token"] = cfg.get("client_token", "")
    req = Request(cfg["server_url"].rstrip("/") + "/api/xml/upload", data=body, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=12) as resp:
            return resp.read()
    except HTTPError as exc:
        try:
            err = exc.read().decode("utf-8")
        except Exception:
            err = str(exc)
        raise RuntimeError(err)
    except URLError as exc:
        raise RuntimeError("Cannot reach server: %s" % exc.reason)


def stable_file(path):
    try:
        first = path.stat().st_size
        time.sleep(0.05)
        return path.exists() and path.stat().st_size == first
    except OSError:
        return False


def unique_target(folder, filename):
    target = folder / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    return folder / ("%s_%s%s" % (stem, int(time.time() * 1000), suffix))


def upload_pending_xmls(cfg, watch, archive):
    for path in watch.glob("*.xml"):
        if not stable_file(path):
            continue
        try:
            post_xml(cfg, str(path))
            target = unique_target(archive, path.name)
            shutil.move(str(path), str(target))
            print("Uploaded and archived %s" % path.name)
        except Exception as exc:
            print("XML upload failed for %s: %s" % (path.name, exc))


def main():
    cfg = load_config()
    watch = Path(cfg["watch_folder"])
    archive = Path(cfg["sent_archive_folder"])
    watch.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    print("XML sender watching captured folder: %s" % watch)
    print("Posting to: %s" % cfg["server_url"])
    while True:
        upload_pending_xmls(cfg, watch, archive)
        time.sleep(float(cfg.get("poll_interval_seconds", 0.25)))


if __name__ == "__main__":
    main()
