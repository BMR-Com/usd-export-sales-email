#!/usr/bin/env python3
"""
USDA ESR Weekly Email Report
=============================
Loads index.html in headless Chromium via Playwright, waits for USDA API
data to render, captures all 8 Chart.js canvases as JPEG images, extracts
KPI/table data from the DOM, builds a fully static email-safe HTML report,
and sends via SMTP (Gmail or any provider).

GitHub Secrets required:
  SMTP_HOST     e.g. smtp.gmail.com
  SMTP_PORT     e.g. 465  (SSL) or 587 (TLS/STARTTLS)
  SMTP_USER     SMTP login username (usually your email address)
  SMTP_PASS     SMTP password / App Password
  EMAIL_FROM    Display from address  e.g. "ESR Reports <you@gmail.com>"
  EMAIL_TO      Comma-separated recipients  e.g. "a@x.com,b@y.com"
"""

import os, sys, re, json, smtplib, ssl
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ── Config from GitHub Secrets ────────────────────────────────────────────────
SMTP_HOST   = os.environ['SMTP_HOST']
SMTP_PORT   = int(os.environ['SMTP_PORT'])
SMTP_USER   = os.environ['SMTP_USER']
SMTP_PASS   = os.environ['SMTP_PASS']
EMAIL_FROM  = os.environ['EMAIL_FROM']
EMAIL_TO    = [r.strip() for r in os.environ['EMAIL_TO'].split(',') if r.strip()]
HTML_FILE   = os.environ.get('HTML_FILE', str(Path(__file__).parent.parent / 'index.html'))
REPORT_DATE = os.environ.get('REPORT_DATE', datetime.now().strftime('%Y-%m-%d'))

# Default: commodity 1404 = Upland Cotton All, compare last 3 years
DEFAULT_COMMODITY = '1404'
COMPARE_YEARS     = [2025, 2024, 2023]

CHART_METRIC_IDS = [
    'grossNewSales','currentMYNetSales','weeklyExports','accumulatedExports',
    'outstandingSales','currentMYTotalCommitment','nextMYNetSales','nextMYOutstandingSales',
]
CHART_LABELS = {
    'grossNewSales':            'Gross Sales (Weekly)',
    'currentMYNetSales':        'Net Sales (Weekly)',
    'weeklyExports':            'Weekly Exports',
    'accumulatedExports':       'Accumulated Exports',
    'outstandingSales':         'Outstanding Sales',
    'currentMYTotalCommitment': 'Total Commitment MY',
    'nextMYNetSales':           'Next MY Net Sales',
    'nextMYOutstandingSales':   'Next MY Outstanding',
}

def log(msg): print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


