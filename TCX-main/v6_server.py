import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from v6_common import BANKS, CASHIERS, normalize_cashier, now_text, parse_datetime_text, parse_xml_document, safe_filename


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CRED_V6_DATA", os.path.join(APP_DIR, "server_data"))
DB_PATH = os.path.join(DATA_DIR, "cred_entry_v6.db")
CONFIG_PATH = os.path.join(DATA_DIR, "server_config.json")
XML_DIR = os.path.join(DATA_DIR, "received_xmls")
HOST = os.environ.get("CRED_V6_HOST", "0.0.0.0")
PORT = int(os.environ.get("CRED_V6_PORT", "8765"))
XML_PUBLIC_COLUMNS = """
    id, sha256, filename, saved_path, source_ip, cashier_name, reference_number,
    invoice_date, invoice_type, payment_type, customer_name, total_amount, root_tag,
    received_at, matched_entry_id, match_score, match_reason, file_saved
"""


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def ensure_dirs():
    for path in (DATA_DIR, XML_DIR):
        if not os.path.isdir(path):
            os.makedirs(path)


def load_config():
    ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        config = {
            "auth_required": False,
            "ip_allowlist_enabled": False,
            "client_token": secrets.token_hex(24),
            "relay_token": secrets.token_hex(24),
            "admin_token": secrets.token_hex(32),
            "keep_xml_files": False,
            "cashiers": CASHIERS,
            "banks": BANKS,
            "allowed_cashier_ips": ["192.168.1.0/24", "127.0.0.1", "::1"],
        }
        save_config(config)
        return config
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    config.setdefault("auth_required", False)
    config.setdefault("ip_allowlist_enabled", False)
    config.setdefault("keep_xml_files", False)
    for key in ("client_token", "relay_token", "admin_token"):
        config.setdefault(key, secrets.token_hex(24))
    config.setdefault("cashiers", CASHIERS)
    config.setdefault("banks", BANKS)
    config.setdefault("allowed_cashier_ips", ["192.168.1.0/24", "127.0.0.1", "::1"])
    for allowed in ("192.168.1.0/24", "127.0.0.1", "::1"):
        if allowed not in config["allowed_cashier_ips"]:
            config["allowed_cashier_ips"].append(allowed)
    save_config(config)
    return config


