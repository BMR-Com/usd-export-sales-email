#!/usr/bin/env python3
"""
build_options_data.py
=====================
Copies data/cotton_options_history.csv to
cotton_options/cotton_options_data.csv

The options page loads this via fetch('cotton_options_data.csv')
which works on GitHub Pages and any HTTP server.

Run by GitHub Actions weekly, or manually:
  python scripts/build_options_data.py
"""
import sys, shutil
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).parent.parent
CSV_SRC   = REPO_ROOT / 'data' / 'cotton_options_history.csv'
CSV_DEST  = REPO_ROOT / 'cotton_options' / 'cotton_options_data.csv'

def log(msg):
    print(msg, flush=True)

def main():
    if not CSV_SRC.exists():
        log(f'ERROR: {CSV_SRC} not found')
        log('Upload cotton_options_history.csv to the data/ folder in the repo.')
        sys.exit(1)

    rows = CSV_SRC.read_text(encoding='utf-8-sig').splitlines()
    log(f'Source CSV: {len(rows):,} rows  ({CSV_SRC.stat().st_size // 1024}KB)')

    CSV_DEST.parent.mkdir(exist_ok=True)
    shutil.copy2(CSV_SRC, CSV_DEST)
    log(f'Copied to:  {CSV_DEST} ({CSV_DEST.stat().st_size // 1024}KB)')
    log(f'Header:     {rows[0][:80] if rows else "empty"}')
    log(f'First data: {rows[1][:80] if len(rows) > 1 else "N/A"}')
    log(f'Last row:   {rows[-1][:80] if rows else "N/A"}')

if __name__ == '__main__':
    main()