# ── Browser render ────────────────────────────────────────────────────────────
def render_and_extract():
    log(f'Loading {HTML_FILE}')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox',
                  '--disable-dev-shm-usage','--disable-gpu']
        )
        page = browser.new_page(viewport={'width':1600,'height':900})
        page.goto(f'file://{HTML_FILE}', wait_until='domcontentloaded')

        # Configure commodity + years
        log('Configuring commodity 1404 and compare years…')
        page.evaluate(f"""() => {{
            const s = document.getElementById('selCommodity');
            if(s) s.value = '{DEFAULT_COMMODITY}';
            const y = document.getElementById('selYear');
            if(y && y.options.length) y.value = y.options[0].value;
            const c = document.getElementById('selCompareYears');
            if(c) for(const o of c.options)
                o.selected = {json.dumps(COMPARE_YEARS)}.includes(+o.value);
        }}""")

        # Click load
        log('Clicking Load Data…')
        page.click('#btnLoad')

        # Wait for success status (up to 90s — USDA API can be slow)
        log('Waiting for USDA API data (up to 90s)…')
        try:
            page.wait_for_function(
                "() => document.getElementById('statusBar')?.textContent?.startsWith('✓')",
                timeout=90_000
            )
        except PwTimeout:
            status = page.text_content('#statusBar') or 'unknown'
            raise RuntimeError(f'Data did not load. Status: {status}')

        # Extra wait for Chart.js rendering
        log('Waiting for charts to finish rendering…')
        page.wait_for_timeout(4000)

        # Switch to print-friendly colors before screenshot
        page.evaluate("() => { if(typeof prepChartsForPrint==='function') prepChartsForPrint(); }")
        page.wait_for_timeout(600)

        # Capture chart canvases
        log('Capturing 8 chart images…')
        chart_images = {}
        for mid in CHART_METRIC_IDS:
            url = page.evaluate(f"""() => {{
                const c = document.getElementById('sc_{mid}');
                try {{ return c ? c.toDataURL('image/jpeg', 0.75) : null; }} catch(e) {{ return null; }}
            }}""")
            if url and url.startswith('data:image'):
                chart_images[mid] = url
                log(f'  ✓ {mid} ({len(url)//1024}KB)')
            else:
                log(f'  ✗ {mid}')

        # Restore dark theme
        page.evaluate("() => { if(typeof restoreChartsAfterPrint==='function') restoreChartsAfterPrint(); }")

        # Extract DOM data
        log('Extracting KPIs, tables, panels from DOM…')
        kpi_data = page.evaluate("""
            () => [...document.querySelectorAll('#intelKpis .kpi-cell')].map(c => ({
                label: c.querySelector('.kpi-label')?.textContent?.trim()||'',
                value: c.querySelector('.kpi-val')?.textContent?.trim()||'—',
                badges: [...c.querySelectorAll('.kpi-badge')].map(b=>({text:b.textContent.trim(),cls:b.className})),
            }))""")

        prog_data = page.evaluate("""() => ({
            pct:   document.getElementById('progPct')?.textContent?.trim()||'',
            label: document.getElementById('progLabel')?.textContent?.trim()||'',
            wks:   document.getElementById('progWks')?.textContent?.trim()||'',
        })""")

        intel_sub = page.text_content('#intelSub') or ''

        intel_panels = page.evaluate("""
            () => [...document.querySelectorAll('#intelBody .intel-panel')].map(p => ({
                title: p.querySelector('.intel-panel-title')?.textContent?.trim()||'',
                rows:  [...p.querySelectorAll('.intel-row,.signal-row')].map(r=>r.textContent.trim()),
            }))""")

        proj_panels = page.evaluate("""
            () => [...document.querySelectorAll('#projBody .proj-panel')].map(p => ({
                title: p.querySelector('.proj-panel-title')?.textContent?.trim()||'',
                rows:  [...p.querySelectorAll('.proj-row')].map(r=>({
                    name:      r.querySelector('.proj-row-name')?.textContent?.trim()||'',
                    val:       r.querySelector('.proj-row-val')?.textContent?.trim()||'',
                    desc:      r.querySelector('.proj-row-desc')?.textContent?.trim()||'',
                    consensus: r.classList.contains('consensus'),
                })),
                table: p.querySelector('table')?.outerHTML||'',
            }))""")

        tbl1_html = page.evaluate("() => document.querySelector('#summaryTbl1 table')?.outerHTML||''")
        tbl2_html = page.evaluate("() => document.querySelector('#summaryTbl2 table')?.outerHTML||''")

        comm_name  = page.evaluate("() => document.getElementById('selCommodity')?.selectedOptions[0]?.text||'Upland Cotton'")
        comm_short = re.sub(r'^[^·]*·\s*','', comm_name) or comm_name

        browser.close()

    return dict(chart_images=chart_images, kpi_data=kpi_data, prog_data=prog_data,
                intel_sub=intel_sub, intel_panels=intel_panels, proj_panels=proj_panels,
                tbl1_html=tbl1_html, tbl2_html=tbl2_html, comm_short=comm_short)


