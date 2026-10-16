#!/usr/bin/env python3
"""Exit 0 and print 'update' when a newer export-sales week is due; print 'current' when the archive already has it.
USDA publishes Thursday 8:30 ET (Friday or later in holiday weeks) for the week ending the previous Thursday."""
import os, sys
from datetime import date, timedelta
import pandas as pd

latest = pd.to_datetime(pd.read_csv("cotton_history_1404.csv", usecols=["weekEndingDate"]).weekEndingDate).max().date()
today = date.today()
last_thu = today - timedelta(days=(today.weekday() - 3) % 7)       # most recent Thursday (today if Thursday)
expected = last_thu - timedelta(days=7)                             # week covered by this week's report
force = os.getenv("FORCE") == "true"
state = "update" if force or latest < expected else "current"
print(f"archive latest {latest}, expected {expected}: {state}")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
    f.write(f"state={state}\n")
