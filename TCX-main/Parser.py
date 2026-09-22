#!/usr/bin/env python3
"""Parser.py

Parses XML files under a server docs/<date>/<source>/xmls folder,
updates processed XMLs with `<fs>` and `<invoice_date>` where applicable,
and inserts rows into a SQLite database `xml.db`.
"""
import argparse
import os
import re
import sqlite3
from xml.etree import ElementTree as ET


ID_RE = re.compile(r'([A-Z]{4})-(\d{5})_(\d{6})_(\d{4})')


def extract_fields_from_xml_text(text):
    """Extract canonical fields from XML-ish text.

    Returns a dict with keys: fs, invoice_date, cashier, amount, reference, source
    Tries XML parsing first, falls back to regex. Accepts common tag name variants.
    """
    # helper to search both ElementTree and regex for variants
    def find_any(root, txt, variants):
        # try element tree search
        if root is not None:
            for v in variants:
                # allow both exact tag and underscore/case variants
                el = root.find('.//' + v)
                if el is not None and el.text and el.text.strip():
                    return el.text.strip()
        # fallback regex (handle optional namespaces, underscores, dashes)
        for v in variants:
            pattern = rf'<\s*{v}\s*>\s*(.*?)\s*<\s*/\s*{v}\s*>'
            m = re.search(pattern, txt, re.IGNORECASE | re.DOTALL)
            if m:
                return m.group(1).strip()
            # try variants with underscores/different casing
            alt = v.replace('_', '[_\-]?')
            m2 = re.search(rf'<\s*{alt}\s*>\s*(.*?)\s*<\s*/\s*{alt}\s*>', txt, re.IGNORECASE | re.DOTALL)
            if m2:
                return m2.group(1).strip()
        return None

    try:
        root = ET.fromstring(text)
    except Exception:
        root = None

    fs = find_any(root, text, ['FS_Number', 'FSNumber', 'fsnumber', 'fs', 'FS'])
    invoice_date = find_any(root, text, ['Invoice_Date', 'InvoiceDate', 'invoice_date', 'invoiceDate', 'date', 'Invoice_DateTime', 'Invoice_DateTime'])
    cashier = find_any(root, text, ['Username', 'Cashier', 'Cashier_Name', 'cashier', 'username'])
    reference = find_any(root, text, ['Reference_Number', 'ReferenceNumber', 'Reference', 'reference', 'ref', 'POS_Reference_Number'])
    source = find_any(root, text, ['Machine_Code', 'MachineCode', 'Plant_Code', 'PlantCode', 'Store', 'Source'])

    # amount: try common total tags, else sum line items if available
    amount = find_any(root, text, ['Total', 'Amount', 'TotalAmount', 'Invoice_Total', 'GrandTotal'])
    if not amount and root is not None:
        # try summing Item_Unit_Price * Item_Quantity for each Line_Items/Item
        prices = []
        qtys = []
        for item in root.findall('.//Line_Items') + root.findall('.//Line_Item'):
            # look for unit price and quantity
            up = item.find('.//Item_Unit_Price') or item.find('.//UnitPrice') or item.find('.//Price')
            q = item.find('.//Item_Quantity') or item.find('.//Quantity')
            try:
                p = float(up.text.strip()) if up is not None and up.text and up.text.strip() else 0.0
            except Exception:
                p = 0.0
            try:
                qq = float(q.text.strip()) if q is not None and q.text and q.text.strip() else 0.0
            except Exception:
                qq = 0.0
            prices.append(p * qq)
        if prices:
            amount = str(sum(prices))

    return {
        'fs': fs,
        'invoice_date': invoice_date,
        'cashier': cashier,
        'amount': amount,
        'reference': reference,
        'source': source,
    }


