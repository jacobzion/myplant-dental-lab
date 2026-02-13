from dotenv import load_dotenv
load_dotenv()  # ✅ 반드시 먼저!

import os
import json
import smtplib
from datetime import date
from email.mime.text import MIMEText
from email.utils import formatdate
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

app = FastAPI()

# ✅ load_dotenv() 이후에 읽어야 함
FROM_EMAIL = os.getenv("FROM_EMAIL")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ROUTING_FILE = os.getenv("ROUTING_FILE", "./routing.json")

CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")

if CORS_ALLOW_ORIGINS == "*":
    allow_origins = ["*"]
else:
    # Netlify 주소를 포함하도록 설정
    allow_origins = [
        "https://axion-dental-pickup-app.netlify.app",
        "http://localhost:3000" # 로컬 테스트용
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Request model
# =========================
class PickupRequest(BaseModel):
    clinic_code: str
    clinic_name: str
    clinic_phone: str | None = None
    address1: str
    city: str
    state: str
    zip: str
    pickup_date: date
    time_window: str | None = None
    notes: str | None = None
    contact_email: EmailStr | None = None

# =========================
# Helpers
# =========================
def _norm(s: str | None) -> str:
    return (s or "").strip().upper()

def load_routing() -> dict:
    try:
        with open(ROUTING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(500, f"Routing config load failed: {e}")

def pick_recipients(p: PickupRequest, routing: dict) -> tuple[list[str], str, bool]:
    """
    returns: (recipients, routed_by, is_verified)
    Verified = clinic_code matched clinic_routes.
    """
    code = (p.clinic_code or "").strip()
    z = (p.zip or "").strip()
    city = _norm(p.city)

    # 1) Clinic code route (most specific, verified)
    for r in routing.get("clinic_routes", []):
        if (r.get("clinic_code") or "").strip() == code:
            return r.get("recipients", []), f"clinic_code:{code}", True

    # Not verified by clinic_code
    is_verified = False

    # 2) ZIP route
    for r in routing.get("zip_routes", []):
        if z in set(r.get("zips", [])):
            return r.get("recipients", []), f"zip:{z}", is_verified

    # 3) City route
    for r in routing.get("city_routes", []):
        cities = [c.strip().upper() for c in r.get("cities", [])]
        if city in set(cities):
            return r.get("recipients", []), f"city:{city}", is_verified

    # 4) Default
    return routing.get("default_recipients", []), "default", is_verified

def build_email(p: PickupRequest, routed_by: str, is_verified: bool) -> tuple[str, str]:
    prefix = "" if is_verified else "[UNVERIFIED] "
    subject = f"{prefix}[Pickup Request] {p.clinic_name} ({p.city} {p.zip}) - {p.pickup_date}"

    unverified_line = "" if is_verified else (
        f"⚠ Unverified clinic_code submitted: {p.clinic_code}\n"
        f"⚠ Action: Sent to default dispatch + admin CC for verification.\n\n"
    )

    body = (
        f"{unverified_line}"
        f"New pickup request received.\n\n"
        f"Clinic Code: {p.clinic_code}\n"
        f"Clinic: {p.clinic_name}\n"
        f"Phone: {p.clinic_phone or '-'}\n"
        f"Address: {p.address1}, {p.city}, {p.state} {p.zip}\n"
        f"Pickup Date: {p.pickup_date}\n"
        f"Time Window: {p.time_window or '-'}\n"
        f"Contact Email: {p.contact_email or '-'}\n"
        f"Notes: {p.notes or '-'}\n\n"
        f"Routing: {routed_by}\n"
    )
    return subject, body

def send_email(to_emails: list[str], cc_emails: list[str], subject: str, body: str):
    if not all([FROM_EMAIL, SMTP_HOST, SMTP_USER, SMTP_PASS]):
        raise HTTPException(500, "SMTP settings are missing in .env")

    if not to_emails:
        raise HTTPException(500, "No recipients resolved (default_recipients empty?)")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = ", ".join(to_emails)
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg["Date"] = formatdate(localtime=True)

    all_recipients = to_emails + (cc_emails or [])

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(FROM_EMAIL, all_recipients, msg.as_string())
    except Exception as e:
        raise HTTPException(502, f"Email send failed: {e}")

# =========================
# API
# =========================
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/pickup-request")
def pickup_request(p: PickupRequest):
    routing = load_routing()

    recipients, routed_by, is_verified = pick_recipients(p, routing)
    subject, body = build_email(p, routed_by, is_verified)

    # ✅ 핵심 요구사항:
    # UNVERIFIED면: To=default_recipients, CC=admin_cc
    cc_list: list[str] = []
    if not is_verified:
        recipients = routing.get("default_recipients", [])
        cc_list = routing.get("admin_cc", [])

    send_email(recipients, cc_list, subject, body)

    return {
        "ok": True,
        "verified": is_verified,
        "routed_by": routed_by,
        "to": recipients,
        "cc": cc_list
    }
