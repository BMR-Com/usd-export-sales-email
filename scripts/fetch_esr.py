#!/usr/bin/env python3
"""
fetch_esr.py — weekly USDA FAS Export Sales (ESR) data for the commodities on the dashboard.

Writes data/esr/<code>.csv (same columns as cotton_history_1404.csv) and data/esr/meta.json.
Only the current and previous marketing years are re-downloaded each week; older years are kept.
Upland cotton (1404) keeps its full history in cotton_history_1404.csv (scripts/fetch_cotton_history.py).
"""
import csv, json, os, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

API_KEY = os.getenv("FAS_API_KEY") or "p5rJTNhEZbSmIPVyRxEixuZrVfgKraUAhCNo8Kmg"
BASE = "https://api.fas.usda.gov/api/esr"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "esr"
FIRST_YEAR = 2011
COMMODITIES = {1301: "Cotton, American Pima", 401: "Corn", 801: "Soybeans", 107: "All Wheat", 901: "Soybean Cake & Meal",
               902: "Soybean Oil", 701: "Sorghum", 1505: "All Rice"}
FIELDS = ["weekEndingDate", "marketYear", "commodityCode", "countryCode", "countryName", "grossNewSales", "currentMYNetSales",
          "weeklyExports", "accumulatedExports", "outstandingSales", "currentMYTotalCommitment", "nextMYNetSales", "nextMYOutstandingSales"]


def get(path, retries=4):
    url = f"{BASE}/{path}"
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"X-Api-Key": API_KEY, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            time.sleep(30 if e.code == 429 else 5)
        except Exception as e:
            print("  retry", a + 1, path, e); time.sleep(5)
    raise RuntimeError(f"failed: {path}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"commodities": {}, "countries": {}, "units": {}}
    try:
        cm = get("commodities"); meta["commodities"] = {str(c["commodityCode"]): c for c in cm}
        meta["countries"] = {str(c["countryCode"]): c.get("countryName", "").strip() for c in get("countries")}
        meta["units"] = {str(u["unitId"]): u.get("unitNames", "").strip() for u in get("unitsOfMeasure")}
    except Exception as e:
        print("metadata unavailable:", e)
    names = meta["countries"]
    now = datetime.utcnow().year
    for code, label in COMMODITIES.items():
        f = OUT / f"{code}.csv"
        old = {}
        if f.exists():
            for r in csv.DictReader(open(f)):
                old.setdefault(int(r["marketYear"]), []).append(r)
        years = range(FIRST_YEAR, now + 2)
        for y in years:
            if y in old and y < now:          # settled years are kept as is
                continue
            data = get(f"exports/commodityCode/{code}/allCountries/marketYear/{y}")
            if not data:
                continue
            rows = []
            for d in data:
                cc = str(d.get("countryCode", ""))
                row = {k: d.get(k, "") for k in FIELDS}
                row["weekEndingDate"] = str(row["weekEndingDate"]).split("T")[0]
                row["marketYear"] = y; row["commodityCode"] = code
                row["countryName"] = names.get(cc, row.get("countryName") or cc)
                rows.append(row)
            old[y] = rows
            print(f"{label} MY{y}: {len(rows)} rows")
            time.sleep(0.5)
        with open(f, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS); w.writeheader()
            for y in sorted(old):
                w.writerows(old[y])
    (OUT / "meta.json").write_text(json.dumps({"fetched": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                                               "commodities": {k: v for k, v in meta["commodities"].items() if int(k) in COMMODITIES or k == "1404"},
                                               "units": meta["units"], "countries": names}, indent=1))


if __name__ == "__main__":
    main()
