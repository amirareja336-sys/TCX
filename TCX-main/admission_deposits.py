import json
import os
import re
import socket
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import openpyxl
from openpyxl.styles import PatternFill

from v6_common import BANKS, CASHIERS

# This app logs "admission deposits" (single fixed user), not per-cashier credits.
ADMISSION_USER = "admission deposits"


def receipt_url(channel, body):
    """Build/extract the bank receipt link from an SMS body.
    Telebirr: construct from the 10-char transaction number.
    CBE / Awash: the receipt URL is present in the body — extract it.
    Returns "" when no link is available (e.g. BOA — not supported)."""
    body = body or ""
    ch = (channel or "").strip().lower()
    if ch == "telebirr":
        m = re.search(r"transaction number is\s+([A-Za-z0-9]{10})", body, re.IGNORECASE)
        if not m:
            m = re.search(r"\b([A-Z0-9]{10})\b", body)  # fallback: a standalone 10-char token
        return ("https://transactioninfo.ethiotelecom.et/receipt/" + m.group(1)) if m else ""
    if ch in ("cbe", "awash"):
        m = re.search(r"https?://\S+", body)
        return m.group(0).rstrip(".,;)") if m else ""
    return ""


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "client_config.json")
SESSION_STATE_FILE = "session_state_admission.json"
HEADERS = ["ID", "Timestamp", "Cashier", "Bank", "Credit", "Status", "ServerEntryID", "SmsID"]
RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
CURRENT_SESSION_DIRECTORY = None
CURRENT_MARKED_FILE = None
CURRENT_CLEAN_FILE = None
CURRENT_SESSION_DATE = None  # business date = date the session started (survives midnight)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = {"server_url": "http://127.0.0.1:8765"}
        save_config(cfg)
        return cfg
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("server_url", "http://127.0.0.1:8765")
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)