def save_config(config):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, sort_keys=True)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    ensure_dirs()
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sms_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                sender TEXT,
                channel TEXT NOT NULL,
                amount REAL NOT NULL,
                payer TEXT,
                received_at TEXT NOT NULL,
                time_source TEXT,
                body TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                logged_entry_id INTEGER,
                logged_by TEXT,
                logged_source_pc TEXT,
                logged_at TEXT,
                reversed_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sms_payment_id INTEGER,
                timestamp TEXT NOT NULL,
                cashier TEXT NOT NULL,
                bank TEXT NOT NULL,
                credit REAL NOT NULL,
                source_pc TEXT NOT NULL,
                local_excel_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                reversal_reason TEXT,
                reversed_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS xml_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT UNIQUE,
                filename TEXT NOT NULL,
                saved_path TEXT NOT NULL,
                source_ip TEXT NOT NULL,
                cashier_name TEXT,
                reference_number TEXT,
                invoice_date TEXT,
                invoice_type TEXT,
                payment_type TEXT,
                customer_name TEXT,
                total_amount REAL NOT NULL,
                root_tag TEXT,
                received_at TEXT NOT NULL,
                matched_entry_id INTEGER,
                match_score INTEGER,
                match_reason TEXT
            )
        """)
        ensure_column(conn, "credit_entries", "source_ip", "TEXT")
        ensure_column(conn, "credit_entries", "session_date", "TEXT")
        ensure_column(conn, "xml_documents", "xml_content", "BLOB")
        ensure_column(conn, "xml_documents", "file_saved", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                actor TEXT,
                source_ip TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)


def ensure_column(conn, table, column, ddl):
    existing = [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
    if column not in existing:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def rowdict(row):
    return dict((k, row[k]) for k in row.keys())


def send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(400, "Invalid JSON.")


class UploadedFile:
    def __init__(self, filename, data):
        self.filename = filename
        self.file = MemoryFile(data)


class MemoryFile:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


def parse_multipart_file(handler):
    content_type = handler.headers.get("Content-Type", "")
    marker = "boundary="
    if marker not in content_type:
        raise ApiError(400, "Missing multipart boundary.")
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    length = int(handler.headers.get("Content-Length", "0") or "0")
    body = handler.rfile.read(length)
    boundary_bytes = ("--" + boundary).encode("utf-8")
    for part in body.split(boundary_bytes):
        part = part.strip()
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].strip()
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, data = part.split(b"\r\n\r\n", 1)
        headers_text = raw_headers.decode("utf-8", errors="replace")
        if 'name="file"' not in headers_text:
            continue
        filename = "upload.xml"
        m = __import__("re").search(r'filename="([^"]+)"', headers_text)
        if m:
            filename = m.group(1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return UploadedFile(filename, data)
    raise ApiError(400, "No file uploaded.")


def query(handler):
    return parse_qs(urlparse(handler.path).query)


def q1(params, key, default=""):
    values = params.get(key)
    return values[0] if values else default


def amount(value):
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        raise ApiError(400, "Amount must be numeric.")


def remote_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    return forwarded.split(",", 1)[0].strip() if forwarded else handler.client_address[0]


def ip_allowed(ip_text, allowed_rules):
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    for rule in allowed_rules:
        rule = str(rule).strip()
        if not rule:
            continue
        try:
            if "/" in rule and ip in ipaddress.ip_network(rule, strict=False):
                return True
            if ip == ipaddress.ip_address(rule):
                return True
        except ValueError:
            if ip_text == rule:
                return True
    return False


def require_token(handler, role):
    if not handler.server.config.get("auth_required", False):
        return
    supplied = handler.headers.get("X-Cred-Token", "")
    expected = handler.server.config.get("%s_token" % role, "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise ApiError(401, "Invalid %s token." % role)
    if role == "client":
        ip = remote_ip(handler)
        allowed = handler.server.config.get("allowed_cashier_ips", [])
        if handler.server.config.get("ip_allowlist_enabled", False) and allowed and not ip_allowed(ip, allowed):
            raise ApiError(403, "Client IP is not allowed: %s" % ip)


def audit(conn, event_type, actor, source_ip, details):
    conn.execute(
        "INSERT INTO audit_log(event_type, actor, source_ip, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, actor, source_ip, json.dumps(details, ensure_ascii=False), now_text()),
    )


def list_sms(params):
    clauses = []
    values = []
    since_id = q1(params, "since_id")
    if since_id:
        clauses.append("id > ?")
        values.append(int(since_id))
    sql = "SELECT * FROM sms_payments"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    values.append(min(max(int(q1(params, "limit", "200")), 1), 1000))
    with db() as conn:
        return [rowdict(r) for r in conn.execute(sql, values).fetchall()]


def list_entries(params):
    clauses = []
    values = []
    if q1(params, "status"):
        clauses.append("status = ?")
        values.append(q1(params, "status"))
    # Date filtering keys off the business/session date (the shift's start date,
    # which stays fixed even when a shift runs past midnight). Rows logged before
    # session_date existed fall back to the entry timestamp's calendar day.
    # date_from/date_to are inclusive calendar-day bounds (YYYY-MM-DD), compared
    # lexically, which is safe for the fixed ISO-like formats used here.
    date_expr = "COALESCE(NULLIF(session_date, ''), substr(timestamp, 1, 10))"
    date_from = q1(params, "date_from", "").strip()
    date_to = q1(params, "date_to", "").strip()
    if date_from:
        clauses.append(f"{date_expr} >= ?")
        values.append(date_from[:10])
    if date_to:
        clauses.append(f"{date_expr} <= ?")
        values.append(date_to[:10])
    sql = "SELECT * FROM credit_entries"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    values.append(min(max(int(q1(params, "limit", "500")), 1), 5000))
    with db() as conn:
        return [rowdict(r) for r in conn.execute(sql, values).fetchall()]


def add_cashier(handler, payload):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, "Cashier name is required.")
    config = handler.server.config
    cashiers = list(config.get("cashiers", []))
    if not any(c.strip().lower() == name.lower() for c in cashiers):
        cashiers.append(name)
        config["cashiers"] = sorted(cashiers)
        save_config(config)
    return {"cashiers": config["cashiers"]}


def upsert_sms(payload, source_ip):
    external_id = str(payload.get("external_id") or "").strip() or None
    channel = str(payload.get("channel") or "").strip()
    if not channel:
        raise ApiError(400, "SMS channel is required.")
    with db() as conn:
        if external_id:
            existing = conn.execute("SELECT * FROM sms_payments WHERE external_id = ?", (external_id,)).fetchone()
            if existing:
                return rowdict(existing)
        cur = conn.execute(
            """
            INSERT INTO sms_payments(external_id, sender, channel, amount, payer, received_at, time_source, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                payload.get("sender"),
                channel,
                amount(payload.get("amount")),
                payload.get("payer", ""),
                payload.get("received_at") or now_text(),
                payload.get("time_source", ""),
                payload.get("body", ""),
                now_text(),
            ),
        )
        row = conn.execute("SELECT * FROM sms_payments WHERE id = ?", (cur.lastrowid,)).fetchone()
        audit(conn, "sms_ingested", "relay", source_ip, {"sms_id": cur.lastrowid, "external_id": external_id})
        return rowdict(row)


