"""
scrape.py  —  Fetches latest CFTC Cotton On-Call report, appends new rows to
              data/cotton_oncall.csv, generates a PDF summary and emails it.

Runs automatically via GitHub Actions every Thursday (both EDT and EST timings).
Retries every 5 minutes for up to 35 minutes if report not yet published.
Only sends email when new data is found.
"""

import requests, re, os, sys, time, csv, smtplib, io
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

HEADERS         = {"User-Agent": "Mozilla/5.0 (compatible; CottonOnCallBot/1.0)"}
HEADERS_NOCACHE = {**HEADERS, "Cache-Control": "no-cache, no-store, must-revalidate",
                   "Pragma": "no-cache", "Expires": "0"}

BASE      = "https://www.cftc.gov"
BASE_PATH = "/MarketReports/CottonOnCall/HistoricalCottonOn-Call/"
# Repo root is one level up from scripts/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH  = os.path.join(REPO_ROOT, "data", "cotton_oncall.csv")
WEB_CSV   = os.path.join(REPO_ROOT, "cotton_oncall", "cotton_oncall_data.csv")

MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12
}
MONTH_MAP_ABBR = {k[:3]:v for k,v in MONTH_MAP.items()}

CSV_COLS = [
    "Week #","Report #","Report Date","Futures Based On",
    "Unfixed Call Sales","Chg Sales","Unfixed Call Purchases","Chg Purchases",
    "At Close","Chg At Close","Yr","Month","Old/New","Report Year"
]

MAX_RETRIES    = 7     # 7 attempts × 5 min = 35 min window
RETRY_INTERVAL = 300   # 5 minutes

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_old_new(cy, cm, ry, report_month):
    if report_month <= 6:
        if cy == ry and cm != 12: return "old"
        n = cy - ry + (1 if cm == 12 else 0)
        if n <= 0: return "old"
        return f"new{n}"
    else:
        if cy == ry: return "old"
        if cy == ry + 1 and cm != 12: return "old"
        n = (cy - ry - 1) + (1 if cm == 12 else 0)
        return f"new{n}"

def to_int(s):
    try: return int(str(s).replace(",","").replace(" ","").replace("+","").strip())
    except: return 0

