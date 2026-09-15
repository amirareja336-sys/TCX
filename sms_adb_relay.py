import json
import os
import re
import subprocess
import time
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from v6_common import TARGET_SMS_SENDERS, parse_sms_payment


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "relay_config.json")
STATE_FILE = os.path.join(APP_DIR, "relay_state.json")
DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8765",
    "adb_path": "adb",
    "poll_interval_seconds": 2,
    "start_after_id": None,
    "target_senders": TARGET_SMS_SENDERS,
}


def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, sort_keys=True)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except JSONDecodeError as exc:
        raise SystemExit(
            "Invalid relay_config.json at line %s column %s. "
            "Windows paths in JSON need doubled backslashes, for example "
            '"C:\\\\platform-tools\\\\adb.exe", or use forward slashes like '
            '"C:/platform-tools/adb.exe". Original error: %s'
            % (exc.lineno, exc.colno, exc)
        )
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def load_last_seen_state():
    """Persisted high-water mark (last forwarded SMS _id). None if never saved."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            val = json.load(f).get("last_seen_id")
        return int(val) if val not in (None, "") else None
    except Exception:
        return None


def save_last_seen_state(last_seen):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_seen_id": int(last_seen)}, f, indent=2)
    except Exception as exc:
        print("Warning: could not persist relay state: %s" % exc)


def adb_shell(cfg, args):
    cmd = [cfg.get("adb_path", "adb"), "shell"] + args
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=20).decode("utf-8", errors="replace")


def adb_shell_command(cfg, command):
    cmd = [cfg.get("adb_path", "adb"), "shell", command]
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=20).decode("utf-8", errors="replace")


def parse_content_rows(output):
    rows = []
    records = []
    current = []
    for line in output.splitlines():
        if line.startswith("Row:"):
            if current:
                records.append("\n".join(current))
            current = [line.strip()]
        elif current:
            current.append(line.rstrip())
    if current:
        records.append("\n".join(current))

    field_pattern = re.compile(r"(?:(?:^Row:\s*\d+\s+)|,\s+)([A-Za-z0-9_]+)=")
    for record in records:
        matches = list(field_pattern.finditer(record))
        if not matches:
            continue
        fields = {}
        for idx, match in enumerate(matches):
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(record)
            fields[match.group(1)] = record[start:end].strip()
        if fields:
            rows.append(fields)
    return rows


def current_max_id(cfg):
    try:
        out = adb_shell(cfg, ["content", "query", "--uri", "content://sms/inbox", "--projection", "_id"])
    except Exception:
        return 0
    ids = []
    for row in parse_content_rows(out):
        try:
            ids.append(int(row.get("_id", "0")))
        except ValueError:
            pass
    return max(ids) if ids else 0


def read_new_messages(cfg, last_seen):
    command = (
        'content query --uri content://sms/inbox '
        '--projection _id,address,date,body '
        '--where "_id>%s"'
    ) % int(last_seen)
    out = adb_shell_command(cfg, command)
    sender_set = set(cfg.get("target_senders", TARGET_SMS_SENDERS))
    messages = []
    for row in parse_content_rows(out):
        try:
            msg_id = int(row.get("_id", "0"))
        except ValueError:
            continue
        if msg_id <= last_seen:
            continue
        sender = row.get("address", "")
        if sender not in sender_set:
            continue
        payment = parse_sms_payment(sender, row.get("body", ""), row.get("date"), "unix_ms")
        if not payment:
            continue
        payment["external_id"] = "adb:%s" % msg_id
        messages.append((msg_id, payment))
    return sorted(messages, key=lambda item: item[0])


def post_payment(cfg, payment):
    url = cfg["server_url"].rstrip("/") + "/api/relay/sms"
    body = json.dumps(payment, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("relay_token"):
        headers["X-Cred-Token"] = cfg.get("relay_token", "")
    req = Request(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:
            err = str(exc)
        raise RuntimeError(err)
    except URLError as exc:
        raise RuntimeError("Cannot reach server: %s" % exc.reason)


def main():
    cfg = load_config()
    configured_start_after = cfg.get("start_after_id")
    persisted = load_last_seen_state()
    if configured_start_after not in (None, ""):
        # Explicit override (e.g. one-time backfill after an outage).
        last_seen = int(configured_start_after)
        print("Using configured start_after_id %s." % last_seen)
    elif persisted is not None:
        # Resume where we left off so texts received while the relay was down
        # still get forwarded (the server dedupes by external_id).
        last_seen = persisted
        print("Resuming from persisted last_seen _id %s." % last_seen)
    else:
        # First run ever: start from the phone's current newest message.
        last_seen = current_max_id(cfg)
        save_last_seen_state(last_seen)
    print("ADB SMS relay active. Starting after SMS _id %s." % last_seen)
    print("Forwarding %s to %s" % (", ".join(cfg.get("target_senders", [])), cfg["server_url"]))
    try:
        while True:
            try:
                for msg_id, payment in read_new_messages(cfg, last_seen):
                    post_payment(cfg, payment)
                    print("Forwarded %s SMS _id %s: %s" % (payment["channel"], msg_id, payment["display_text"]))
                    last_seen = max(last_seen, msg_id)
                    save_last_seen_state(last_seen)  # persist after each success so a restart resumes here
            except Exception as exc:
                print("Relay error: %s" % exc)
            time.sleep(int(cfg.get("poll_interval_seconds", 2)))
    except KeyboardInterrupt:
        print("\nADB SMS relay stopped.")


if __name__ == "__main__":
    main()
