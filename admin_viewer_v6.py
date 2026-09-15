import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import messagebox, ttk


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "admin_config.json")
DEFAULT_CONFIG = {"server_url": "http://127.0.0.1:8765"}


def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("server_url", DEFAULT_CONFIG["server_url"])
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)


class AdminApi:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, path, params=None):
        headers = {}
        if self.cfg.get("admin_token"):
            headers["X-Cred-Token"] = self.cfg.get("admin_token", "")
        url = self.cfg["server_url"].rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                err = str(exc)
            raise RuntimeError(err)
        except URLError as exc:
            raise RuntimeError("Cannot reach server: %s" % exc.reason)


class Setup(ttk.Frame):
    def __init__(self, master):
        ttk.Frame.__init__(self, master, padding=16)
        self.cfg = load_config()
        self.url = tk.StringVar(value=self.cfg["server_url"])
        self.pack(fill="both", expand=True)
        ttk.Label(self, text="Admin Viewer", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(self, text="Server URL").grid(row=1, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.url, width=48).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Open", command=self.open).grid(row=2, column=1, sticky="e", pady=8)
        self.columnconfigure(1, weight=1)

    def open(self):
        self.cfg["server_url"] = self.url.get().strip()
        save_config(self.cfg)
        self.master.open_main(AdminApi(self.cfg))


class Main(ttk.Frame):
    def __init__(self, master, api):
        ttk.Frame.__init__(self, master, padding=8)
        self.api = api
        today = datetime.now().strftime("%Y-%m-%d")
        self.date_from = tk.StringVar(value=today)
        self.date_to = tk.StringVar(value=today)
        self.pack(fill="both", expand=True)
        filters = ttk.Frame(self)
        filters.pack(fill="x")
        ttk.Label(filters, text="From").pack(side="left")
        ttk.Entry(filters, textvariable=self.date_from, width=12).pack(side="left", padx=(4, 12))
        ttk.Label(filters, text="To").pack(side="left")
        ttk.Entry(filters, textvariable=self.date_to, width=12).pack(side="left", padx=(4, 12))
        ttk.Button(filters, text="Refresh", command=self.refresh).pack(side="right")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, pady=5)
        self.matched = self.make_tree("Matched XML to Credit Entries", ("xml", "cashier", "amount", "entry", "bank", "score", "reason", "source"))
        self.unmatched_xml = self.make_tree("Unmatched XMLs", ("xml", "cashier", "amount", "reference", "source", "date"))
        self.unmatched_entries = self.make_tree("Unmatched Credit Entries", ("entry", "cashier", "amount", "bank", "sms", "time", "source"))
        self.refresh()

    def make_tree(self, title, cols):
        frame = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(frame, text=title)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col.title())
            tree.column(col, width=140)
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        return tree

    def clear(self, tree):
        for item in tree.get_children():
            tree.delete(item)

    def refresh(self):
        try:
            data = self.api.get("/api/admin/reconciliation", {
                "date_from": self.date_from.get().strip(),
                "date_to": self.date_to.get().strip(),
            })
        except Exception as exc:
            messagebox.showerror("Admin Error", str(exc))
            return
        for tree in (self.matched, self.unmatched_xml, self.unmatched_entries):
            self.clear(tree)
        for m in data["matches"]:
            x = m["xml"]
            e = m["entry"]
            self.matched.insert("", "end", values=(
                x["filename"], x.get("cashier_name") or "", format(x["total_amount"], ",.2f"),
                e["id"], e["bank"], m["score"], m.get("reason") or "", x["source_ip"],
            ))
        for x in data["unmatched_xml"]:
            self.unmatched_xml.insert("", "end", values=(
                x["filename"], x.get("cashier_name") or "", format(x["total_amount"], ",.2f"),
                x.get("reference_number") or "", x["source_ip"], x.get("invoice_date") or "",
            ))
        for e in data["unmatched_entries"]:
            self.unmatched_entries.insert("", "end", values=(
                e["id"], e["cashier"], format(e["credit"], ",.2f"), e["bank"],
                e.get("sms_payment_id") or "", e["timestamp"], e["source_pc"],
            ))


class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Cred Entry v6 Admin")
        self.geometry("1180x720")
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        self.show_setup()

    def clear(self):
        for widget in self.winfo_children():
            widget.destroy()

    def show_setup(self):
        self.clear()
        Setup(self)

    def open_main(self, api):
        self.clear()
        Main(self, api)


if __name__ == "__main__":
    App().mainloop()