def parse_date_from_text(text):
    """Try all known date formats, return (date_str MM/DD/YYYY, mo, dy, yr) or None."""
    # Format A: as of MM/DD/YYYY
    m = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text)
    if m:
        s = m.group(1)
        mo, dy, yr = map(int, s.split("/"))
        return s, mo, dy, yr

    # Format B: as of Month DD, YYYY  (full or abbreviated)
    m = re.search(
        r"as of\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        text, re.IGNORECASE)
    if m:
        mo = MONTH_MAP_ABBR[m.group(1).lower()[:3]]
        dy, yr = int(m.group(2)), int(m.group(3))
        return f"{mo:02d}/{dy:02d}/{yr}", mo, dy, yr

    # Format C: header "Weekly Report N – Month DD, YYYY"
    m = re.search(
        r"Weekly Report[^-\n]*[-\u2013]\s*(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
        r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        text, re.IGNORECASE)
    if m:
        mo = MONTH_MAP_ABBR[m.group(1).lower()[:3]]
        dy, yr = int(m.group(2)), int(m.group(3))
        return f"{mo:02d}/{dy:02d}/{yr}", mo, dy, yr

    return None

def parse_report(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    result = parse_date_from_text(text)
    if not result: return []
    report_date_str, release_month, dy, release_year = result

    try: week_num = date(release_year, release_month, dy).isocalendar()[1]
    except: week_num = None

    rn = re.search(r"Weekly Report\s+(\d+)", text, re.IGNORECASE)
    report_num = int(rn.group(1)) if rn else None

    rows_out = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td","th"])]
            if len(cells) < 6: continue
            label = cells[0].strip()
            year_m = re.search(r"\b(20\d{2})\b", label)
            mon_m  = re.search(
                r"(january|february|march|april|may|june|july|august|"
                r"september|october|november|december)", label.lower())
            is_total = bool(re.search(r"^\s*total", label, re.IGNORECASE))
            if not (year_m or is_total): continue
            if re.search(r"unfixed|futures based|call cotton|change from|open futures", label.lower()): continue

            s =to_int(cells[1]) if len(cells)>1 else 0
            cs=to_int(cells[2]) if len(cells)>2 else 0
            p =to_int(cells[3]) if len(cells)>3 else 0
            cp=to_int(cells[4]) if len(cells)>4 else 0
            cl=to_int(cells[5]) if len(cells)>5 else 0
            cc=to_int(cells[6]) if len(cells)>6 else 0

            if is_total and not year_m:
                rows_out.append({
                    "Week #":week_num,"Report #":report_num,
                    "Report Date":report_date_str,"Futures Based On":"Totals",
                    "Unfixed Call Sales":s,"Chg Sales":cs,
                    "Unfixed Call Purchases":p,"Chg Purchases":cp,
                    "At Close":cl,"Chg At Close":cc,
                    "Yr":"","Month":"","Old/New":"total",
                    "Report Year":str(release_year) if release_year else "",
                    "_release_year":release_year,
                })
            elif year_m and mon_m:
                cy = int(year_m.group(1))
                cm = MONTH_MAP[mon_m.group(1)]
                rows_out.append({
                    "Week #":week_num,"Report #":report_num,
                    "Report Date":report_date_str,"Futures Based On":label.strip(),
                    "Unfixed Call Sales":s,"Chg Sales":cs,
                    "Unfixed Call Purchases":p,"Chg Purchases":cp,
                    "At Close":cl,"Chg At Close":cc,
                    "Yr":cy,"Month":cm,
                    "Old/New":get_old_new(cy,cm,release_year,release_month) if release_year else "",
                    "Report Year":str(release_year) if release_year else "",
                    "_release_year":release_year,
                })
    return rows_out

# ── URL helpers ───────────────────────────────────────────────────────────────

def get_candidate_urls():
    print("Fetching CFTC index page...")
    try:
        r = requests.get(BASE + BASE_PATH + "index.htm", headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        seen, urls = set(), []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "deaoncal" not in href.lower(): continue
            if href.startswith("http"): full = href
            elif href.startswith("/"): full = BASE + href
            else: full = BASE + BASE_PATH + href
            full = full.split("#")[0]
            if not full.lower().endswith(".html"): full += ".html"
            if full not in seen:
                seen.add(full); urls.append(full)
    except Exception as e:
        print(f"⚠️  Index page error: {e}")
        urls, seen = [], set()

    known_2026 = [
        "deaoncall010226.html","deaoncall010826.html","deaoncall011526.html",
        "deaoncall012226.html","deaoncall012926.html","deaoncall020526.html",
        "deaoncall021226.html","deaoncall021926.html","deaoncall022626.html",
        "deaoncall030526.html","deaoncall030626.html","deaoncall031226.html",
        "deaoncall031926.html","deaoncall032626.html","deaoncall040226.html",
        "deaoncall040926.html","deaoncall041626.html","deaoncall042326.html",
        "deaoncall043026.html","deaoncall050726.html","deaoncall051426.html",
        "deaoncall052126.html","deaoncall052826.html","deaoncall060426.html",
        "deaoncall061126.html","deaoncall061826.html","deaoncall062526.html",
        "deaoncall070226.html","deaoncall070926.html","deaoncall071626.html",
        "deaoncall072326.html","deaoncall073026.html","deaoncall080626.html",
        "deaoncall081326.html","deaoncall082026.html","deaoncall082726.html",
        "deaoncall090326.html","deaoncall091026.html","deaoncall091726.html",
        "deaoncall092426.html","deaoncall100126.html","deaoncall100826.html",
        "deaoncall101526.html","deaoncall102226.html","deaoncall102926.html",
        "deaoncall110526.html","deaoncall111226.html","deaoncall111926.html",
        "deaoncall112626.html","deaoncall120326.html","deaoncall121026.html",
        "deaoncall121726.html","deaoncall122426.html","deaoncall123126.html",
    ]
    for fn in known_2026:
        u = BASE + BASE_PATH + fn
        if u not in seen: urls.append(u)

    print(f"Found {len(urls)} total candidate URLs")
    return urls

# ── CSV helpers ───────────────────────────────────────────────────────────────

def read_existing_dates(csv_path):
    existing = set()
    if not os.path.exists(csv_path): return existing
    for enc in ("utf-8-sig","utf-8","latin-1"):
        try:
            with open(csv_path, newline="", encoding=enc) as f:
                for row in csv.DictReader(f):
                    d = row.get("Report Date","").strip()
                    if d: existing.add(d)
            if existing: break
        except: pass
    print(f"Found {len(existing)} existing report dates in CSV")
    return existing

def read_all_rows(csv_path):
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}"); return [], 0
    fsize = os.path.getsize(csv_path)
    print(f"CSV size: {fsize} bytes")
    if fsize < 100:
        print("⚠️  CSV too small — aborting"); sys.exit(1)
    for enc in ("utf-8-sig","utf-8","latin-1"):
        try:
            with open(csv_path, newline="", encoding=enc) as f:
                rows = [dict(r) for r in csv.DictReader(f)]
            if rows:
                print(f"Read {len(rows)} rows (encoding: {enc})")
                return rows, len(rows)
        except Exception as e:
            print(f"  {enc}: {e}")
    print("⚠️  Could not read CSV — aborting"); sys.exit(1)

def append_rows(csv_path, new_rows):
    existing_rows, rows_before = read_all_rows(csv_path)
    if os.path.exists(csv_path) and rows_before == 0:
        print("⚠️  SAFETY ABORT: file exists but 0 rows"); sys.exit(1)

    for r in new_rows:
        clean = {k:v for k,v in r.items() if not k.startswith("_")}
        if not clean.get("Report Year") and clean.get("Report Date","").count("/")==2:
            clean["Report Year"] = clean["Report Date"].split("/")[2]
        existing_rows.append(clean)

    existing_rows.sort(key=lambda r: (
        datetime.strptime(r.get("Report Date",""), "%m/%d/%Y")
        if r.get("Report Date","") else datetime.min))

    if len(existing_rows) < rows_before:
        print(f"⚠️  SAFETY ABORT: would shrink {rows_before}→{len(existing_rows)}"); sys.exit(1)

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader(); w.writerows(existing_rows)
    os.replace(tmp, csv_path)
    print(f"✅ CSV saved: {rows_before} → {len(existing_rows)} rows (+{len(existing_rows)-rows_before})")

# ── PDF generation ────────────────────────────────────────────────────────────

def safe_text(t):
    """Replace unicode chars that latin-1 fonts can't handle."""
    return (str(t)
        .replace('—', ' - ')   # em dash
        .replace('–', ' - ')   # en dash
        .replace('−', ' - ')   # minus sign
        .replace('’', "'")     # right single quote
        .replace('‘', "'")     # left single quote
        .replace('“', '"')     # left double quote
        .replace('”', '"')     # right double quote
        .replace('▲', '^')     # triangle up
        .replace('▼', 'v')     # triangle down
        .replace('●', 'o')     # circle
    )


def generate_pdf(new_rows, all_rows_for_charts):
    """
    Renders cotton_oncall/index.html via Playwright and returns PDF bytes.
    Injects the full CSV data so the page renders with the latest data.
    Falls back to None if Playwright is unavailable.
    """
    import subprocess, sys, pathlib, csv, io

    # Find the HTML file relative to this script
    repo_root  = pathlib.Path(__file__).parent.parent
    html_file  = repo_root / "cotton_oncall" / "index.html"
    csv_file   = repo_root / "cotton_oncall" / "cotton_oncall_data.csv"

    if not html_file.exists():
        print(f"HTML file not found: {html_file}")
        return None
    if not csv_file.exists():
        print(f"CSV file not found: {csv_file}")
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed — skipping PDF generation")
        return None

    # Read CSV text to inject into the page
    csv_text = csv_file.read_text(encoding="utf-8-sig")
    report_date = new_rows[0].get("Report Date", "") if new_rows else ""
    print(f"Rendering page for report date: {report_date}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-web-security", "--allow-file-access-from-files"
            ])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)[:100]))

            page.goto(f"file://{html_file}", wait_until="load")
            page.wait_for_timeout(1000)

            # Bypass password screen — inject auth token into sessionStorage
            page.evaluate("() => { try { sessionStorage.setItem('bmr_auth','1'); } catch(e){} }")
            # Remove the overlay if it rendered before we injected
            page.evaluate("""() => {
                var o = document.getElementById('auth-overlay');
                if(o) o.remove();
            }""")
            page.wait_for_timeout(500)

            # Inject the CSV and trigger the page to render
            js_extract = """
                () => typeof parseCSV === 'function'
                    || typeof ALL_DATA !== 'undefined'
            """
            # Inject boot logic: set CSV as if fetched, then re-run boot
            safe_csv = csv_text.replace('`', '\\`').replace('${', '\\${')
            inject_js = (
                "() => {"
                "  var _orig = window.fetch;"
                "  window.fetch = function(url) {"
                "    if(url && url.toString().includes('cotton_oncall_data')) {"
                "      return Promise.resolve({"
                "        ok: true,"
                "        text: function() { return Promise.resolve(`" + safe_csv + "`); }"
                "      });"
                "    }"
                "    return _orig ? _orig(url) : Promise.reject('no fetch');"
                "  };"
                "}"
            )
            page.evaluate(inject_js)

            # Re-run boot with our intercepted fetch
            page.evaluate("() => { if(typeof boot === 'function') boot(); }")
            page.wait_for_timeout(4000)

            # Wait for data to load (badge shows OK)
            try:
                page.wait_for_function(
                    "() => { var b = document.getElementById('badge'); "
                    "return b && b.className === 'ok'; }",
                    timeout=10000
                )
            except Exception:
                print("Warning: badge not OK after 10s — rendering anyway")

            page.wait_for_timeout(2000)

            if errors:
                print(f"Page errors: {errors[:3]}")

            pdf_bytes = page.pdf(
                format="A4",
                landscape=True,
                print_background=True,
                margin={"top": "8mm", "bottom": "8mm",
                        "left": "8mm", "right": "8mm"}
            )
            browser.close()
            print(f"PDF generated: {len(pdf_bytes)//1024}KB")
            return pdf_bytes

    except Exception as e:
        print(f"Playwright PDF generation failed: {e}")
        return None


