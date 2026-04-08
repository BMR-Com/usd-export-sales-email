#!/usr/bin/env python3
"""
USDA ESR Weekly Email Report
=============================
Loads index.html in headless Chromium, selects commodity 1404 / All Countries,
waits for USDA API data and all 8 charts to fully render, resizes canvases to
print resolution, then uses page.pdf() to produce an exact PDF matching what
window.print() would generate. Sent as email attachment via SMTP.

GitHub Secrets required:
  SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS  EMAIL_FROM  EMAIL_TO
"""

import os, re, ssl, smtplib, json
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ── Config ────────────────────────────────────────────────────────────────────
SMTP_HOST    = os.environ['SMTP_HOST']
SMTP_PORT    = int(os.environ['SMTP_PORT'])
SMTP_USER    = os.environ['SMTP_USER']
SMTP_PASS    = os.environ['SMTP_PASS']
EMAIL_FROM   = os.environ['EMAIL_FROM']
EMAIL_TO     = [r.strip() for r in os.environ['EMAIL_TO'].split(',') if r.strip()]
HTML_FILE    = os.environ.get('HTML_FILE',
                  str(Path(__file__).parent.parent / 'index.html'))
REPORT_DATE  = os.environ.get('REPORT_DATE', datetime.now().strftime('%Y-%m-%d'))

DEFAULT_COMMODITY = '1404'
FROM_YEAR         = '2020'   # From year for chart range
TO_YEAR           = '2026'   # To year (most recent = primary)

def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


def render_to_pdf():
    log(f'Loading {HTML_FILE}')
    if not Path(HTML_FILE).exists():
        raise FileNotFoundError(f'index.html not found: {HTML_FILE}')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox',
                '--disable-dev-shm-usage', '--disable-gpu',
                '--disable-web-security',
                '--allow-file-access-from-files',
            ]
        )
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.goto(f'file://{HTML_FILE}', wait_until='domcontentloaded')
        log('Page DOM loaded')

        # Select commodity 1404 + compare years
        log('Configuring commodity 1404 and compare years…')
        page.evaluate(f"""() => {{
            // Set commodity
            const s = document.getElementById('selCommodity');
            if (s) s.value = '{DEFAULT_COMMODITY}';
            // Set From year
            const fromSel = document.getElementById('selYearFrom');
            if (fromSel) fromSel.value = '{FROM_YEAR}';
            // Set To year (most recent = primary)
            const toSel = document.getElementById('selYearTo');
            if (toSel) toSel.value = '{TO_YEAR}';
        }}""")

        # Click Load Data
        log('Clicking Load Data…')
        page.click('#btnLoad')

        # Wait for USDA API to respond — up to 90 seconds
        log('Waiting for USDA API (up to 90s)…')
        try:
            page.wait_for_function(
                "() => document.getElementById('statusBar')"
                "    ?.textContent?.startsWith('✓')",
                timeout=90_000
            )
        except PwTimeout:
            status = page.text_content('#statusBar') or 'unknown'
            raise RuntimeError(f'Data did not load in 90s. Status: "{status}"')
        log('✓ Data loaded from USDA API')

        # Wait 1 full minute for all charts + tables to render
        # (projection model fetches 15 years of history which takes time)
        log('Waiting 60s for all charts and tables to fully render…')
        page.wait_for_timeout(60_000)

        # Make sure we're on the dashboard tab
        page.evaluate("""() => {
            if (typeof switchTab === 'function') switchTab('dashboard');
        }""")

        # Call prepChartsForPrint — resizes canvases to 1020×756 and switches colours
        log('Resizing charts to print resolution (1020×756)…')
        page.evaluate("""() => {
            if (typeof prepChartsForPrint === 'function') prepChartsForPrint();
        }""")

        # Wait 3 extra seconds for Chart.js to finish re-rendering at new dimensions
        log('Waiting 3s for chart re-render at print resolution…')
        page.wait_for_timeout(3_000)

        # Generate PDF — same engine as Chromium File → Print → Save as PDF
        log('Generating PDF via Chromium print engine…')
        pdf_bytes = page.pdf(
            format='A4',
            landscape=True,
            margin={
                'top':    '8mm',
                'bottom': '8mm',
                'left':   '8mm',
                'right':  '8mm',
            },
            print_background=True,
        )

        log(f'✓ PDF: {len(pdf_bytes):,} bytes ({len(pdf_bytes)//1024}KB)')
        browser.close()

    return pdf_bytes


def send_email(pdf_bytes):
    date_str = datetime.now().strftime('%b %d, %Y')
    subject  = f'USDA ESR Weekly Report — All Upland Cotton — {date_str}'
    filename = f'ESR_Report_Upland_Cotton_{REPORT_DATE}.pdf'

    msg            = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)

    body = (
        f'USDA ESR Weekly Report — All Upland Cotton\n'
        f'Generated: {datetime.now().strftime("%A, %B %d, %Y")}\n\n'
        f'Please find the full PDF report attached.\n\n'
        f'Report includes:\n'
        f'  Pages 1–2: Seasonality charts — all 8 metrics, multi-year\n'
        f'  Page 3:    Weekly Intelligence + End-of-Year Projection\n'
        f'  Page 4:    Country Summary — TW/LW/change + multi-year snapshot\n'
        f'  Page 5:    8-Week Sales Trend + Historical Percentile Ranges\n'
        f'  Page 6:    Export Sales Narrative Summary\n\n'
        f'Source: USDA FAS Export Sales Reporting | api.fas.usda.gov\n'
    )
    msg.attach(MIMEText(body, 'plain'))

    pdf_part = MIMEBase('application', 'pdf')
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    pdf_part.add_header('Content-Disposition', 'attachment', filename=filename)
    msg.attach(pdf_part)

    log(f'Sending to {len(EMAIL_TO)} recipient(s) via {SMTP_HOST}:{SMTP_PORT}…')
    log(f'  Attachment: {filename} ({len(pdf_bytes)//1024}KB)')

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
    log('=== USDA ESR Weekly Report ===')
    log(f'Date:      {REPORT_DATE}')
    log(f'Commodity: {DEFAULT_COMMODITY} (All Upland Cotton, All Countries)')
    log(f'From:      {EMAIL_FROM}')
    log(f'To:        {", ".join(EMAIL_TO)}')
    log(f'SMTP:      {SMTP_HOST}:{SMTP_PORT}')

    if not EMAIL_TO:
        raise ValueError('EMAIL_TO is empty')

    pdf_bytes = render_to_pdf()
    send_email(pdf_bytes)
    log('=== Done ===')

if __name__ == '__main__':
    main()