def update_processed_xml(processed_path, fs_value, invoice_date):
    """Update processed XML file by inserting/updating FS and invoice_date tags.

    Accepts various tag name variants and falls back to text replacement when parsing fails.
    """
    try:
        tree = ET.parse(processed_path)
        root = tree.getroot()
    except Exception:
        # cannot parse; append via text replacement with common variants
        with open(processed_path, 'r+', encoding='utf-8', errors='ignore') as f:
            txt = f.read()
            if fs_value and not re.search(r'<\s*(FS_Number|FSNumber|fsnumber|fs)\s*>', txt, re.IGNORECASE):
                txt += f'\n<FS_Number>{fs_value}</FS_Number>\n'
            if invoice_date:
                if re.search(r'<\s*(Invoice_Date|InvoiceDate|invoice_date|invoiceDate|Invoice_DateTime)\s*>', txt, re.IGNORECASE):
                    txt = re.sub(r'<\s*(Invoice_Date|InvoiceDate|invoice_date|invoiceDate|Invoice_DateTime)\s*>.*?<\s*/\s*\1\s*>', f'<Invoice_Date>{invoice_date}</Invoice_Date>', txt, flags=re.IGNORECASE|re.DOTALL)
                else:
                    txt += f'\n<Invoice_Date>{invoice_date}</Invoice_Date>\n'
            f.seek(0)
            f.write(txt)
            f.truncate()
        return True

    # prefer FS_Number-like tags
    fs_tags = ['FS_Number', 'FSNumber', 'fsnumber', 'fs', 'FS']
    inv_tags = ['Invoice_Date', 'InvoiceDate', 'invoice_date', 'invoiceDate', 'Invoice_DateTime']

    # set or update fs
    changed = False
    for t in fs_tags:
        el = root.find('.//' + t)
        if el is not None:
            if fs_value:
                el.text = str(fs_value)
                changed = True
            break
    else:
        if fs_value:
            new = ET.Element('FS_Number')
            new.text = str(fs_value)
            root.append(new)
            changed = True

    # set or update invoice date
    for t in inv_tags:
        el = root.find('.//' + t)
        if el is not None:
            if invoice_date:
                el.text = str(invoice_date)
                changed = True
            break
    else:
        if invoice_date:
            newi = ET.Element('Invoice_Date')
            newi.text = str(invoice_date)
            root.append(newi)
            changed = True

    if changed:
        tree.write(processed_path, encoding='utf-8', xml_declaration=True)
    return True


def ensure_db(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS xml_entries (
        id INTEGER PRIMARY KEY,
        filename TEXT,
        cashier TEXT,
        amount TEXT,
        reference TEXT,
        source TEXT,
        date TEXT,
        fsnumber TEXT,
        rawxml TEXT
    )
    ''')
    conn.commit()
    return conn


def insert_row(conn, filename, fields, rawxml):
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO xml_entries (filename, cashier, amount, reference, source, date, fsnumber, rawxml)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (filename, fields.get('cashier'), fields.get('amount'), fields.get('reference'), fields.get('source'), fields.get('invoice_date'), fields.get('fs'), rawxml))
    conn.commit()


def pair_and_process(input_dir, db_path='xml.db'):
    files = [f for f in os.listdir(input_dir) if f.lower().endswith('.xml')]
    # index files by their 5-digit group if available
    by_five = {}
    for fn in files:
        m = ID_RE.search(fn)
        if m:
            five = m.group(2)
            by_five.setdefault(five, []).append(fn)

    conn = ensure_db(db_path)
    processed = 0
    for five, fns in by_five.items():
        # try to find response and processed pair: heuristics - one file containing fs, one without
        paths = [os.path.join(input_dir, fn) for fn in fns]
        contents = {}
        for p in paths:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                contents[p] = f.read()

        # pick response as the one that contains a tag 'fs' or 'time_created'
        response_path = None
        processed_path = None
        for p, txt in contents.items():
            if re.search(r'<fs>|<fsnumber>|time_created', txt, re.IGNORECASE):
                response_path = p
            else:
                processed_path = p
        # fallback assignments
        if not response_path and paths:
            response_path = paths[0]
            processed_path = paths[1] if len(paths) > 1 else None

        # extract fields from response and processed and merge
        resp_fields = extract_fields_from_xml_text(contents[response_path]) if response_path else {}
        proc_fields = extract_fields_from_xml_text(contents[processed_path]) if processed_path else {}
        # merge giving priority to non-empty values from response, but keep processed invoice_date if present
        merged = proc_fields.copy()
        for k, v in (resp_fields or {}).items():
            if v:
                merged[k] = v

        raw = contents[response_path] if response_path else (contents.get(processed_path) or '')

        # update processed xml with FS and invoice_date from merged fields
        if processed_path:
            update_processed_xml(processed_path, merged.get('fs'), merged.get('invoice_date'))
            insert_row(conn, os.path.basename(processed_path), merged, raw)
        else:
            insert_row(conn, os.path.basename(response_path), merged, raw)
            processed += 1

    conn.close()
    return processed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input_dir', help='Server directory docs/<date>/<source>/xmls')
    ap.add_argument('--db', default='xml.db', help='SQLite DB path (default: xml.db)')
    args = ap.parse_args()

    count = pair_and_process(args.input_dir, args.db)
    print(f'Processed and added {count} entries to database {args.db}')


if __name__ == '__main__':
    main()
