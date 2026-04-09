# scripts/test_email.py - Quick email test
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from scrape_cotton_oncall import send_email, now

# Load minimal env
os.environ.setdefault('SMTP_HOST', os.getenv('SMTP_HOST'))
os.environ.setdefault('SMTP_PORT', os.getenv('SMTP_PORT', '465'))
os.environ.setdefault('SMTP_USER', os.getenv('SMTP_USER'))
os.environ.setdefault('SMTP_PASS', os.getenv('SMTP_PASS'))
os.environ.setdefault('EMAIL_FROM', os.getenv('EMAIL_FROM'))
os.environ.setdefault('EMAIL_TO', os.getenv('EMAIL_TO'))

# Create dummy PDF
pdf_bytes = b'%PDF-1.4 test content for sample email'

print(f"[{now()}] Sending test email...")
send_email(pdf_bytes, "04/09/2026")
print(f"[{now()}] Test complete")