class Api:
    def __init__(self, cfg):
        self.cfg = cfg

    def url(self, path, params=None):
        base = self.cfg["server_url"].rstrip("/")
        return base + path + (("?" + urlencode(params)) if params else "")

    def request(self, method, path, payload=None, params=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cfg.get("client_token"):
            headers["X-Cred-Token"] = self.cfg.get("client_token", "")
        req = Request(self.url(path, params), data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                err = str(exc)
            raise RuntimeError(err)
        except URLError as exc:
            raise RuntimeError("Cannot reach server: %s" % exc.reason)

    def config(self):
        return self.request("GET", "/api/config")

    def sms(self):
        return self.request("GET", "/api/sms", params={"limit": "300"})["sms"]

    def entries(self, date_from=None, date_to=None):
        params = {"limit": "500"}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self.request("GET", "/api/entries", params=params)["entries"]

    def create_entry(self, payload):
        return self.request("POST", "/api/entries", payload)["entry"]

    def reverse_entry(self, entry_id, cashier, reason):
        return self.request("POST", "/api/entries/reverse", {"entry_id": entry_id, "cashier": cashier, "reason": reason})["entry"]

    def add_cashier(self, name):
        return self.request("POST", "/api/cashiers", {"name": name})["cashiers"]


def today():
    return datetime.now().strftime("%Y-%m-%d")


def _session_date_from_dir(session_dir: str) -> str:
    # Folder is named "<host>_<YYYY-MM-DD>" (optionally "_<n>"); pull the date.
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(session_dir).name)
    return m.group(1) if m else today()


def get_session_files():
    global CURRENT_SESSION_DIRECTORY, CURRENT_MARKED_FILE, CURRENT_CLEAN_FILE, CURRENT_SESSION_DATE
    if CURRENT_SESSION_DIRECTORY and Path(CURRENT_SESSION_DIRECTORY).exists():
        return
    if Path(SESSION_STATE_FILE).exists():
        try:
            state = json.loads(Path(SESSION_STATE_FILE).read_text())
            session_dir = state.get("session_directory")
            if session_dir and Path(session_dir).exists():
                CURRENT_SESSION_DIRECTORY = session_dir
                # Prefer the stored session_date; fall back to the folder name.
                CURRENT_SESSION_DATE = state.get("session_date") or _session_date_from_dir(session_dir)
                name = Path(session_dir).name
                CURRENT_MARKED_FILE = Path(session_dir) / ("marked_%s.xlsx" % name)
                CURRENT_CLEAN_FILE = Path(session_dir) / ("clean_%s.xlsx" % name)
                return
        except Exception:
            pass
    session_date = today()  # business date fixed at session start
    base = "admission_%s_%s" % (socket.gethostname(), session_date)
    session_dir = base
    i = 1
    while Path(session_dir).exists() and any(Path(session_dir).iterdir()):
        session_dir = "%s_%s" % (base, i)
        i += 1
    Path(session_dir).mkdir(exist_ok=True)
    CURRENT_SESSION_DIRECTORY = session_dir
    CURRENT_SESSION_DATE = session_date
    name = Path(session_dir).name
    CURRENT_MARKED_FILE = Path(session_dir) / ("marked_%s.xlsx" % name)
    CURRENT_CLEAN_FILE = Path(session_dir) / ("clean_%s.xlsx" % name)
    Path(SESSION_STATE_FILE).write_text(json.dumps(
        {"session_directory": session_dir, "session_date": session_date}, indent=2))


def current_session_date() -> str:
    """Business date of the active session (its start date). Ensures a session exists."""
    get_session_files()
    return CURRENT_SESSION_DATE or today()


def init_workbook(path):
    if not Path(path).exists():
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Entries"
        ws.append(HEADERS)
        wb.save(path)


def next_id(ws, cashier):
    max_id = 0
    headers = [cell.value for cell in ws[1]]
    try:
        id_idx = headers.index("ID")
        cashier_idx = headers.index("Cashier")
    except ValueError:
        id_idx = 0
        cashier_idx = 2
    for row in ws.iter_rows(min_row=2, values_only=True):
        row_cashier = row[cashier_idx] if len(row) > cashier_idx else ""
        if str(row_cashier) != str(cashier):
            continue
        try:
            max_id = max(max_id, int(row[id_idx]))
        except Exception:
            pass
    return max_id + 1


def save_local_entry(entry):
    get_session_files()
    init_workbook(CURRENT_MARKED_FILE)
    init_workbook(CURRENT_CLEAN_FILE)
    wb = openpyxl.load_workbook(CURRENT_MARKED_FILE)
    ws = wb.active
    entry = dict(entry)
    local_id = next_id(ws, entry.get("Cashier", ""))
    entry["ID"] = local_id
    row = [entry.get(h, "") for h in HEADERS]
    ws.append(row)
    wb.save(CURRENT_MARKED_FILE)
    wb.close()
    wb = openpyxl.load_workbook(CURRENT_CLEAN_FILE)
    wb.active.append(row)
    wb.save(CURRENT_CLEAN_FILE)
    wb.close()
    return entry


def mark_local_deleted(local_id):
    get_session_files()
    for path, hard_delete in ((CURRENT_MARKED_FILE, False), (CURRENT_CLEAN_FILE, True)):
        if not Path(path).exists():
            continue
        wb = openpyxl.load_workbook(path)
        ws = wb.active
        for idx in range(ws.max_row, 1, -1):
            if str(ws.cell(idx, 1).value) == str(local_id):
                if hard_delete:
                    ws.delete_rows(idx)
                else:
                    status_col = HEADERS.index("Status") + 1
                    ws.cell(idx, status_col).value = "reversed"
                    for cell in ws[idx]:
                        cell.fill = RED_FILL
                break
        wb.save(path)
        wb.close()


def update_local_server_entry(local_id, server_entry_id, status="active"):
    get_session_files()
    for path in (CURRENT_MARKED_FILE, CURRENT_CLEAN_FILE):
        if not Path(path).exists():
            continue
        wb = openpyxl.load_workbook(path)
        ws = wb.active
        headers = [cell.value for cell in ws[1]]
        try:
            server_col = headers.index("ServerEntryID") + 1
            status_col = headers.index("Status") + 1
        except ValueError:
            wb.close()
            continue
        for idx in range(ws.max_row, 1, -1):
            if str(ws.cell(idx, 1).value) == str(local_id):
                ws.cell(idx, server_col).value = server_entry_id
                ws.cell(idx, status_col).value = status
                break
        wb.save(path)
        wb.close()


def end_current_session():
    global CURRENT_SESSION_DIRECTORY, CURRENT_MARKED_FILE, CURRENT_CLEAN_FILE, CURRENT_SESSION_DATE
    if Path(SESSION_STATE_FILE).exists():
        Path(SESSION_STATE_FILE).unlink()
    CURRENT_SESSION_DIRECTORY = None
    CURRENT_SESSION_DATE = None
    CURRENT_MARKED_FILE = None
    CURRENT_CLEAN_FILE = None


def load_offline_queue(cashier):
    get_session_files()
    queue = []
    if not CURRENT_MARKED_FILE or not Path(CURRENT_MARKED_FILE).exists():
        return queue
    
    try:
        wb = openpyxl.load_workbook(CURRENT_MARKED_FILE, data_only=True)
        ws = wb.active
        headers = [str(c.value) for c in ws[1]]
        
        for row in ws.iter_rows(min_row=2, values_only=True):
            row_dict = dict(zip(headers, row))
            if str(row_dict.get("Cashier", "")) != cashier:
                continue
            if str(row_dict.get("Status", "")) == "reversed":
                continue
            if not row_dict.get("ServerEntryID"):
                queue.append({
                    "local_excel_id": row_dict.get("ID"),
                    "timestamp": str(row_dict.get("Timestamp", "")),
                    "session_date": current_session_date(),
                    "cashier": cashier,
                    "bank": str(row_dict.get("Bank", "")),
                    "credit": str(row_dict.get("Credit", "")),
                    "source_pc": socket.gethostname(),
                    "sms_payment_id": row_dict.get("SmsID") or None
                })
        wb.close()
    except Exception:
        pass
    return queue


class CashierFrame(ttk.Frame):
    def __init__(self, master, api, app_cfg):
        ttk.Frame.__init__(self, master, padding=12)
        self.api = api
        self.app_cfg = app_cfg
        self.pack(fill="both", expand=True)
        ttk.Label(self, text="Select Cashier", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 8))
        for cashier in app_cfg.get("cashiers", CASHIERS):
            ttk.Button(self, text=cashier, command=lambda c=cashier: master.open_main(api, app_cfg, c)).pack(fill="x", pady=3)
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(self, text="+ Add Cashier", command=self.add_cashier).pack(fill="x", pady=3)

    def add_cashier(self):
        name = simpledialog.askstring("Add Cashier", "New cashier name:", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            return
        try:
            cashiers = self.api.add_cashier(name)
        except Exception as exc:
            messagebox.showerror("Add Cashier", str(exc))
            return
        self.app_cfg["cashiers"] = cashiers
        # Rebuild the selection screen so the new cashier appears.
        self.master.open_cashiers(self.api, self.app_cfg)


class MainFrame(ttk.Frame):
    def __init__(self, master, api, app_cfg, cashier):
        ttk.Frame.__init__(self, master, padding=8)
        self.api = api
        self.app_cfg = app_cfg
        self.cashier = cashier
        self.selected_sms = None
        self.selected_entry_id = None
        self.bank_var = tk.StringVar()
        self.credit_var = tk.StringVar()
        self.sms_all = []          # all fetched SMS rows (newest first)
        self.sms_page = 0          # current page index for the SMS list
        self.SMS_PAGE_SIZE = 25
        self.entries = []
        self.offline_queue = load_offline_queue(self.cashier)
        self.is_online = True
        self.server_entries_cache = []
        self.pack(fill="both", expand=True)
        self.build()
        self.network_loop()

    def build(self):
        style = ttk.Style()
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        form = ttk.LabelFrame(self, text="New Entry", padding=8)
        form.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Cashier: %s" % self.cashier, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.bank_buttons = {}
        for idx, bank in enumerate(self.app_cfg.get("banks", BANKS)):
            btn = tk.Button(form, text=bank, width=15, bg="#f0f0f0", command=lambda b=bank: self.select_bank(b))
            btn.grid(row=1 + idx // 2, column=idx % 2, padx=3, pady=3)
            self.bank_buttons[bank] = btn
        row = 9
        ttk.Label(form, text="Credit Amount").grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.credit_entry = ttk.Entry(form, textvariable=self.credit_var)
        self.credit_entry.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=4)
        self.credit_entry.bind("<Return>", lambda _event: self.submit())
        ttk.Button(form, text="Submit", command=self.submit).grid(row=row + 2, column=0, columnspan=2, sticky="ew", pady=4)
        tk.Button(form, text="End Session & Close", command=self.end_session_with_confirmation, bg="#b91c1c", fg="white", activebackground="#991b1b", activeforeground="white", relief="raised").grid(row=row + 3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.feedback = ttk.Label(form, text="")
        self.feedback.grid(row=row + 4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        work_area = ttk.Frame(self)
        work_area.grid(row=0, column=1, sticky="nsew")
        work_area.columnconfigure(0, weight=1)
        work_area.rowconfigure(0, weight=1)
        work_area.rowconfigure(1, weight=1)

        log_box = ttk.LabelFrame(work_area, text="Entries Log", padding=8)
        log_box.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        log_box.rowconfigure(1, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.session_label = ttk.Label(log_box, text="")
        self.session_label.grid(row=0, column=0, sticky="w")
        cols = ("local", "server", "time", "cashier", "bank", "credit", "sms", "status")
        self.entry_tree = ttk.Treeview(log_box, columns=cols, show="headings")
        entry_columns = (
            ("local", "Local", 55, "center"),
            ("server", "Server", 65, "center"),
            ("time", "Time", 145, "w"),
            ("cashier", "Cashier", 95, "w"),
            ("bank", "Bank", 120, "w"),
            ("credit", "Credit", 95, "e"),
            ("sms", "SMS", 55, "center"),
            ("status", "Status", 75, "center"),
        )
        for col, label, width, anchor in entry_columns:
            self.entry_tree.heading(col, text=label)
            self.entry_tree.column(col, width=width, minwidth=width, anchor=anchor, stretch=(col == "time"))
        self.entry_tree.grid(row=1, column=0, sticky="nsew")
        entry_y = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.entry_tree.yview)
        entry_x = ttk.Scrollbar(log_box, orient=tk.HORIZONTAL, command=self.entry_tree.xview)
        self.entry_tree.configure(yscrollcommand=entry_y.set, xscrollcommand=entry_x.set)
        entry_y.grid(row=1, column=1, sticky="ns")
        entry_x.grid(row=2, column=0, sticky="ew")
        self.entry_tree.bind("<<TreeviewSelect>>", self.pick_entry)
        ttk.Button(log_box, text="Reverse Selected", command=self.reverse_selected).grid(row=3, column=0, sticky="w", pady=5)
        self.session_total_label = ttk.Label(log_box, text="Total: 0.00", font=("Segoe UI", 11, "bold"))
        self.session_total_label.grid(row=3, column=0, sticky="e", pady=5)

        sms_box = ttk.LabelFrame(work_area, text="Live Incoming Payments", padding=8)
        sms_box.grid(row=1, column=0, sticky="nsew")
        sms_box.rowconfigure(1, weight=1)
        sms_box.columnconfigure(0, weight=1)
        sms_top = ttk.Frame(sms_box)
        sms_top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Button(sms_top, text="Open Receipt", command=self.open_selected_receipt).pack(side="left")
        ttk.Button(sms_top, text="Refresh", command=self.refresh_sms).pack(side="right")
        self.sms_tree = ttk.Treeview(sms_box, columns=("id", "received", "status", "bank", "amount", "payer", "receipt", "logged"), show="headings", height=18)
        sms_columns = (
            ("id", "ID", 55, "center"),
            ("received", "Received", 150, "w"),
            ("status", "Status", 80, "center"),
            ("bank", "Bank", 125, "w"),
            ("amount", "Amount", 100, "e"),
            ("payer", "Payer", 200, "w"),
            ("receipt", "Receipt", 90, "center"),
            ("logged", "Logged By", 105, "w"),
        )
        for col, label, width, anchor in sms_columns:
            self.sms_tree.heading(col, text=label)
            self.sms_tree.column(col, width=width, minwidth=width, anchor=anchor, stretch=(col == "payer"))
        # Sharper alternating-row contrast (clear blue vs white).
        self.entry_tree.tag_configure("odd", background="#bcd4f0")
        self.entry_tree.tag_configure("even", background="#ffffff")
        self.entry_tree.tag_configure("reversed", foreground="#7f1d1d", background="#fecaca")
        self.sms_tree.tag_configure("odd", background="#bcd4f0")
        self.sms_tree.tag_configure("even", background="#ffffff")
        # Logged SMS: obvious bright-green highlight instead of a faint grey.
        self.sms_tree.tag_configure("logged", foreground="#14532d", background="#86efac")
        self.sms_tree.tag_configure("reversed", foreground="#7f1d1d", background="#fecaca")
        self.sms_tree.grid(row=1, column=0, sticky="nsew")
        sms_y = ttk.Scrollbar(sms_box, orient=tk.VERTICAL, command=self.sms_tree.yview)
        sms_x = ttk.Scrollbar(sms_box, orient=tk.HORIZONTAL, command=self.sms_tree.xview)
        self.sms_tree.configure(yscrollcommand=sms_y.set, xscrollcommand=sms_x.set)
        sms_y.grid(row=1, column=1, sticky="ns")
        sms_x.grid(row=2, column=0, sticky="ew")
        self.sms_tree.bind("<<TreeviewSelect>>", self.pick_sms)
        self.sms_tree.bind("<Double-1>", self.open_selected_receipt)  # double-click a text to open its receipt

        pager = ttk.Frame(sms_box)
        pager.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.sms_prev_btn = ttk.Button(pager, text="< Prev", command=self.sms_prev_page)
        self.sms_prev_btn.pack(side="left")
        self.sms_page_label = ttk.Label(pager, text="")
        self.sms_page_label.pack(side="left", padx=8)
        self.sms_next_btn = ttk.Button(pager, text="Next >", command=self.sms_next_page)
        self.sms_next_btn.pack(side="left")
        
        self.offline_banner = tk.Label(self.sms_tree, text="OFFLINE - Enter manually", font=("Segoe UI", 16, "bold"), fg="white", bg="#b91c1c")

    def select_bank(self, bank):
        self.bank_var.set(bank)
        for name, btn in self.bank_buttons.items():
            btn.config(relief="sunken" if name == bank else "raised", bg="#4db6ac" if name == bank else "#f0f0f0")

    def network_loop(self):
        try:
            self.api.config()  # ping
            was_offline = not self.is_online
            self.is_online = True
            
            if was_offline:
                self.offline_banner.place_forget()
                self.feedback.config(text="Connection restored. Syncing...", foreground="green")
                self.sync_offline_entries()
                
            self.refresh_sms()
            self.refresh_entries()
        except Exception:
            self.is_online = False
            self.offline_banner.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.refresh_entries()  # to update display with offline queue
            
        self.after(3000, self.network_loop)
        
    def sync_offline_entries(self):
        if not self.offline_queue:
            return
            
        try:
            sms_rows = self.api.sms()
        except Exception:
            return
            
        remaining_queue = []
        for payload in self.offline_queue:
            matched_sms_id = None
            try:
                payload_time = datetime.strptime(payload["timestamp"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                payload_time = datetime.now()
                
            payload_credit = float(payload.get("credit", 0))
            payload_bank = payload.get("bank")
            
            for sms in sms_rows:
                if sms.get("status") != "new" or sms.get("channel") != payload_bank:
                    continue
                try:
                    sms_amount = float(sms.get("amount", 0))
                    if abs(sms_amount - payload_credit) > 0.01:
                        continue
                    if sms.get("received_at"):
                        sms_time = datetime.strptime(sms["received_at"], "%Y-%m-%d %H:%M:%S")
                        diff = abs((sms_time - payload_time).total_seconds())
                        if diff <= 300:
                            matched_sms_id = sms["id"]
                            break
                except ValueError:
                    pass
            
            payload["sms_payment_id"] = matched_sms_id
            try:
                server_entry = self.api.create_entry(payload)
                update_local_server_entry(payload["local_excel_id"], server_entry["id"], "active")
            except Exception as exc:
                if "reach server" in str(exc).lower() or "timeout" in str(exc).lower():
                    remaining_queue.append(payload)
                else:
                    print("Sync error (dropped entry):", exc)
                    
        self.offline_queue = remaining_queue

    def sms_prev_page(self):
        if self.sms_page > 0:
            self.sms_page -= 1
            self._render_sms_page()

    def sms_next_page(self):
        max_page = max(0, (len(self.sms_all) - 1) // self.SMS_PAGE_SIZE)
        if self.sms_page < max_page:
            self.sms_page += 1
            self._render_sms_page()

    def refresh_sms(self):
        try:
            rows = self.api.sms()
        except Exception as exc:
            self.feedback.config(text=str(exc), foreground="red")
            return
        
        # Float unconfirmed ('new') values to the top
        self.sms_all = sorted(rows, key=lambda r: r.get("status") != "new")
        
        # Keep the current page valid as new SMS arrive (don't yank back to page 1).
        max_page = max(0, (len(self.sms_all) - 1) // self.SMS_PAGE_SIZE)
        if self.sms_page > max_page:
            self.sms_page = max_page
        self._render_sms_page()

    def _render_sms_page(self):
        selected_id = str(self.selected_sms["id"]) if self.selected_sms else ""
        for item in self.sms_tree.get_children():
            self.sms_tree.delete(item)
        self.sms_rows = {}
        total = len(self.sms_all)
        max_page = max(0, (total - 1) // self.SMS_PAGE_SIZE) if total else 0
        start = self.sms_page * self.SMS_PAGE_SIZE
        page_rows = self.sms_all[start:start + self.SMS_PAGE_SIZE]
        for index, row in enumerate(page_rows):
            logged = ""
            if row["status"] == "logged":
                logged = row.get("logged_by") or ""
            elif row["status"] == "reversed":
                logged = "reversed by %s" % (row.get("logged_by") or "")
            self.sms_rows[str(row["id"])] = row
            tags = ["even" if index % 2 == 0 else "odd"]
            if row["status"] == "logged":
                tags.append("logged")
            elif row["status"] == "reversed":
                tags.append("reversed")
            has_receipt = "Open ↗" if receipt_url(row.get("channel"), row.get("body")) else ""
            self.sms_tree.insert("", "end", iid=str(row["id"]), values=(row["id"], row.get("received_at") or "", row["status"], row["channel"], format(row["amount"], ",.2f"), row.get("payer") or "", has_receipt, logged), tags=tuple(tags))
        shown_from = start + 1 if page_rows else 0
        shown_to = start + len(page_rows)
        self.sms_page_label.config(text="Page %d/%d  (%d-%d of %d)" % (
            self.sms_page + 1, max_page + 1, shown_from, shown_to, total))
        self.sms_prev_btn.config(state=("normal" if self.sms_page > 0 else "disabled"))
        self.sms_next_btn.config(state=("normal" if self.sms_page < max_page else "disabled"))
        if selected_id and selected_id in self.sms_tree.get_children():
            self.sms_tree.selection_set(selected_id)
            self.sms_tree.focus(selected_id)

    def refresh_entries(self):
        get_session_files()
        self.session_label.config(text="Session Folder: %s" % CURRENT_SESSION_DIRECTORY)
        selected_id = self.selected_entry_id
        for item in self.entry_tree.get_children():
            self.entry_tree.delete(item)
        # Blank slate per session: only this session's entries (its start/business
        # date) for this cashier — never a previous session's or cashier's rows.
        session_date = current_session_date()
        
        if self.is_online:
            try:
                rows = self.api.entries(date_from=session_date, date_to=session_date)
                self.server_entries_cache = rows
            except Exception:
                rows = getattr(self, "server_entries_cache", [])
        else:
            rows = getattr(self, "server_entries_cache", [])
            
        display_rows = list(rows)
        for off_payload in self.offline_queue:
            display_rows.append({
                "id": "offline-%s" % off_payload["local_excel_id"],
                "local_excel_id": off_payload["local_excel_id"],
                "timestamp": off_payload["timestamp"],
                "cashier": off_payload["cashier"],
                "bank": off_payload["bank"],
                "credit": off_payload["credit"],
                "sms_payment_id": off_payload.get("sms_payment_id") or "",
                "status": "offline"
            })
            
        self.server_entries = {str(r["id"]): r for r in rows}
        visible_index = 0
        total_credit = 0.0
        for r in display_rows:
            if r["cashier"] == self.cashier and (r.get("session_date") or session_date) == session_date:
                iid = str(r["id"])
                tags = ["even" if visible_index % 2 == 0 else "odd"]
                if r["status"] == "reversed":
                    tags.append("reversed")
                else:
                    try:
                        total_credit += float(r.get("credit", 0) or 0)
                    except ValueError:
                        pass
                    
                    try:
                        disp_credit = float(r.get("credit", 0) or 0)
                    except ValueError:
                        disp_credit = 0.0
                        
                self.entry_tree.insert("", "end", iid=iid, values=(r.get("local_excel_id") or "", r["id"], r["timestamp"], r["cashier"], r["bank"], format(disp_credit, ",.2f"), r.get("sms_payment_id") or "", r["status"]), tags=tuple(tags))
                visible_index += 1
                
        if hasattr(self, 'session_total_label'):
            self.session_total_label.config(text="Total: %s" % format(total_credit, ",.2f"))
        if selected_id and selected_id in self.entry_tree.get_children():
            self.entry_tree.selection_set(selected_id)
            self.entry_tree.focus(selected_id)

    def pick_sms(self, _event=None):
        selected = self.sms_tree.selection()
        if not selected:
            return
        row = self.sms_rows.get(selected[0])
        if not row:
            return
        if row["status"] == "logged":
            self.sms_tree.selection_remove(selected[0])
            if self.selected_sms and str(self.selected_sms["id"]) in self.sms_tree.get_children():
                self.sms_tree.selection_set(str(self.selected_sms["id"]))
                self.sms_tree.focus(str(self.selected_sms["id"]))
            self.feedback.config(text="This SMS is already logged by %s." % (row.get("logged_by") or "another cashier"), foreground="blue")
            return
        self.selected_sms = row
        self.select_bank(row["channel"])
        self.credit_var.set("%.2f" % row["amount"])
        if row["status"] == "reversed":
            self.feedback.config(text="This SMS was reversed and can be logged again after review.", foreground="blue")
        else:
            self.feedback.config(text="Selected SMS %s. Review and submit." % row["id"], foreground="green")

    def open_selected_receipt(self, _event=None):
        selected = self.sms_tree.selection()
        if not selected:
            self.feedback.config(text="Select a text first, then Open Receipt.", foreground="blue")
            return
        row = self.sms_rows.get(selected[0])
        if not row:
            return
        url = receipt_url(row.get("channel"), row.get("body"))
        if not url:
            self.feedback.config(text="No receipt link available for this text (%s)." % (row.get("channel") or "?"), foreground="blue")
            return
        try:
            webbrowser.open(url, new=2)
            self.feedback.config(text="Opened receipt for SMS %s." % row["id"], foreground="green")
        except Exception as exc:
            self.feedback.config(text="Could not open receipt: %s" % exc, foreground="red")

    def pick_entry(self, _event=None):
        selected = self.entry_tree.selection()
        self.selected_entry_id = selected[0] if selected else None

    def submit(self):
        bank = self.bank_var.get()
        credit = self.credit_var.get().strip()
        if not bank or not credit:
            messagebox.showwarning("Missing Data", "Choose a bank and enter amount.")
            return
        sms_id = self.selected_sms["id"] if self.selected_sms else None
        payload = {
            "sms_payment_id": sms_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_date": current_session_date(),  # business date (session start), survives midnight
            "cashier": self.cashier,
            "bank": bank,
            "credit": credit,
            "source_pc": socket.gethostname(),
        }
        local_entry = save_local_entry({
            "Timestamp": payload["timestamp"], "Cashier": self.cashier, "Bank": bank,
            "Credit": credit, "Status": "", "SmsID": sms_id or "",
        })
        payload["local_excel_id"] = local_entry["ID"]
        try:
            server_entry = self.api.create_entry(payload)
            update_local_server_entry(local_entry["ID"], server_entry["id"])
            self.feedback.config(text="Logged server entry %s and local backup %s." % (server_entry["id"], local_entry["ID"]), foreground="green")
        except Exception as exc:
            self.feedback.config(text="Saved local backup, server rejected: %s" % exc, foreground="red")
            self.offline_queue = load_offline_queue(self.cashier)
        self.selected_sms = None
        self.credit_var.set("")
        self.refresh_sms()
        self.refresh_entries()

    def reverse_selected(self):
        selected = self.entry_tree.selection()
        if not selected:
            return
        vals = self.entry_tree.item(selected[0], "values")
        server_id = selected[0]
        if not server_id:
            return
        if not messagebox.askyesno("Reverse Entry", "Reverse server entry %s?" % server_id):
            return
        try:
            self.api.reverse_entry(server_id, self.cashier, "cashier reversal")
        except Exception as exc:
            messagebox.showerror("Reverse Error", str(exc))
            return
        local_id = vals[0] if vals else ""
        if local_id:
            try:
                mark_local_deleted(local_id)
            except Exception as exc:
                messagebox.showwarning("Local Backup Warning", "Server entry was reversed, but the local Excel backup could not be updated:\n%s" % exc)
        self.feedback.config(text="Reversed entry %s." % server_id, foreground="blue")
        self.refresh_sms()
        self.refresh_entries()

    def end_session_with_confirmation(self):
        get_session_files()
        if not CURRENT_SESSION_DIRECTORY:
            messagebox.showinfo("Info", "No active session to end.")
            return
        if not messagebox.askyesno("Confirm", "Are you sure you want to end this session? A new folder will be created for the next entry."):
            return
        end_current_session()
        self.master.destroy()


class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Admission Deposits")
        self.geometry("1280x760")
        try:
            self.state("zoomed")  # open maximized on Windows
        except tk.TclError:
            pass
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Work around the Tk 8.6.9/8.6.10 regression (bundled with Python 3.8 on
        # Windows 7) where ttk.Treeview tag background/foreground are ignored:
        # drop the default ('!disabled','!selected') entries from the style map
        # so per-row tag colors are honored again. Harmless on newer Tk.
        def _fixed_map(option):
            return [e for e in style.map("Treeview", query_opt=option)
                    if e[:2] != ("!disabled", "!selected")]
        try:
            style.map(
                "Treeview",
                foreground=_fixed_map("foreground"),
                background=_fixed_map("background"),
            )
        except tk.TclError:
            pass
        self.cfg = load_config()
        self.api = Api(self.cfg)
        self.after(100, self.auto_connect)

    def clear(self):
        for widget in self.winfo_children():
            widget.destroy()

    def auto_connect(self):
        try:
            app_cfg = self.api.config()
        except Exception as exc:
            print("Offline mode:", exc)
            app_cfg = {"cashiers": CASHIERS, "banks": BANKS}
        self.open_main(self.api, app_cfg, ADMISSION_USER)

    def open_cashiers(self, api, app_cfg):
        self.clear()
        CashierFrame(self, api, app_cfg)

    def open_main(self, api, app_cfg, cashier):
        self.clear()
        MainFrame(self, api, app_cfg, cashier)


_SINGLE_INSTANCE_SOCK = None


def acquire_single_instance(port=61998):
    """Hold a fixed loopback port for the process lifetime. A second instance
    fails to bind it and knows one is already running. The OS releases the port
    when the process exits (even on a crash), so there is no stale-lock issue."""
    global _SINGLE_INSTANCE_SOCK
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    _SINGLE_INSTANCE_SOCK = sock
    return True


if __name__ == "__main__":
    if not acquire_single_instance():
        warn = tk.Tk()
        warn.withdraw()
        messagebox.showwarning("Admission Deposits", "Admission Deposits is already running on this computer.")
        warn.destroy()
        raise SystemExit(0)
    App().mainloop()