# ── Build email HTML ──────────────────────────────────────────────────────────
def build_email_html(data):
    log('Building email HTML…')
    comm      = data['comm_short']
    prog      = data['prog_data']
    intel_sub = data['intel_sub']
    pct_num   = float(re.sub(r'[^0-9.]','', prog.get('pct','0') or '0') or 0)
    gen_date  = datetime.now().strftime('%A, %B %d, %Y')

    def badge_style(cls):
        if 'pos' in cls: return 'background:#dcfce7;color:#166534;border:1px solid #86efac'
        if 'neg' in cls: return 'background:#fee2e2;color:#991b1b;border:1px solid #fca5a5'
        return 'background:#f1f5f9;color:#475569;border:1px solid #cbd5e1'

    # KPI cells
    kpi_cells = ''
    for k in data['kpi_data']:
        badges = ''.join(
            f'<span style="font-size:9px;padding:1px 5px;border-radius:3px;{badge_style(b["cls"])};margin-left:3px">{b["text"]}</span>'
            for b in k.get('badges',[])
        )
        kpi_cells += (
            f'<td style="padding:8px 10px;background:#fff;border:1px solid #e2e8f0;vertical-align:top;min-width:88px">'
            f'<div style="font-family:\'Courier New\',monospace;font-size:8px;color:#64748b;letter-spacing:1px;text-transform:uppercase;margin-bottom:3px">{k["label"]}</div>'
            f'<div style="font-family:\'Courier New\',monospace;font-size:19px;font-weight:700;color:#0f172a;line-height:1">{k["value"]}</div>'
            f'<div style="margin-top:3px">{badges}</div></td>'
        )

    # Intel panels
    intel_cols = ''
    for panel in data['intel_panels']:
        rows = ''.join(
            f'<div style="padding:3px 0;border-bottom:1px solid #f1f5f9;font-family:\'Courier New\',monospace;font-size:10px;color:#1e293b">{r}</div>'
            for r in panel['rows']
        )
        intel_cols += (
            f'<td style="padding:0;vertical-align:top;width:33%">'
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:3px;padding:8px 10px;margin:0 3px">'
            f'<div style="font-family:\'Courier New\',monospace;font-size:8px;font-weight:700;color:#334155;letter-spacing:1.5px;text-transform:uppercase;border-bottom:1px solid #e2e8f0;padding-bottom:4px;margin-bottom:5px">{panel["title"]}</div>'
            f'{rows}</div></td>'
        )

    # Projection panels
    proj_cols = ''
    for panel in data['proj_panels']:
        rows = ''
        for r in panel['rows']:
            con = r['consensus']
            col = '#92400e' if con else '#334155'
            vcol = '#92400e' if con else '#0f172a'
            bg = 'background:#fffbeb;border-radius:2px;padding:3px 5px;' if con else ''
            rows += (
                f'<div style="padding:3px 0;border-bottom:1px solid #f1f5f9;display:flex;justify-content:space-between;align-items:center;{bg}">'
                f'<div><div style="font-family:\'Courier New\',monospace;font-size:10px;color:{col}">{r["name"]}</div>'
                + (f'<div style="font-family:\'Courier New\',monospace;font-size:8px;color:#64748b">{r["desc"]}</div>' if r["desc"] else '')
                + f'</div><div style="font-family:\'Courier New\',monospace;font-size:13px;font-weight:700;color:{vcol}">{r["val"]}</div></div>'
            )
        tbl_safe = ''
        if panel.get('table'):
            t = re.sub(r' class="[^"]*"','',panel['table'])
            t = re.sub(r' style="[^"]*"','',t)
            t = t.replace('<table>','<table style="width:100%;border-collapse:collapse;font-family:\'Courier New\',monospace;font-size:9px">')
            t = re.sub(r'<th\b','<th style="padding:2px 4px;background:#1e293b;color:#f1f5f9;border:1px solid #334155;text-align:center"',t)
            t = re.sub(r'<td\b','<td style="padding:2px 4px;border:1px solid #e2e8f0;color:#1e293b"',t)
            tbl_safe = f'<div style="margin-top:5px;overflow-x:auto">{t}</div>'
        proj_cols += (
            f'<td style="padding:0;vertical-align:top;width:33%">'
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:3px;padding:8px 10px;margin:0 3px">'
            f'<div style="font-family:\'Courier New\',monospace;font-size:8px;font-weight:700;color:#334155;letter-spacing:1.5px;text-transform:uppercase;border-bottom:1px solid #e2e8f0;padding-bottom:4px;margin-bottom:5px">{panel["title"]}</div>'
            f'{rows}{tbl_safe}</div></td>'
        )

    # Chart rows
    def chart_row(ids):
        cells = ''
        for mid in ids:
            lbl = CHART_LABELS.get(mid, mid)
            img = data['chart_images'].get(mid)
            img_tag = (f'<img src="{img}" style="width:100%;height:auto;display:block" alt="{lbl}">'
                       if img else '<div style="padding:20px;color:#94a3b8;font-size:10px;text-align:center">No data</div>')
            cells += (
                f'<td style="padding:4px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:2px;width:25%;vertical-align:top">'
                f'<div style="font-family:\'Courier New\',monospace;font-size:8px;font-weight:700;color:#334155;letter-spacing:.5px;text-transform:uppercase;padding-bottom:3px;border-bottom:1px solid #e2e8f0;margin-bottom:3px">{lbl}</div>'
                f'{img_tag}</td>'
            )
        return f'<tr>{cells}</tr>'

    # Sanitise country tables
    def sanitise(html):
        if not html: return '<p style="color:#94a3b8;font-size:11px">No data</p>'
        html = re.sub(r' class="[^"]*"','',html)
        html = re.sub(r' style="[^"]*"','',html)
        html = html.replace('<table>','<table style="width:100%;border-collapse:collapse;font-family:\'Courier New\',monospace;font-size:9px">')
        html = re.sub(r'<th\b','<th style="padding:2px 4px;background:#1e293b;color:#f1f5f9;border:1px solid #334155;text-align:center;white-space:nowrap"',html)
        html = re.sub(r'<td\b','<td style="padding:2px 4px;border:1px solid #e2e8f0;color:#1e293b;white-space:nowrap"',html)
        return html

    tbl1 = sanitise(data['tbl1_html'])
    tbl2 = sanitise(data['tbl2_html'])

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USDA ESR Weekly Report — {comm} — {REPORT_DATE}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:12px 0">
<tr><td align="center">
<table width="900" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:4px;border:1px solid #e2e8f0">