def create_entry(payload, source_ip):
    cashier = str(payload.get("cashier") or "").strip()
    bank = str(payload.get("bank") or "").strip()
    if not cashier or not bank:
        raise ApiError(400, "Cashier and bank are required.")
    sms_id = payload.get("sms_payment_id")
    with db() as conn:
        if sms_id:
            sms = conn.execute("SELECT * FROM sms_payments WHERE id = ?", (int(sms_id),)).fetchone()
            if not sms:
                raise ApiError(404, "SMS payment not found.")
            if sms["status"] == "logged":
                raise ApiError(409, "SMS already logged by %s." % (sms["logged_by"] or "another cashier"))
            if abs(float(sms["amount"]) - amount(payload.get("credit"))) > 0.01:
                raise ApiError(400, "Credit amount does not match selected SMS.")
        # Business date of the shift = when the session started (survives midnight).
        # Fall back to the entry timestamp's calendar day if the client did not send one.
        entry_ts = payload.get("timestamp") or now_text()
        session_date = str(payload.get("session_date") or "").strip() or entry_ts[:10]
        cur = conn.execute(
            """
            INSERT INTO credit_entries(sms_payment_id, timestamp, session_date, cashier, bank, credit, source_pc, source_ip, local_excel_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(sms_id) if sms_id else None,
                entry_ts,
                session_date,
                cashier,
                bank,
                amount(payload.get("credit")),
                payload.get("source_pc") or source_ip,
                source_ip,
                payload.get("local_excel_id"),
                now_text(),
            ),
        )
        entry_id = cur.lastrowid
        if sms_id:
            conn.execute(
                """
                UPDATE sms_payments
                SET status='logged', logged_entry_id=?, logged_by=?, logged_source_pc=?, logged_at=?, reversed_at=NULL
                WHERE id=?
                """,
                (entry_id, cashier, payload.get("source_pc") or source_ip, now_text(), int(sms_id)),
            )
        audit(conn, "entry_created", cashier, source_ip, {"entry_id": entry_id, "sms_payment_id": sms_id})
        return rowdict(conn.execute("SELECT * FROM credit_entries WHERE id = ?", (entry_id,)).fetchone())


def reverse_entry(payload, source_ip):
    entry_id = int(payload.get("entry_id"))
    cashier = str(payload.get("cashier") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    with db() as conn:
        entry = conn.execute("SELECT * FROM credit_entries WHERE id = ?", (entry_id,)).fetchone()
        if not entry:
            raise ApiError(404, "Entry not found.")
        if entry["status"] == "reversed":
            return rowdict(entry)
        if cashier and entry["cashier"] != cashier:
            raise ApiError(403, "Cashiers can reverse only their own entries.")
        conn.execute(
            "UPDATE credit_entries SET status='reversed', reversal_reason=?, reversed_at=? WHERE id=?",
            (reason, now_text(), entry_id),
        )
        if entry["sms_payment_id"]:
            conn.execute(
                """
                UPDATE sms_payments
                SET status='reversed', logged_entry_id=NULL, logged_by=?, logged_source_pc=?, reversed_at=?
                WHERE id=?
                """,
                (entry["cashier"], entry["source_pc"], now_text(), entry["sms_payment_id"]),
            )
        audit(conn, "entry_reversed", entry["cashier"], source_ip, {"entry_id": entry_id, "reason": reason})
        return rowdict(conn.execute("SELECT * FROM credit_entries WHERE id = ?", (entry_id,)).fetchone())


def save_xml_upload(file_item, source_ip, keep_xml_file=False):
    filename = safe_filename(file_item.filename)
    data = file_item.file.read()
    parsed = parse_xml_document(data)
    path = ""
    file_saved = 0
    if keep_xml_file:
        target_dir = os.path.join(XML_DIR, "(%s) received" % source_ip)
        if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        path = os.path.join(target_dir, filename)
        counter = 1
        base, ext = os.path.splitext(path)
        while os.path.exists(path):
            path = "%s_%s%s" % (base, counter, ext)
            counter += 1
        with open(path, "wb") as f:
            f.write(data)
        file_saved = 1
    with db() as conn:
        existing = conn.execute("SELECT %s FROM xml_documents WHERE sha256 = ?" % XML_PUBLIC_COLUMNS, (parsed["sha256"],)).fetchone()
        if existing:
            return rowdict(existing)
        cur = conn.execute(
            """
            INSERT INTO xml_documents(
                sha256, filename, saved_path, source_ip, cashier_name, reference_number,
                invoice_date, invoice_type, payment_type, customer_name, total_amount, root_tag,
                received_at, xml_content, file_saved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed["sha256"], filename, path, source_ip, parsed["cashier_name"],
                parsed["reference_number"], parsed["invoice_date"], parsed["invoice_type"],
                parsed["payment_type"], parsed["customer_name"], parsed["total_amount"],
                parsed["root_tag"], now_text(), data, file_saved,
            ),
        )
        audit(conn, "xml_received", "xml_client", source_ip, {"xml_id": cur.lastrowid, "filename": filename})
        return rowdict(conn.execute("SELECT %s FROM xml_documents WHERE id = ?" % XML_PUBLIC_COLUMNS, (cur.lastrowid,)).fetchone())


