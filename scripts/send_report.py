#!/usr/bin/env python3
"""
USDA ESR Weekly Email Report
=============================
Loads index.html in headless Chromium, selects commodity 1404 / All Countries,
waits for USDA API data + charts to fully render, then calls Playwright's
page.pdf() — the same Chromium print engine as window.print() — to produce
an exact PDF of what you see on screen. The PDF is sent as an email attachment
via SMTP.

GitHub Secrets required:
  SMTP_HOST           e.g. smtp.gmail.com
  SMTP_PORT           e.g. 465  (SSL) or 587 (STARTTLS)
  SMTP_USER           SMTP login username
  SMTP_PASS           SMTP password / App Password
  EMAIL_FROM          e.g. ESR Reports <you@gmail.com>
  EMAIL_TO            comma-separated recipients
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

DEFAULT_COMMODITY = '1404'   # All Upland Cotton
COMPARE_YEARS     = [2025, 2024, 2023, 2022]

CHART_METRIC_IDS = [
    'grossNewSales', 'currentMYNetSales', 'weeklyExports', 'accumulatedExports',
    'outstandingSales', 'currentMYTotalCommitment', 'nextMYNetSales',
    'nextMYOutstandingSales',
]

def log(msg):
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


# ── Render page and produce PDF bytes ─────────────────────────────────────────
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
                '--disable-web-security',   # allow file:// cross-origin fetches
                '--allow-file-access-from-files',
            ]
        )
        # Use a wide viewport so the layout matches the dashboard view
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        page.goto(f'file://{HTML_FILE}', wait_until='domcontentloaded')
        log('Page loaded')

        # ── Set commodity 1404 + compare years ───────────────────────────────
        log('Selecting commodity 1404 (All Upland Cotton) and compare years…')
        page.evaluate(f"""() => {{
            const s = document.getElementById('selCommodity');
            if (s) s.value = '{DEFAULT_COMMODITY}';

            const y = document.getElementById('selYear');
            if (y && y.options.length) y.value = y.options[0].value;

            const c = document.getElementById('selCompareYears');
            if (c) {{
                for (const o of c.options) {{
                    o.selected = {json.dumps(COMPARE_YEARS)}.includes(+o.value);
                }}
            }}
        }}""")

        # ── Click Load Data ───────────────────────────────────────────────────
        log('Clicking Load Data…')
        page.click('#btnLoad')

        # ── Wait for USDA API to return data (up to 90s) ──────────────────────
        log('Waiting for USDA API response (up to 90s)…')
        try:
            page.wait_for_function(
                "() => document.getElementById('statusBar')"
                "    ?.textContent?.startsWith('✓')",
                timeout=90_000
            )
        except PwTimeout:
            status = page.text_content('#statusBar') or 'unknown'
            raise RuntimeError(f'Data did not load in 90s. Status: "{status}"')

        log('✓ Data loaded')

        # ── Wait for Chart.js to finish all 8 charts ──────────────────────────
        log('Waiting for all 8 charts to render…')
        page.wait_for_timeout(5000)

        # ── Switch charts to print-friendly colours (black text, white bg) ────
        log('Switching charts to print colours…')
        page.evaluate("""() => {
            if (typeof prepChartsForPrint === 'function') prepChartsForPrint();
        }""")
        page.wait_for_timeout(800)

        # ── Make sure we're on the Dashboard tab ──────────────────────────────
        page.evaluate("""() => {
            if (typeof switchTab === 'function') switchTab('dashboard');
        }""")

        # ── Capture PDF using Playwright's print engine ────────────────────────
        # This is identical to what Chromium produces when you do File → Print → Save as PDF
        # or window.print() — it applies all @media print CSS rules exactly.
        log('Generating PDF via Chromium print engine…')
        pdf_bytes = page.pdf(
            format='A4',
            landscape=True,
            margin={
                'top':    '6mm',
                'bottom': '6mm',
                'left':   '7mm',
                'right':  '7mm',
            },
            print_background=True,   # preserves dark header backgrounds, chart colours
        )

        log(f'✓ PDF generated: {len(pdf_bytes):,} bytes ({len(pdf_bytes)//1024}KB)')

        browser.close()

    return pdf_bytes


# ── Build plain-text email body ───────────────────────────────────────────────
def build_plain_body(comm_short):
    now = datetime.now().strftime('%A, %B %d, %Y')
    return (
        f'USDA ESR Weekly Report — {comm_short}\n'
        f'Generated: {now}\n\n'
        f'Please find the PDF report attached.\n\n'
        f'The report includes:\n'
        f'  • Seasonality charts — all 8 metrics, multi-year comparison\n'
        f'  • Weekly Market Intelligence — top buyers, shipments, signals\n'
        f'  • End-of-Year Export Projection — 6 models, backtested\n'
        f'  • Country Summary — TW/LW/change + multi-year snapshot\n\n'
        f'Commodity: {comm_short}  |  All Countries\n'
        f'Data source: USDA FAS Export Sales Reporting  |  api.fas.usda.gov\n'
    )


# ── Send email with PDF attachment ────────────────────────────────────────────
def send_email(pdf_bytes, comm_short):
    date_str   = datetime.now().strftime('%b %d, %Y')
    subject    = f'USDA ESR Weekly Report — {comm_short} — {date_str}'
    filename   = f'ESR_Report_{comm_short.replace(" ","_")}_{REPORT_DATE}.pdf'

    msg            = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)

    # Plain text body
    msg.attach(MIMEText(build_plain_body(comm_short), 'plain'))

    # PDF attachment
    pdf_part = MIMEBase('application', 'pdf')
    pdf_part.set_payload(pdf_bytes)
    encoders.encode_base64(pdf_part)
    pdf_part.add_header(
        'Content-Disposition',
        'attachment',
        filename=filename
    )
    msg.attach(pdf_part)

    log(f'Sending to {len(EMAIL_TO)} recipient(s) via {SMTP_HOST}:{SMTP_PORT}…')
    log(f'  Subject:  {subject}')
    log(f'  PDF file: {filename}  ({len(pdf_bytes)//1024}KB)')

    if SMTP_PORT == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    else:
        # Port 587 — STARTTLS
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls(context=ssl.create_default_context())
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    log(f'✓ Email sent to: {", ".join(EMAIL_TO)}')


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log('=== USDA ESR Weekly Report ===')
    log(f'Date:       {REPORT_DATE}')
    log(f'Commodity:  {DEFAULT_COMMODITY} (All Upland Cotton)')
    log(f'From:       {EMAIL_FROM}')
    log(f'To:         {", ".join(EMAIL_TO)}')
    log(f'SMTP:       {SMTP_HOST}:{SMTP_PORT}')
    log(f'HTML file:  {HTML_FILE}')

    if not EMAIL_TO:
        raise ValueError('EMAIL_TO is empty — add recipients to GitHub Secrets')

    # 1. Render the dashboard and export as PDF
    pdf_bytes = render_to_pdf()

    # 2. Send with PDF attached
    send_email(pdf_bytes, 'Upland Cotton')

    log('=== Done ===')


if __name__ == '__main__':
    main()