<tr><td style="background:#1e293b;padding:14px 18px">
  <table width="100%"><tr>
    <td><div style="font-family:'Courier New',monospace;font-size:20px;font-weight:900;color:#f1f5f9;letter-spacing:2px">ESR EXPORT ANALYTICS</div>
      <div style="font-family:'Courier New',monospace;font-size:10px;color:#94a3b8;margin-top:2px">USDA FAS · Export Sales Reporting · {comm}</div></td>
    <td align="right">
      <div style="font-family:'Courier New',monospace;font-size:10px;color:#fbbf24;font-weight:700">WEEKLY REPORT</div>
      <div style="font-family:'Courier New',monospace;font-size:10px;color:#94a3b8;margin-top:2px">{gen_date}</div>
      <div style="font-family:'Courier New',monospace;font-size:10px;color:#94a3b8">{intel_sub}</div>
    </td>
  </tr></table>
</td></tr>

<tr><td style="padding:10px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:7px">▸ Key Performance Indicators</div>
  <table width="100%" cellpadding="0" cellspacing="1" style="background:#e2e8f0;border-radius:2px"><tr>{kpi_cells}</tr></table>
</td></tr>

<tr><td style="padding:7px 14px 0">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:2px;padding:5px 10px"><tr>
    <td style="font-family:'Courier New',monospace;font-size:8px;color:#475569;white-space:nowrap;padding-right:10px">{prog.get('label','')}</td>
    <td width="100%"><div style="background:#e2e8f0;height:5px;border-radius:3px"><div style="background:#b45309;height:5px;border-radius:3px;width:{min(100,pct_num):.1f}%"></div></div></td>
    <td style="font-family:'Courier New',monospace;font-size:15px;font-weight:700;color:#b45309;padding-left:10px;white-space:nowrap">{prog.get('pct','')}</td>
    <td style="font-family:'Courier New',monospace;font-size:8px;color:#94a3b8;padding-left:8px;white-space:nowrap">{prog.get('wks','')}</td>
  </tr></table>
