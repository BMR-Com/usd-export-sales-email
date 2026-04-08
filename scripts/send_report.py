#!/usr/bin/env python3
"""
USDA ESR Weekly Email Report
Loads index.html, waits for data + charts, generates PDF via page.pdf(),
sends as email attachment via SMTP.

Required GitHub Secrets:
  SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS  EMAIL_FROM  EMAIL_TO
"""

import os, re, ssl, smtplib, json, sys
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ── Config ────────────────────────────────────────────────────────────────────
def require_env(name):
    val = os.environ.get(name, '').strip()
    if not val:
        print(f'✗ MISSING SECRET: {name} is not set in GitHub Secrets', flush=True)
        sys.exit(1)
    return val

SMTP_HOST   = require_env('SMTP_HOST')
SMTP_PORT   = int(require_env('SMTP_PORT'))
SMTP_USER   = require_env('SMTP_USER')
SMTP_PASS   = require_env('SMTP_PASS')
EMAIL_FROM  = require_env('EMAIL_FROM')
EMAIL_TO    = [r.strip() for r in require_env('EMAIL_TO').split(',') if r.strip()]
HTML_FILE   = os.environ.get('HTML_FILE', str(Path(__file__).parent.parent / 'index.html'))
REPORT_DATE = os.environ.get('REPORT_DATE', datetime.now().strftime('%Y-%m-%d'))

FROM_YEAR = '2020'
TO_YEAR   = '2026'

def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


def render_to_pdf():
    log(f'HTML file: {HTML_FILE}')
    if not Path(HTML_FILE).exists():
        raise FileNotFoundError(f'index.html not found at: {HTML_FILE}')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox',
                  '--disable-dev-shm-usage','--disable-gpu',
                  '--disable-web-security','--allow-file-access-from-files']
        )
        page = browser.new_page(viewport={'width':1440,'height':900})

        log('Loading page…')
        page.goto(f'file://{HTML_FILE}', wait_until='domcontentloaded')
        log('DOM loaded')

        # Set year range and commodity
        log(f'Setting commodity 1404, years {FROM_YEAR}–{TO_YEAR}…')
        page.evaluate(f"""() => {{
            const s = document.getElementById('selCommodity');
            if(s) s.value = '1404';
            const f = document.getElementById('selYearFrom');
            if(f) f.value = '{FROM_YEAR}';
            const t = document.getElementById('selYearTo');
            if(t) t.value = '{TO_YEAR}';
        }}""")

        # Click Load Data
        log('Clicking Load Data…')
        page.click('#btnLoad')

        # Wait for status bar to show success — up to 90s
        log('Waiting for USDA API data (up to 90s)…')
        try:
            page.wait_for_function(
                "() => (document.getElementById('statusBar')?.textContent||'').startsWith('✓')",
                timeout=90_000
            )
        except PwTimeout:
            status = page.text_content('#statusBar') or 'no status'
            log(f'Status bar content: "{status}"')
            raise RuntimeError(f'Data load timed out after 90s. Status: "{status}"')

        status_text = page.text_content('#statusBar')
        log(f'Status: {status_text}')

        # Wait 60s for projection model + all 8 charts to fully render
        log('Waiting 60s for all charts and tables to render…')
        page.wait_for_timeout(60_000)

        # Switch to print colors (colors only — no canvas resize)
        log('Switching charts to print colors…')
        page.evaluate("() => { if(typeof prepChartsForPrint==='function') prepChartsForPrint(); }")
        page.wait_for_timeout(2_000)

        # Ensure dashboard tab is active
        page.evaluate("() => { if(typeof switchTab==='function') switchTab('dashboard'); }")
        page.wait_for_timeout(500)

        # Generate PDF
        log('Generating PDF…')
        pdf_bytes = page.pdf(
            format='A4',
            landscape=True,
            margin={'top':'7mm','bottom':'7mm','left':'8mm','right':'8mm'},
            print_background=True,
        )
        log(f'PDF generated: {len(pdf_bytes):,} bytes ({len(pdf_bytes)//1024}KB)')
        browser.close()

    return pdf_bytes


def send_email(pdf_bytes):
    date_str = datetime.now().strftime('%b %d, %Y')
    subject  = f'USDA ESR Weekly Report — All Upland Cotton — {date_str}'
    filename = f'ESR_Report_Cotton_{REPORT_DATE}.pdf'

    log(f'Building email: "{subject}"')
    log(f'From: {EMAIL_FROM}')
    log(f'To:   {", ".join(EMAIL_TO)}')
    log(f'SMTP: {SMTP_HOST}:{SMTP_PORT}')

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)

    body = (f'USDA ESR Weekly Report — All Upland Cotton\n'
            f'Period: MY{FROM_YEAR}–MY{TO_YEAR}\n'
            f'Generated: {datetime.now().strftime("%A, %B %d, %Y %H:%M ET")}\n\n'
            f'The full PDF report is attached ({len(pdf_bytes)//1024}KB).\n\n'
            f'Pages:\n'
            f'  1-2: Seasonality charts (8 metrics, multi-year)\n'
            f'  3:   Market Intelligence + Year-End Projection\n'
            f'  4:   Country Summary (TW/LW + multi-year)\n'
            f'  5:   8-Week Trend + Percentile Ranges\n'
            f'  6:   Export Sales Summary\n\n'
            f'Source: USDA FAS | api.fas.usda.gov\n')
    msg.attach(MIMEText(body, 'plain'))

    pdf_part = MIMEBase('application', 'pdf')
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    pdf_part.add_header('Content-Disposition','attachment',filename=filename)
    msg.attach(pdf_part)

    try:
        if SMTP_PORT == 465:
            log('Connecting via SSL (port 465)…')
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
                log('Logging in…')
                srv.login(SMTP_USER, SMTP_PASS)
                log('Sending…')
                srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        else:
            log('Connecting via STARTTLS (port 587)…')
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
                srv.ehlo()
                srv.starttls(context=ssl.create_default_context())
                log('Logging in…')
                srv.login(SMTP_USER, SMTP_PASS)
                log('Sending…')
                srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        log(f'✓ Email sent successfully to: {", ".join(EMAIL_TO)}')
    except smtplib.SMTPAuthenticationError as e:
        log(f'✗ SMTP Authentication failed: {e}')
        log('Check SMTP_USER and SMTP_PASS secrets. For Gmail, use an App Password.')
        raise
    except smtplib.SMTPException as e:
        log(f'✗ SMTP error: {e}')
        raise
    except Exception as e:
        log(f'✗ Unexpected error sending email: {type(e).__name__}: {e}')
        raise


def main():
    log('='*50)
    log('USDA ESR Weekly Report Generator')
    log('='*50)
    log(f'Date:      {REPORT_DATE}')
    log(f'Years:     MY{FROM_YEAR}–MY{TO_YEAR}')
    log(f'Commodity: 1404 (All Upland Cotton)')
    log(f'HTML:      {HTML_FILE}')
    log(f'From:      {EMAIL_FROM}')
    log(f'To:        {", ".join(EMAIL_TO)}')
    log(f'SMTP:      {SMTP_HOST}:{SMTP_PORT}')
    log('='*50)

    if not EMAIL_TO:
        log('✗ EMAIL_TO is empty — add recipient addresses to GitHub Secrets')
        sys.exit(1)

    pdf = render_to_pdf()
    send_email(pdf)
    log('='*50)
    log('DONE')
    log('='*50)

if __name__ == '__main__':
    main()
