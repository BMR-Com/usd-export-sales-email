#!/usr/bin/env python3
"""
send_options_report.py
======================
Loads cotton_options/index.html in headless Chromium,
injects the vol data directly (bypasses file:// script-src restriction),
generates PDF, sends via SMTP.

Reuses same GitHub Secrets as ESR send_report.py:
  SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASS  EMAIL_FROM  EMAIL_TO
"""
import os, sys, ssl, smtplib
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from playwright.sync_api import sync_playwright

def require_env(name):
    val = os.environ.get(name, '').strip()
    if not val:
        print(f'\u2717 MISSING SECRET: {name}', flush=True)
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

def get_csv_text():
    html_dir  = Path(HTML_FILE).parent
    repo_root = html_dir.parent

    # Priority 1: cotton_options/cotton_options_data.csv (built by build_options_data.py)
    csv_copy = html_dir / 'cotton_options_data.csv'
    if csv_copy.exists():
        log(f'Found cotton_options_data.csv ({csv_copy.stat().st_size // 1024}KB)')
        return csv_copy.read_text(encoding='utf-8-sig')

    # Priority 2: data/cotton_options_history.csv (original upload)
    csv_src = repo_root / 'data' / 'cotton_options_history.csv'
    if csv_src.exists():
        log(f'Found cotton_options_history.csv ({csv_src.stat().st_size // 1024}KB)')
        return csv_src.read_text(encoding='utf-8-sig')

    raise FileNotFoundError(
        'No vol data found.\n'
        '  Run build_options_data.py to create cotton_options/cotton_options_data.csv'
    )

def render_to_pdf():
    log(f'HTML: {HTML_FILE}')
    if not Path(HTML_FILE).exists():
        raise FileNotFoundError(f'Options page not found: {HTML_FILE}')

    csv_text = get_csv_text()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox',
                '--disable-dev-shm-usage', '--disable-gpu',
                '--disable-web-security', '--allow-file-access-from-files',
            ]
        )
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)

        log('Loading page...')
        page.goto(f'file://{HTML_FILE}', wait_until='load')
        page.wait_for_timeout(1000)
        # Bypass password screen
        page.evaluate("() => { try { sessionStorage.setItem('bmr_auth','1'); } catch(e){} }")
        page.evaluate("() => { var o=document.getElementById('auth-overlay'); if(o) o.remove(); }")
        page.wait_for_timeout(500)

        # Check if main script executed (file:// can block inline scripts in some environments)
        defined = page.evaluate("() => typeof parseAndBuild")
        if defined == 'undefined':
            log('Main script not auto-executed — injecting via add_script_tag...')
            html_content = Path(HTML_FILE).read_text(encoding='utf-8')
            import re as _re
            script_tags  = [m.start() for m in _re.finditer(r'<script>', html_content)]
            script_close = [m.start() for m in _re.finditer(r'</script>', html_content)]
            main_js = html_content[script_tags[-1]:script_close[-1]].replace('<script>','',1)
            page.add_script_tag(content=main_js)
            page.wait_for_timeout(800)
            log('Main script injected via add_script_tag')

        defined2 = page.evaluate("() => typeof parseAndBuild")
        if defined2 == 'undefined':
            raise RuntimeError('parseAndBuild still not defined after manual injection')
        log('Page JS ready')

        # Inject CSV directly — file:// blocks <script src> cross-file loading
        log(f'Injecting {len(csv_text):,} chars of vol data...')
        safe_csv = csv_text.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        page.evaluate(f'window.COTTON_OPTIONS_DATA = `{safe_csv}`;')

        # Parse and render
        result = page.evaluate("""() => {
            try {
                parseAndBuild(window.COTTON_OPTIONS_DATA);
                return {
                    ok: true,
                    records: allData.length,
                    latest: allData.length ? allData[allData.length-1].date : 'none'
                };
            } catch(e) {
                return { ok: false, error: e.message };
            }
        }""")

        log(f'Parse result: {result}')

        if not result.get('ok'):
            if errors: log(f'JS errors: {errors[:3]}')
            raise RuntimeError(f'parseAndBuild failed: {result.get("error","unknown")}')

        log(f'Records: {result["records"]:,}  Latest: {result["latest"]}')

        log('Rendering charts (waiting 6s)...')
        page.wait_for_timeout(6000)

        chart_count = page.evaluate("() => Object.keys(charts).length")
        log(f'Charts rendered: {chart_count}')

        page.evaluate("() => { if (typeof prepForPrint === 'function') prepForPrint(); }")
        page.wait_for_timeout(1000)

        log('Generating PDF...')
        pdf_bytes = page.pdf(
            format='A4', landscape=True,
            margin={'top':'6mm','bottom':'6mm','left':'7mm','right':'7mm'},
            print_background=True,
        )
        log(f'PDF: {len(pdf_bytes):,} bytes ({len(pdf_bytes)//1024}KB)')
        browser.close()

    return pdf_bytes, result['latest']

def send_email(pdf_bytes, latest_date):
    date_str = datetime.now().strftime('%b %d, %Y')
    subject  = f'Cotton Options Analytics \u2014 Implied Vol Report \u2014 {date_str}'
    filename = f'Cotton_Options_Analytics_{REPORT_DATE}.pdf'
    log(f'Sending to: {", ".join(EMAIL_TO)}')

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)

    body = (
        f'Cotton Options Analytics \u2014 Weekly Implied Volatility Report\n'
        f'Generated: {datetime.now().strftime("%A, %B %d, %Y")}\n'
        f'Latest data: {latest_date}\n\n'
        f'Report includes:\n'
        f'  \u2022 Seasonality charts \u2014 ATM Vol, 25D RR, Call Skew, Put Skew (all tenors)\n'
        f'  \u2022 Term structure differentials \u2014 1M spreads vs 2M/3M/6M/1Y\n'
        f'  \u2022 Current level summary \u2014 percentile rankings vs 5/10/20-year history\n'
        f'  \u2022 Rule-based trade ideas\n'
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
            srv.ehlo(); srv.starttls(context=ssl.create_default_context())
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    log(f'\u2713 Sent successfully')

def main():
    log('=== Cotton Options Analytics Report ===')
    log(f'Date: {REPORT_DATE}  SMTP: {SMTP_HOST}:{SMTP_PORT}')
    pdf_bytes, latest_date = render_to_pdf()
    send_email(pdf_bytes, latest_date)
    log('=== Done ===')

if __name__ == '__main__':
    main()