</td></tr>

<tr><td style="padding:8px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:7px">▸ Weekly Market Intelligence</div>
  <table width="100%" cellpadding="0" cellspacing="0"><tr>{intel_cols}</tr></table>
</td></tr>

<tr><td style="padding:8px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:7px">▸ End-of-Year Export Projection</div>
  <table width="100%" cellpadding="0" cellspacing="0"><tr>{proj_cols}</tr></table>
</td></tr>

<tr><td style="padding:8px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:7px">▸ Seasonality Charts 1–4</div>
  <table width="100%" cellpadding="3" cellspacing="3" style="background:#f1f5f9;border-radius:2px">{chart_row(CHART_METRIC_IDS[:4])}</table>
</td></tr>

<tr><td style="padding:6px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:7px">▸ Seasonality Charts 5–8</div>
  <table width="100%" cellpadding="3" cellspacing="3" style="background:#f1f5f9;border-radius:2px">{chart_row(CHART_METRIC_IDS[4:])}</table>
</td></tr>

<tr><td style="padding:8px 14px 0">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:6px">▸ Country Summary — This Week / Last Week / Change</div>
  <div style="overflow-x:auto">{tbl1}</div>
</td></tr>

<tr><td style="padding:6px 14px 14px">
  <div style="font-family:'Courier New',monospace;font-size:8px;letter-spacing:2px;text-transform:uppercase;color:#64748b;border-bottom:1px solid #e2e8f0;padding-bottom:3px;margin-bottom:6px">▸ Country Summary — Accumulated / Outstanding / Commitment / Next MY</div>
  <div style="overflow-x:auto">{tbl2}</div>
</td></tr>

<tr><td style="background:#1e293b;padding:8px 18px">
  <div style="font-family:'Courier New',monospace;font-size:9px;color:#94a3b8;text-align:center">
    USDA FAS Export Sales Reporting · {comm} · Auto-generated {gen_date} · api.fas.usda.gov
  </div>
</td></tr>

</table></td></tr></table>
</body></html>"""

    log(f'Email HTML: {len(html)//1024}KB')
    return html


# ── SMTP send ─────────────────────────────────────────────────────────────────
def send_email(html_body, comm_short):
    subject = f'USDA ESR Weekly Report — {comm_short} — {datetime.now().strftime("%b %d, %Y")}'
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(EMAIL_TO)
    msg.attach(MIMEText(
        f'USDA ESR Weekly Report — {comm_short}\nGenerated: {datetime.now().strftime("%A, %B %d, %Y")}\n'
        'Please view this email in an HTML client to see the full report.',
        'plain'
    ))
    msg.attach(MIMEText(html_body, 'html'))

    log(f'Sending to {len(EMAIL_TO)} recipient(s) via {SMTP_HOST}:{SMTP_PORT}…')

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

    log(f'✓ Sent to: {", ".join(EMAIL_TO)}')


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log('=== USDA ESR Email Report ===')
    log(f'Date:      {REPORT_DATE}')
    log(f'Commodity: {DEFAULT_COMMODITY}')
    log(f'From:      {EMAIL_FROM}')
    log(f'To:        {", ".join(EMAIL_TO)}')
    log(f'SMTP:      {SMTP_HOST}:{SMTP_PORT}')

    if not Path(HTML_FILE).exists():
        raise FileNotFoundError(f'index.html not found: {HTML_FILE}')
    if not EMAIL_TO:
        raise ValueError('EMAIL_TO is empty')

    data = render_and_extract()
    html = build_email_html(data)
    send_email(html, data['comm_short'])
    log('=== Done ===')

if __name__ == '__main__':
    main()
