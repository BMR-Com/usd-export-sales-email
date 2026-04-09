# scripts/send_report.py
import os
import sys
import time
import smtplib
import re
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.utils import formatdate
from email import encoders
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# === SECURITY: Load from environment only ===
REQUIRED_SECRETS = ['SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS', 
                    'EMAIL_FROM', 'EMAIL_TO', 'DASHBOARD_PASSWORD']

for secret in REQUIRED_SECRETS:
    if not os.getenv(secret):
        print(f"[CRITICAL] Missing required secret: {secret}")
        sys.exit(1)

SMTP_HOST = os.getenv('SMTP_HOST')
SMTP_PORT = int(os.getenv('SMTP_PORT'))
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASS = os.getenv('SMTP_PASS')
EMAIL_FROM = os.getenv('EMAIL_FROM')
EMAIL_TO = [e.strip() for e in os.getenv('EMAIL_TO').split(',') if e.strip()]
AUTH_PASSWORD = os.getenv('DASHBOARD_PASSWORD')
MAX_RETRIES = 3

def now():
    return datetime.now().strftime('%H:%M:%S')

def mask_secret(s, show=4):
    if len(s) <= show * 2:
        return '*' * len(s)
    return s[:show] + '*' * (len(s) - show * 2) + s[-show:]

def authenticate(page):
    try:
        overlay = page.locator('#auth-overlay')
        overlay.wait_for(state='visible', timeout=5000)
        print(f"[{now()}] 🔒 Auth required — entering credentials...")
        page.fill('#auth-inp', '')
        page.locator('#auth-inp').press_sequentially(AUTH_PASSWORD, delay=10)
        with page.expect_response(lambda r: r.status == 200 or r.status == 304, timeout=5000):
            page.click('#auth-btn')
        overlay.wait_for(state='hidden', timeout=10000)
        print(f"[{now()}] ✅ Auth successful (password: {mask_secret(AUTH_PASSWORD)})")
    except PlaywrightTimeout:
        print(f"[{now()}] ℹ️ No auth overlay detected")
    except Exception as e:
        print(f"[{now()}] ❌ Auth failed: {e}")
        raise

