#!/usr/bin/env python3
"""Sender.py

Usage:
  python Sender.py <input_xmls_dir> <server_root_dir> [--source <name>]

Copies all files from the input directory into the server-root under
docs/<date>/<source>/xmls to simulate daily transfer.
"""
import argparse
import os
import shutil
import socket


def detect_date_from_path(path):
    # expect path like .../docs/YYYY-MM-DD/xmls
    parts = os.path.normpath(path).split(os.path.sep)
    if 'docs' in parts:
        i = parts.index('docs')
        if len(parts) > i + 1:
            return parts[i + 1]
    return None


def send_xmls(input_dir, server_root, source=None):
    date = detect_date_from_path(input_dir) or os.path.basename(os.path.dirname(input_dir))
    if not source:
        source = socket.gethostname()
    dest_dir = os.path.join(server_root, 'docs', date, source, 'xmls')
    os.makedirs(dest_dir, exist_ok=True)

    files = [f for f in os.listdir(input_dir) if f.lower().endswith('.xml')]
    sent = 0
    for fn in files:
        src = os.path.join(input_dir, fn)
        dst = os.path.join(dest_dir, fn)
        shutil.copy2(src, dst)
        sent += 1
    return dest_dir, sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input_dir', help='Directory with xmls (docs/<date>/xmls)')
    ap.add_argument('server_root', help='Root path representing server storage')
    ap.add_argument('--source', help='Optional source name to use on server')
    args = ap.parse_args()

    dest, sent = send_xmls(args.input_dir, args.server_root, args.source)
    print(f'Sent {sent} files to server location: {dest}')


if __name__ == '__main__':
    main()
