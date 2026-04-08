#!/usr/bin/env python3
"""
send_options_report.py
======================
Loads cotton_options/index.html in headless Chromium,
waits for cotton_options_data.js to auto-load,
generates PDF via page.pdf(), sends via SMTP.

Reuses the same GitHub Secrets as send_report.py (ESR):
  SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS  EMAIL_FROM  EMAIL_TO
"""
import os, sys, ssl, smtplib
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

def require_env(name):
    val = os.environ.get(name, '').strip()
    if not val:
        print(f'✗ MISSING SECRET: {name}', flush=True)
        sys.exit(1)
    return val

SMTP_HOST   = require_env('SMTP_HOST')
SMTP_PORT   = int(require_env('SMTP_PORT'))
SMTP_USER   = require_env('SMTP_USER')
SMTP_PASS   = require_env('SMTP_PASS')
EMAIL_FROM  = require_env('EMAIL_FROM')
EMAIL_TO    = [r.strip() for r in require_env('EMAIL_TO').split(',') if r.strip()]
HTML_FILE   = os.environ.get('HTML_FILE',
              str(Path(__file__).parent.parent / 'cotton_options' / 'index.html'))
REPORT_DATE = os.environ.get('REPORT_DATE', datetime.now().strftime('%Y-%m-%d'))

def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)

def render_to_pdf():
    log(f'HTML file: {HTML_FILE}')
    if not Path(HTML_FILE).exists():
        raise FileNotFoundError(f'Options page not found: {HTML_FILE}')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox',
                  '--disable-dev-shm-usage','--disable-gpu',
                  '--disable-web-security','--allow-file-access-from-files']
        )
        page = browser.new_page(viewport={'width':1440,'height':900})

        log('Loading page...')
        page.goto(f'file://{HTML_FILE}', wait_until='domcontentloaded')
        page.wait_for_timeout(2000)

        # Check if data auto-loaded from cotton_options_data.js
        data_loaded = page.evaluate("() => typeof allData !== 'undefined' && allData.length > 0")
        log(f'Data auto-loaded: {data_loaded}')

        if not data_loaded:
            log('No auto-load data — checking if cotton_options_data.js exists...')
            js_file = Path(HTML_FILE).parent / 'cotton_options_data.js'
            if js_file.exists():
                log(f'cotton_options_data.js found ({js_file.stat().st_size//1024}KB) — retrying...')
                page.wait_for_timeout(3000)
                data_loaded = page.evaluate("() => typeof allData !== 'undefined' && allData.length > 0")

            if not data_loaded:
                raise RuntimeError(
                    'No vol data loaded. '
                    'Ensure data/cotton_options_history.csv is in the repo '
                    'and build_options_data.py has been run to generate cotton_options_data.js'
                )

        record_count = page.evaluate("() => allData.length")
        latest_date  = page.evaluate("() => allData[allData.length-1].date")
        log(f'Records: {record_count:,} · Latest: {latest_date}')

        # Wait for charts to render
        log('Waiting for charts to render...')
        page.wait_for_timeout(5000)

        # Switch to print-friendly colors
        log('Preparing for print...')
        page.evaluate("() => { if(typeof prepForPrint === 'function') prepForPrint(); }")
        page.wait_for_timeout(1000)

        # Generate PDF
        log('Generating PDF...')
        pdf_bytes = page.pdf(
            format='A4',
            landscape=True,
            margin={'top':'6mm','bottom':'6mm','left':'7mm','right':'7mm'},
            print_background=True,
        )
        log(f'PDF: {len(pdf_bytes):,} bytes ({len(pdf_bytes)//1024}KB)')
        browser.close()

    return pdf_bytes, latest_date

def send_email(pdf_bytes, latest_date):
    date_str = datetime.now().strftime('%b %d, %Y')
    subject  = f'Cotton Options Analytics — Implied Vol Report — {date_str}'
    filename = f'Cotton_Options_Analytics_{REPORT_DATE}.pdf'

    log(f'Sending: "{subject}"')
    log(f'To: {", ".join(EMAIL_TO)}')

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)

    body = (
        f'Cotton Options Analytics — Weekly Implied Volatility Report\n'
        f'Generated: {datetime.now().strftime("%A, %B %d, %Y")}\n'
        f'Latest data: {latest_date}\n\n'
        f'Report includes:\n'
        f'  • Seasonality charts — ATM Vol, 25D RR, Call Skew, Put Skew for all tenors\n'
        f'  • Term structure differentials — 1M spreads vs 2M/3M/6M/1Y\n'
        f'  • Current level summary — percentile rankings vs 5/10/20-year history\n'
        f'  • Rule-based trade ideas\n\n'
        f'Source: BVOL — Cotton implied volatility\n'
    )
    msg.attach(MIMEText(body, 'plain'))

    pdf_part = MIMEBase('application', 'pdf')
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    pdf_part.add_header('Content-Disposition', 'attachment', filename=filename)
    msg.attach(pdf_part)

    if SMTP_PORT == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls(context=ssl.create_default_context())
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    log(f'✓ Email sent to: {", ".join(EMAIL_TO)}')

def main():
    log('=== Cotton Options Analytics Report ===')
    log(f'Date: {REPORT_DATE} | SMTP: {SMTP_HOST}:{SMTP_PORT}')
    pdf_bytes, latest_date = render_to_pdf()
    send_email(pdf_bytes, latest_date)
    log('=== Done ===')

if __name__ == '__main__':
    main()