def render_to_pdf():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1920, 'height': 1080}, user_agent='BMR-ReportBot/1.0')
        page = context.new_page()
        
        print(f"[{now()}] Loading dashboard...")
        page.goto(f"file://{os.path.abspath('index.html')}", wait_until='networkidle')
        
        authenticate(page)
        
        # Force clean DOM
        print(f"[{now()}] Cleaning DOM...")
        page.evaluate('''() => {
            const overlay = document.getElementById('auth-overlay');
            if (overlay) overlay.remove();
            document.querySelectorAll('.modal, .overlay, [class*="backdrop"]').forEach(el => el.remove());
        }''')
        page.wait_for_timeout(300)
        
        # Set defaults
        current_year = datetime.now().year
        start_year = current_year - 5
        print(f"[{now()}] Setting defaults: ALL countries, {start_year}–{current_year}...")
        
        page.evaluate(f'''() => {{
            const ys = document.getElementById('yearStart') || document.getElementById('startYear') || document.querySelector('[id*="start"][id*="year"]');
            const ye = document.getElementById('yearEnd') || document.getElementById('endYear') || document.querySelector('[id*="end"][id*="year"]');
            const cs = document.getElementById('countrySelect') || document.getElementById('country') || document.querySelector('select');
            
            if (ys) {{ ys.value = '{start_year}'; ys.dispatchEvent(new Event('change')); }}
            if (ye) {{ ye.value = '{current_year}'; ye.dispatchEvent(new Event('change')); }}
            if (cs) {{
                const allOpt = Array.from(cs.options).find(o => /all|total|world|global/i.test(o.text)) || cs.options[0];
                if (allOpt) {{ cs.value = allOpt.value; cs.dispatchEvent(new Event('change')); }}
            }}
        }}''')
        page.wait_for_timeout(500)
        print(f"[{now()}] Defaults applied")
        
        # Activate and click button
        print(f"[{now()}] Activating button...")
        page.evaluate('''() => {
            const btn = document.getElementById('btnLoad');
            if (btn) {{
                btn.style.cssText = 'display:block!important;visibility:visible!important;opacity:1!important;pointer-events:auto!important;position:relative!important;z-index:9999!important';
                btn.disabled = false;
                btn.hidden = false;
            }}
        }}''')
        page.wait_for_timeout(200)
        
        print(f"[{now()}] Clicking Load Data...")
        page.evaluate('() => { const b=document.getElementById("btnLoad"); if(b)b.click(); else if(typeof loadAll==="function")loadAll(); }')
        
        # === 30 SECOND WAIT FOR PAGE LOAD ===
        print(f"[{now()}] ⏳ Waiting 30 seconds for data to load...")
        page.wait_for_timeout(30000)
        print(f"[{now()}] ✓ Wait complete, checking for content...")
        
        # Verify content appeared
        try:
            page.wait_for_selector('table, canvas, .chart, tbody, tr, [class*="data"]', timeout=10000)
            print(f"[{now()}] ✓ Data content detected")
        except:
            page.screenshot(path='/tmp/debug_no_content.png')
            print(f"[{now()}] ⚠️ No content after 30s wait, screenshot saved")
            raise
        
        # Extra 2 seconds for charts
        page.wait_for_timeout(2000)
        print(f"[{now()}] Data fully loaded, generating PDF...")
        
        # Generate PDF
        pdf_path = '/tmp/cotton_report.pdf'
        page.pdf(path=pdf_path, format='A4', print_background=True,
                 margin={'top': '20px', 'right': '20px', 'bottom': '20px', 'left': '20px'},
                 display_header_footer=True,
                 header_template='<div style="font-size:9px;margin-left:20px;">BMR Cotton Analytics — Confidential</div>',
                 footer_template='<div style="font-size:9px;text-align:center;"><span class="pageNumber"></span></div>')
        
        browser.close()
        print(f"[{now()}] PDF generated: {pdf_path}")
        return pdf_path

def send_email(pdf_path):
    current_year = datetime.now().year
    start_year = current_year - 5
    
    msg = MIMEMultipart()
    msg['From'] = EMAIL_FROM
    msg['To'] = ', '.join(EMAIL_TO)
    msg['Date'] = formatdate(localtime=True)
    msg['Subject'] = f'[BMR] USDA Cotton Report — {datetime.now():%Y-%m-%d %H:%M} UTC'
    msg['X-Priority'] = '1'
    
    body = MIMEText(f'''BMR Cotton Analytics — Automated Report

Generated: {datetime.now():%Y-%m-%d %H:%M:%S} UTC
Source: USDA ESR Commodity 1404 (All Upland Cotton)
Period: MY{start_year}–MY{current_year} (All Countries)

This is an automated report. Do not reply.
''', 'plain')
    msg.attach(body)
    
    with open(pdf_path, 'rb') as f:
        attachment = MIMEBase('application', 'pdf')
        attachment.set_payload(f.read())
        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', f'attachment; filename=BMR_Cotton_Report_{datetime.now():%Y%m%d_%H%M}.pdf')
        msg.attach(attachment)
    
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    
    print(f"[{now()}] 📧 Email sent to {len(EMAIL_TO)} recipient(s)")

def main():
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"\n[{now()}] {'='*40}")
            print(f"[{now()}] Attempt {attempt}/{MAX_RETRIES}")
            print(f"[{now()}] {'='*40}")
            
            pdf = render_to_pdf()
            send_email(pdf)
            
            print(f"[{now()}] ✅ SUCCESS — Report delivered")
            sys.exit(0)
            
        except Exception as e:
            print(f"[{now()}] ❌ FAILED: {str(e)}")
            if attempt < MAX_RETRIES:
                backoff = min(5 * (2 ** attempt), 60)
                print(f"[{now()}] ⏳ Backoff {backoff}s...")
                time.sleep(backoff)
    
    print(f"[{now()}] 🚨 CRITICAL: All retries exhausted")
    sys.exit(1)

if __name__ == '__main__':
    main()