def date_bounds(params):
    date_from = q1(params, "date_from", "").strip()
    date_to = q1(params, "date_to", "").strip()
    start = parse_datetime_text(date_from) if date_from else None
    end = parse_datetime_text(date_to) if date_to else None
    if start and len(date_from) == 10:
        start = start.replace(hour=0, minute=0, second=0)
    if end and len(date_to) == 10:
        end = end.replace(hour=23, minute=59, second=59)
    return start, end


def in_bounds(value, start, end):
    if not start and not end:
        return True
    dt = parse_datetime_text(value)
    if not dt:
        return False
    if start and dt < start:
        return False
    if end and dt > end:
        return False
    return True


def seconds_between(left, right):
    left_dt = parse_datetime_text(left)
    right_dt = parse_datetime_text(right)
    if not left_dt or not right_dt:
        return None
    return abs((left_dt - right_dt).total_seconds())


def match_candidate_score(xml_row, entry_row):
    amount_diff = abs(float(xml_row["total_amount"]) - float(entry_row["credit"]))
    if amount_diff > 0.01:
        return None

    score = 60
    reasons = ["amount exact"]

    xml_dt = xml_row.get("invoice_date") or xml_row.get("received_at")
    entry_dt = entry_row.get("sms_received_at") or entry_row.get("timestamp")
    delta = seconds_between(xml_dt, entry_dt)
    if delta is not None:
        minutes = int(round(delta / 60.0))
        if delta <= 5 * 60:
            score += 30
            reasons.append("time within 5m")
        elif delta <= 30 * 60:
            score += 22
            reasons.append("time within 30m")
        elif delta <= 2 * 60 * 60:
            score += 14
            reasons.append("time within 2h")
        elif delta <= 24 * 60 * 60:
            score += 6
            reasons.append("same-day/nearby time")
        else:
            score -= 20
            reasons.append("time %sm apart" % minutes)

    xml_cashier = normalize_cashier(xml_row.get("cashier_name"))
    entry_cashier = normalize_cashier(entry_row.get("cashier"))
    if xml_cashier and entry_cashier:
        if xml_cashier == entry_cashier or xml_cashier.split()[0] in entry_cashier:
            score += 20
            reasons.append("cashier matches")
        else:
            score -= 8
            reasons.append("cashier differs")

    source_ip = xml_row.get("source_ip") or ""
    source_pc = entry_row.get("source_pc") or ""
    if source_ip and source_pc and source_ip in source_pc:
        score += 8
        reasons.append("source matches")

    sms_channel = entry_row.get("sms_channel") or ""
    if sms_channel and sms_channel == entry_row.get("bank"):
        score += 5
        reasons.append("bank matches SMS")

    return score, ", ".join(reasons)