def send_email(pdf_bytes, report_date):
    smtp_host = os.environ.get('SMTP_HOST','')
    smtp_port = int(os.environ.get('SMTP_PORT', 587))
    smtp_user = os.environ.get('SMTP_USER','')
    smtp_pass = os.environ.get('SMTP_PASS','')
    email_from= os.environ.get('EMAIL_FROM','')
    email_to  = os.environ.get('EMAIL_TO','')

    if not all([smtp_host, smtp_user, smtp_pass, email_from, email_to]):
        print("⚠️  Email env vars not set — skipping email"); return

    recipients = [e.strip() for e in email_to.split(',') if e.strip()]
    fname = f"cotton_oncall_{report_date.replace('/','_')}.pdf"

    msg = MIMEMultipart()
    msg['From']    = email_from
    msg['To']      = ', '.join(recipients)
    msg['Subject'] = f"Cotton On-Call Report - {report_date}"
    msg.attach(MIMEText(
        f"Please find attached the CFTC Cotton On-Call report for {report_date}.\n\n"
        f"Dashboard: https://bmr-com.github.io/usd-export-sales-email/cotton_oncall/", 'plain'))

    part = MIMEBase('application','pdf')
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
    msg.attach(part)

    try:
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=_ctx, timeout=30) as srv:
                srv.login(smtp_user, smtp_pass)
                srv.sendmail(email_from, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
                srv.ehlo()
                srv.starttls(context=_ctx)
                srv.login(smtp_user, smtp_pass)
                srv.sendmail(email_from, recipients, msg.as_string())
        print(f"✅ Email sent to {', '.join(recipients)}")
    except Exception as e:
        print(f"⚠️  Email failed: {e}")

# ── Core scrape logic ─────────────────────────────────────────────────────────

def check_for_new_reports(existing_dates, new_rows):
    """Check live page + archive. Returns number of new reports found."""
    found = 0

    # Step 1: live main page
    live_url = f"https://www.cftc.gov/MarketReports/CottonOnCall/index.htm?_={int(time.time())}"
    print(f"Checking live page...")
    try:
        r = requests.get(live_url, headers=HEADERS_NOCACHE, timeout=15)
        if r.status_code == 200 and "Unfixed" in r.text:
            rows = parse_report(r.text)
            if rows:
                rdate = rows[0]["Report Date"]
                if rdate not in existing_dates:
                    new_rows.extend(rows)
                    existing_dates.add(rdate)
                    found += 1
                    print(f"✅ LIVE PAGE: {rdate} ({len(rows)} rows)")
                else:
                    print(f"⏭️  Live page already in CSV: {rdate}")
    except Exception as e:
        print(f"⚠️  Live page: {e}")

    # Step 2: archive last 60 days
    cutoff = datetime.now() - timedelta(days=60)
    all_urls = get_candidate_urls()
    recent = []
    for url in all_urls:
        fn = url.split("/")[-1].replace(".html","")
        digits = re.sub(r"[^0-9]","",fn)
        for fmt, dlen in [("%m%d%y",6),("%m%d%Y",8)]:
            if len(digits) == dlen:
                try:
                    if datetime.strptime(digits, fmt) >= cutoff:
                        recent.append(url); break
                except: pass

    print(f"Checking {len(recent)} recent archive URLs")
    for url in recent:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200 or "Unfixed" not in r.text: continue
            rows = parse_report(r.text)
            if not rows: continue
            rdate = rows[0]["Report Date"]
            if rdate in existing_dates:
                print(f"⏭️  Already have {rdate}"); continue
            new_rows.extend(rows)
            existing_dates.add(rdate)
            found += 1
            print(f"✅ NEW: {url.split('/')[-1]} → {rdate} ({len(rows)} rows)")
        except Exception as e:
            print(f"⚠️  {url.split('/')[-1]}: {e}")
        time.sleep(0.3)

    return found

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    force_email = os.environ.get('FORCE_EMAIL','').lower() in ('true','1','yes')

    existing_dates = read_existing_dates(CSV_PATH)
    new_rows = []
    found_total = 0

    if force_email:
        # Skip retry loop — just do one quick check, then use existing data for email
        print("\nFORCE_EMAIL mode — single check, no retry loop")
        found_total = check_for_new_reports(existing_dates, new_rows)
        print(f"New reports found: {found_total}")
    else:
        for attempt in range(MAX_RETRIES):
            print(f"\n--- Attempt {attempt+1}/{MAX_RETRIES} ---")
            found = check_for_new_reports(existing_dates, new_rows)
            found_total += found
            if found_total > 0:
                print(f"✅ New data found on attempt {attempt+1}")
                break
            if attempt < MAX_RETRIES - 1:
                print(f"No new report yet — waiting 5 minutes before retry...")
                time.sleep(RETRY_INTERVAL)

    print(f"\nTotal new reports: {found_total} | New rows: {len(new_rows)}")

    if not new_rows and not force_email:
        print("Nothing to add — exiting")
        sys.exit(0)

    if new_rows:
        # Save CSV and update web file
        append_rows(CSV_PATH, new_rows)
        import shutil, pathlib
        pathlib.Path(os.path.dirname(WEB_CSV)).mkdir(parents=True, exist_ok=True)
        shutil.copy2(CSV_PATH, WEB_CSV)
        print(f"✓ Web CSV updated: {WEB_CSV}")
    else:
        # force_email with no new data — just ensure web CSV exists
        import shutil, pathlib
        if os.path.exists(CSV_PATH):
            pathlib.Path(os.path.dirname(WEB_CSV)).mkdir(parents=True, exist_ok=True)
            shutil.copy2(CSV_PATH, WEB_CSV)
            print("✓ Web CSV refreshed (no new data)")

    # Generate PDF from web page and send email
    print("Generating PDF...")
    all_rows, _ = read_all_rows(CSV_PATH)
    # Use most recent report date for the email subject
    report_date = (new_rows[0].get("Report Date","") if new_rows
                   else next((r.get("Report Date","") for r in reversed(all_rows)
                              if r.get("Report Date")), "latest"))
    pdf_bytes = generate_pdf(new_rows if new_rows else all_rows[-20:], all_rows)
    if pdf_bytes:
        send_email(pdf_bytes, report_date)
    else:
        print("PDF generation failed")

    print("✅ Done")

if __name__ == "__main__":
    main()
