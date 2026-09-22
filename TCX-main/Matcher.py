#!/usr/bin/env python3
"""Matcher.py

Usage:
  python Matcher.py <responses_dir> <processed_dir> [--out docs]

Copies matched XML pairs (from previous day) into docs/<YYYY-MM-DD>/xmls
"""
import argparse
import os
import re
import shutil
from datetime import datetime, timedelta


ID_RE = re.compile(r'([A-Z]{4})-(\d{5})_(\d{6})_(\d{4})')


def extract_identifier_from_text(text):
    m = ID_RE.search(text)
    if not m:
        return None
    return {'full': m.group(0), 'five': m.group(2)}


def file_mtime_date_matches(path, target_date):
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        return mtime.date() == target_date
    except Exception:
        return False


def find_id_in_file(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            txt = f.read()
        return extract_identifier_from_text(txt)
    except Exception:
        return None


def match_and_copy(responses_dir, processed_dir, out_base='docs'):
    today = datetime.now().date()
    target_date = today - timedelta(days=1)
    out_dir = os.path.join(out_base, target_date.isoformat(), 'xmls')
    os.makedirs(out_dir, exist_ok=True)

    responses = [os.path.join(responses_dir, f) for f in os.listdir(responses_dir) if f.lower().endswith('.xml')]
    processed = [os.path.join(processed_dir, f) for f in os.listdir(processed_dir) if f.lower().endswith('.xml')]

    responses = [p for p in responses if file_mtime_date_matches(p, target_date)]
    processed = [p for p in processed if file_mtime_date_matches(p, target_date)]

    processed_index = {}
    for p in processed:
        pid = find_id_in_file(p)
        if pid:
            processed_index.setdefault(pid['five'], []).append(p)

    copied_pairs = 0
    for r in responses:
        rid = find_id_in_file(r)
        if not rid:
            continue
        key = rid['five']
        matches = processed_index.get(key, [])
        if matches:
            # copy response and matched processed files
            shutil.copy2(r, os.path.join(out_dir, os.path.basename(r)))
            for ppath in matches:
                dest = os.path.join(out_dir, os.path.basename(ppath))
                if not os.path.exists(dest):
                    shutil.copy2(ppath, dest)
            copied_pairs += 1
    return out_dir, copied_pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('responses', help='Directory containing response XMLs')
    ap.add_argument('processed', help='Directory containing processed XMLs')
    ap.add_argument('--out', default='docs', help='Output base directory (default: docs)')
    args = ap.parse_args()

    out_dir, pairs = match_and_copy(args.responses, args.processed, args.out)
    print(f'Copied {pairs} matched response(s) and their processed partner(s) to: {out_dir}')


if __name__ == '__main__':
    main()
