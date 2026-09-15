import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime


TARGET_SMS_SENDERS = ["127", "CBE", "223", "Awash Bank", "BOA"]
BANKS = sorted([
    "Abay", "Amhara", "Awash", "Bank of Abyssinia", "Bunna", "CBE",
    "Dashen", "Enat", "Hibret", "Lion", "Nib", "Telebirr", "Wegagen", "Zemen",
])
CASHIERS = sorted(["Adanu", "Bereket", "Ejigayehu", "Emush", "Hewan", "Meaza", "Misrak", "Tigist", "Yemisrach", "Zenebu"])


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def filetime_to_datetime(ft):
    epoch_seconds = (int(ft) - 116444736000000000) / 10000000.0
    return datetime.fromtimestamp(epoch_seconds)


def unix_ms_to_datetime(value):
    return datetime.fromtimestamp(int(value) / 1000.0)


def amount_to_float(value):
    if value in (None, ""):
        return None
    return float(str(value).replace(",", "").strip())


def normalize_cashier(name):
    return " ".join(str(name or "").strip().lower().split())


def parse_datetime_text(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if "T" in raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def parse_sms_payment(sender, body, timestamp_value=None, timestamp_kind="filetime"):
    amount = None
    payer = None
    channel = None
    actual_dt = None
    time_source = "Sync Time"

    m_time1 = re.search(r"on\s+(\d{2}/\d{2}/\d{4}(?:\s+at)?\s+\d{2}:\d{2}:\d{2})", body, re.IGNORECASE)
    if m_time1:
        clean = m_time1.group(1).replace(" at ", " ").strip()
        actual_dt = parse_datetime_text(clean)
        if actual_dt:
            time_source = "Exact"

    if not actual_dt:
        m_time2 = re.search(r"on\s*:?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body, re.IGNORECASE)
        if m_time2:
            actual_dt = parse_datetime_text(m_time2.group(1).strip())
            if actual_dt:
                time_source = "Exact"

    if not actual_dt and timestamp_value:
        actual_dt = unix_ms_to_datetime(timestamp_value) if timestamp_kind == "unix_ms" else filetime_to_datetime(timestamp_value)

    if not actual_dt:
        actual_dt = datetime.now()

    if sender == "127":
        channel = "Telebirr"
        m = re.search(r"received ETB\s*([\d,]+(?:\.\d{1,2})?)\s*from\s*(.*?)\s*on\s*(\d{2}/\d{2}/\d{4})", body, re.IGNORECASE)
        if m:
            amount = amount_to_float(m.group(1))
            payer = m.group(2).strip()

    elif sender in ("CBE", "223"):
        channel = "CBE"
        m2 = re.search(r"received ETB\s*([\d,]+(?:\.\d{1,2})?)\s*from account .*?\((.*?)\)", body, re.IGNORECASE)
        if m2:
            amount = amount_to_float(m2.group(1))
            payer = m2.group(2).strip()
        else:
            m_cb = re.search(r"credited by\s+(.*?)\s+with\s+(?:ETB)?\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
            if m_cb:
                payer = m_cb.group(1).strip()
                amount = amount_to_float(m_cb.group(2))
            else:
                m3 = re.search(r"(?:has been )?credited with\s*(?:ETB)?\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
                if m3:
                    amount = amount_to_float(m3.group(1))
                    m_p = re.search(r"(?:has been )?credited with.*?from\s+([^\.]+?)(?:\s+on|\.|$)", body, re.IGNORECASE)
                    payer = m_p.group(1).strip() if m_p else "CBE Customer"

    elif sender == "Awash Bank":
        channel = "Awash"
        m_aw1 = re.search(r"ETB\s*([\d,]+(?:\.\d{1,2})?)\s*has been credited to your account from\s+(.*?)\s+on\s*:", body, re.IGNORECASE)
        if m_aw1:
            amount = amount_to_float(m_aw1.group(1))
            payer = m_aw1.group(2).strip()
        else:
            m_aw2 = re.search(r"has been Credited with ETB\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
            if m_aw2:
                amount = amount_to_float(m_aw2.group(1))
                m_p = re.search(r"by\s+(.*?)\s+(?:via|\.)", body, re.IGNORECASE)
                payer = m_p.group(1).strip() if m_p else "Awash Transfer"

    elif sender == "BOA":
        channel = "Bank of Abyssinia"
        m_boa = re.search(r"Dear\s+[A-Za-z]+\s+([\d,]+(?:\.\d{1,2})?),", body, re.IGNORECASE)
        if m_boa:
            amount = amount_to_float(m_boa.group(1))
            m_trx = re.search(r"Receipt:\s*https?://cs\.bankofabyssinia\.com/slip/\?trx=([^\r\n]+)", body, re.IGNORECASE)
            payer = m_trx.group(1).strip() if m_trx else "BoA Customer"

    if amount is None or not channel:
        return None

    return {
        "sender": sender,
        "channel": channel,
        "amount": amount,
        "payer": payer or "",
        "received_at": actual_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "time_source": time_source,
        "body": body,
        "display_text": "[%s] ETB %s from %s at %s" % (channel, format(amount, ",.2f"), payer or "Unknown", actual_dt.strftime("%H:%M:%S")),
    }


def safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "upload.xml"))
    return cleaned.strip("._") or "upload.xml"


def parse_xml_document(xml_bytes):
    digest = hashlib.sha256(xml_bytes).hexdigest()
    root = ET.fromstring(xml_bytes)

    def first_text(*names):
        wanted = set(names)
        for elem in root.iter():
            tag = elem.tag.split("}", 1)[-1]
            if tag in wanted and elem.text is not None:
                text = elem.text.strip()
                if text:
                    return text
        return ""

    cashier = first_text("Username", "Cashier", "CashierName", "CashierID")
    reference = first_text("Reference_Number", "ReceiptNumber", "InvoiceNumber", "Reference")
    payment_type = first_text("Payment_Type", "PaymentMethod")
    invoice_type = first_text("Invoice_Type")
    customer = first_text("Customer_Name", "CustomerName")
    raw_date = first_text("Invoice_Date", "Date")
    raw_time = first_text("Time")
    if raw_time and raw_date and "T" not in raw_date and " " not in raw_date:
        raw_date = raw_date + " " + raw_time
    invoice_dt = parse_datetime_text(raw_date)

    explicit_total = first_text("GrandTotal", "AmountPaid", "Total", "Invoice_Total", "NetAmount")
    total = amount_to_float(explicit_total) if explicit_total else None
    if total is None:
        total = 0.0
        for item in root.iter():
            tag = item.tag.split("}", 1)[-1]
            if tag not in ("Line_Items", "Item"):
                continue
            qty = None
            price = None
            disc = 0.0
            for child in list(item):
                ctag = child.tag.split("}", 1)[-1]
                text = (child.text or "").strip()
                if ctag in ("Item_Quantity", "Quantity"):
                    qty = amount_to_float(text) or 0.0
                elif ctag in ("Item_Unit_Price", "UnitPrice"):
                    price = amount_to_float(text) or 0.0
                elif ctag in ("Item_DiscOrAdd_Amount", "Discount"):
                    disc = amount_to_float(text) or 0.0
                elif ctag == "TotalPrice" and total == 0.0:
                    pass
            if qty is not None and price is not None:
                total += (qty * price) + disc
        if total == 0.0:
            item_totals = []
            for elem in root.iter():
                if elem.tag.split("}", 1)[-1] == "TotalPrice" and elem.text:
                    item_totals.append(amount_to_float(elem.text) or 0.0)
            if item_totals:
                total = sum(item_totals)

    return {
        "sha256": digest,
        "cashier_name": cashier,
        "reference_number": reference,
        "invoice_date": invoice_dt.strftime("%Y-%m-%d %H:%M:%S") if invoice_dt else "",
        "invoice_type": invoice_type,
        "payment_type": payment_type,
        "customer_name": customer,
        "total_amount": float(total or 0.0),
        "root_tag": root.tag.split("}", 1)[-1],
    }