def reconciliation(params=None):
    params = params or {}
    start, end = date_bounds(params)
    with db() as conn:
        xml_rows = [rowdict(r) for r in conn.execute("SELECT %s FROM xml_documents ORDER BY COALESCE(invoice_date, received_at) DESC" % XML_PUBLIC_COLUMNS).fetchall()]
        entries = [rowdict(r) for r in conn.execute("""
            SELECT ce.*, sp.received_at AS sms_received_at, sp.channel AS sms_channel, sp.payer AS sms_payer
            FROM credit_entries ce
            LEFT JOIN sms_payments sp ON sp.id = ce.sms_payment_id
            WHERE ce.status='active'
            ORDER BY COALESCE(sp.received_at, ce.timestamp) DESC
        """).fetchall()]
    xml_rows = [x for x in xml_rows if in_bounds(x.get("invoice_date") or x.get("received_at"), start, end)]
    entries = [e for e in entries if in_bounds(e.get("sms_received_at") or e.get("timestamp"), start, end)]
    used = set()
    matches = []
    unmatched_xml = []
    for x in xml_rows:
        best = None
        best_score = -1
        best_reason = ""
        for e in entries:
            if e["id"] in used:
                continue
            scored = match_candidate_score(x, e)
            if not scored:
                continue
            score, reason = scored
            if score > best_score:
                best = e
                best_score = score
                best_reason = reason
        if best:
            used.add(best["id"])
            matches.append({"xml": x, "entry": best, "score": best_score, "reason": best_reason})
        else:
            unmatched_xml.append(x)
    unmatched_entries = [e for e in entries if e["id"] not in used]
    return {
        "date_from": q1(params, "date_from", ""),
        "date_to": q1(params, "date_to", ""),
        "matches": matches,
        "unmatched_xml": unmatched_xml,
        "unmatched_entries": unmatched_entries,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CredEntryV6/0.1"

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/ping":
                send_json(self, 200, {"ok": True, "time": now_text()})
                return
            if path == "/api/config":
                require_token(self, "client")
                send_json(self, 200, {"cashiers": self.server.config["cashiers"], "banks": self.server.config["banks"]})
            elif path == "/api/sms":
                require_token(self, "client")
                send_json(self, 200, {"sms": list_sms(query(self))})
            elif path == "/api/entries":
                require_token(self, "client")
                send_json(self, 200, {"entries": list_entries(query(self))})
            elif path == "/api/admin/reconciliation":
                require_token(self, "admin")
                send_json(self, 200, reconciliation(query(self)))
            else:
                raise ApiError(404, "Not found.")
        except ApiError as exc:
            send_json(self, exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:
            send_json(self, 500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/relay/sms":
                require_token(self, "relay")
                send_json(self, 201, {"sms": upsert_sms(parse_json(self), remote_ip(self))})
            elif path == "/api/entries":
                require_token(self, "client")
                send_json(self, 201, {"entry": create_entry(parse_json(self), remote_ip(self))})
            elif path == "/api/cashiers":
                require_token(self, "client")
                send_json(self, 201, add_cashier(self, parse_json(self)))
            elif path == "/api/entries/reverse":
                require_token(self, "client")
                send_json(self, 200, {"entry": reverse_entry(parse_json(self), remote_ip(self))})
            elif path == "/api/xml/upload":
                require_token(self, "client")
                send_json(self, 201, {"xml": save_xml_upload(parse_multipart_file(self), remote_ip(self), self.server.config.get("keep_xml_files", False))})
            else:
                raise ApiError(404, "Not found.")
        except ApiError as exc:
            send_json(self, exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:
            send_json(self, 500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (remote_ip(self), fmt % args))


class Server(ThreadingHTTPServer):
    def __init__(self):
        ThreadingHTTPServer.__init__(self, (HOST, PORT), Handler)
        self.config = load_config()


def main():
    init_db()
    server = Server()
    print("Cred Entry v6 server listening on http://%s:%s" % (HOST, PORT))
    print("Config: %s" % CONFIG_PATH)
    if server.config.get("auth_required", False):
        print("Client token: %s" % server.config["client_token"])
        print("Relay token:  %s" % server.config["relay_token"])
        print("Admin token:  %s" % server.config["admin_token"])
    else:
        print("Auth disabled for LAN testing.")
    server.serve_forever()


if __name__ == "__main__":
    main()
