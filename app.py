#!/usr/bin/env python3
"""Link Exchange Platform — free link exchange directory for The Web Hospitality.

Business model:
  - FREE for users: list their site, find link-exchange partners, contact them
  - Monetization: every listing has a "Guest Post" path -> routes to your paid
    guest post service (TWH already sells guest posts)
  - Growth: each member site adds a backlink to TWH (reciprocal), building DR
  - Email capture on submit -> newsletter / guest post leads

Tech: Flask + SQLite, single-file, deploy anywhere (Render free tier ok).
"""
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# load .env if present (SMTP creds etc.)
try:
    with open(Path(__file__).resolve().parent / ".env") as _envf:
        for _line in _envf:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
except FileNotFoundError:
    pass

import requests
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

try:
    from niche_data import get_niche_data
except ImportError:
    def get_niche_data(slug):
        pretty = slug.replace("-", " ").title()
        return {
            "name": pretty,
            "kw": f"link exchange in {pretty.lower()}",
            "intro": f"A link exchange in {pretty} connects site owners who share audiences, helping both sides earn relevant authority links.",
            "faqs": [
                (f"Do {pretty} link exchanges work?", f"Yes — a link exchange in {pretty} sends targeted referral traffic and topical authority."),
                (f"Who should {pretty} sites exchange with?", "Sites in the same niche with similar Domain Rating."),
                (f"How many {pretty} exchanges is safe?", "Keep reciprocal links under 10% of your profile."),
            ],
        }

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "linkexchange.db"
SITE_URL = "https://www.thewebhospitality.com"
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hello@thewebhospitality.com")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "twh@linkexchange")

from functools import wraps


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
            return app.response_class(
                "Unauthorized", status=401,
                headers={"WWW-Authenticate": 'Basic realm="TWH Admin"'}
            )
        return f(*args, **kwargs)
    return wrapper

# ---------- Email config (SMTP) ----------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER or "no-reply@thewebhospitality.com")


def send_mail(to_email, subject, html_body):
    """Send email via SMTP. Returns (ok, error). Skips silently if SMTP not configured."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        return False, "SMTP not configured (set SMTP_HOST/SMTP_USER/SMTP_PASS)"
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            server.ehlo()
            if SMTP_PORT == 587:
                server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(MAIL_FROM, [to_email], msg.as_string())
        server.quit()
        return True, ""
    except Exception as e:
        return False, str(e)

NICHES = [
    "All", "Astrology / Esotericism", "Business / Finance", "City Portals",
    "Construction / Repair", "Cooking", "Country House", "Cryptocurrencies",
    "Culture / Art", "Ecology / Conservation", "Education / Science",
    "Electronics / Technology", "Entertainment / Hobbies", "Fashion / Beauty",
    "Furniture / Interior", "Health / Medicine", "Home / Family", "Internet",
    "Law / Jurisprudence", "Lifestyle", "Logistics / Transportation",
    "Manufacturing / Agriculture", "Marketing", "Media / News",
    "Mobile Technology", "Music / Cinema", "Other", "PC / Video Games",
    "Pets", "Photography / Videography", "Psychology / Development",
    "Real Estate", "Religion", "SaaS", "SEO / Marketing",
    "Society / Politics", "Software / PC", "Sports / Nutrition",
    "Tech / SaaS", "Technologies", "Tourism / Travel", "Web Design",
    "Web Development", "Shopping / Coupons", "Work / Jobs",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name TEXT NOT NULL,
    site_url TEXT NOT NULL,
    email TEXT NOT NULL,
    niche TEXT NOT NULL,
    description TEXT DEFAULT '',
    dr INTEGER DEFAULT 0,
    da INTEGER DEFAULT 0,
    traffic TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',   -- pending | active | rejected
    token TEXT DEFAULT '',
    password TEXT DEFAULT '',
    verified INTEGER DEFAULT 0,
    verify_token TEXT DEFAULT '',
    verify_expires TEXT DEFAULT '',
    owner_verified INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_site_id INTEGER,
    to_site_id INTEGER,
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',   -- pending | contacted | done
    created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
        # migration: ensure traffic + token + password + verified columns exist (older DBs)
        cols = [r[1] for r in g.db.execute("PRAGMA table_info(sites)").fetchall()]
        if "traffic" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN traffic TEXT DEFAULT ''")
        if "token" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN token TEXT DEFAULT ''")
        if "password" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN password TEXT DEFAULT ''")
        if "verified" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN verified INTEGER DEFAULT 0")
            g.db.execute("ALTER TABLE sites ADD COLUMN verify_token TEXT DEFAULT ''")
            g.db.execute("ALTER TABLE sites ADD COLUMN verify_expires TEXT DEFAULT ''")
        elif "verify_token" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN verify_token TEXT DEFAULT ''")
        if "owner_verified" not in cols:
            g.db.execute("ALTER TABLE sites ADD COLUMN owner_verified INTEGER DEFAULT 0")
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- helpers ----------

def normalize_url(url: str) -> str:
    url = url.strip().lower()
    url = url.replace("http://", "").replace("https://", "").strip("/")
    if url.startswith("www."):
        url = url[4:]
    return url


def fetch_dr_da(domain: str) -> tuple[int, int]:
    """Best-effort DR/DA via free sources. Returns (dr, da) with graceful fallback."""
    dr = da = 0
    # Ahrefs free public endpoint (needs AHREFS_API_KEY)
    key = os.environ.get("AHREFS_API_KEY", "")
    if key:
        try:
            r = requests.get(
                "https://api.ahrefs.com/v3/public/domain-rating-free",
                params={"target": domain}, headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            data = r.json()
            dr = int(round(float(data["domain_rating"]["domain_rating"]))) if r.ok else 0
        except Exception:
            pass
    return dr, da


def validate_site(site_url: str, email: str) -> tuple[bool, str]:
    if not re.match(r"^[a-z0-9\-\.]+\.[a-z]{2,}$", site_url):
        return False, "Invalid domain URL"
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return False, "Invalid email address"
    # must be reachable
    try:
        r = requests.get(f"https://{site_url}", timeout=12,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            return False, f"Site not reachable (HTTP {r.status_code})"
    except Exception:
        return False, "Site not reachable (connection failed)"
    return True, ""


# ---------- pages ----------

@app.route("/")
def index():
    db = get_db()
    sites = db.execute(
        "SELECT * FROM sites WHERE status='active' ORDER BY owner_verified DESC, dr DESC").fetchall()
    # count of registered exchangers per domain (any status except rejected)
    exch_counts = {}
    for row in db.execute(
            "SELECT site_url, COUNT(*) c FROM sites WHERE status IN ('active','pending') GROUP BY site_url").fetchall():
        exch_counts[row["site_url"]] = row["c"]
    return render_template("index.html", sites=sites, niches=NICHES,
                           exch_counts=exch_counts, site_url=SITE_URL)


@app.route("/submit", methods=["GET", "POST"])
def submit():
    db = get_db()
    msg, ok = "", False
    email = ""
    if request.method == "POST":
        f = request.form
        site_url = normalize_url(f.get("site_url", ""))
        email = f.get("email", "").strip().lower()
        valid, err = validate_site(site_url, email)
        if not valid:
            msg = f"❌ {err}"
        else:
            # ---- ownership check: email domain == site domain? ----
            site_domain = site_url.split("/")[0].lower()
            email_domain = email.split("@")[-1].lower() if "@" in email else ""
            owner_verified = 1 if email_domain == site_domain else 0

            # ---- same site: prefer verified owner, others can still apply ----
            existing_active = db.execute(
                "SELECT * FROM sites WHERE site_url=? AND status='active' ORDER BY id DESC LIMIT 1",
                (site_url,)).fetchone()
            if existing_active:
                if owner_verified:
                    # owner claims their site -> demote unverified listing, go live
                    db.execute(
                        "UPDATE sites SET status='rejected' WHERE site_url=? AND status='active' AND owner_verified=0",
                        (site_url,))
                    new_status = "active"
                else:
                    new_status = "pending"
            else:
                new_status = "pending"

            # multi-niche: select 3-5 niches (getlist returns list)
            niches = [n for n in f.getlist("niche") if n and n != "All"]
            niches = list(dict.fromkeys(niches))  # dedupe preserve order
            if len(niches) < 1:
                msg = "❌ Please select at least 1 niche."
                ok = False
            elif len(niches) > 5:
                msg = "❌ Please select at most 5 niches."
                ok = False
            else:
                # DR + DA mandatory
                try:
                    dr = int(f.get("dr", "").strip())
                    da = int(f.get("da", "").strip())
                except ValueError:
                    dr = da = None
                if dr is None or da is None:
                    msg = "❌ DR and DA are mandatory. Please enter both values (0-100)."
                    ok = False
                elif not (0 <= dr <= 100 and 0 <= da <= 100):
                    msg = "❌ DR and DA must be between 0 and 100."
                    ok = False
                else:
                    niche_str = ", ".join(niches)
                    token = secrets.token_urlsafe(16)
                    password = secrets.token_urlsafe(6)  # e.g. "xY3kPq_RsT"
                    verify_token = secrets.token_urlsafe(24)
                    verify_expires = (datetime.utcnow() +
                                      timedelta(hours=24)).isoformat()
                    db.execute(
                        "INSERT INTO sites (site_name, site_url, email, niche, description, dr, da, traffic, status, token, password, verified, verify_token, verify_expires, owner_verified, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?, ?, ?, ?, 0, ?, ?, ?, ?)",
                        (f.get("site_name", "").strip()[:60], site_url, email,
                         niche_str, f.get("description", "").strip()[:200],
                         min(dr, 100), min(da, 100),
                         f.get("traffic", "").strip()[:60],
                         new_status, token, password, verify_token, verify_expires,
                         owner_verified, datetime.utcnow().isoformat()))
                    db.commit()
                    if new_status == "active":
                        msg = ("✅ Site submitted & LIVE! Your email matches the site domain — "
                               "you're verified as the owner. Check your inbox for the verification link.")
                    elif owner_verified:
                        msg = ("✅ Site submitted! You're verified as the owner (email domain matches). "
                               "Our team will approve your listing shortly. Check your inbox for the verification link.")
                    elif existing_active:
                        msg = ("✅ Site submitted for review! Note: this site already has a verified owner — "
                               "your listing will be reviewed before going live. Check your inbox for the verification link.")
                    else:
                        msg = ("✅ Site submitted! Your email domain doesn't match the website domain, so "
                               "our team will verify ownership manually. Check your inbox for the verification link.")
                    ok = True
                    # verification email: login + verify link only (no site details)
                    if ok:
                        verify_link = f"{SITE_URL}/link-exchange/verify/{verify_token}"
                        if email:
                            send_mail(
                                email,
                                "✅ Verify your email – TWH Link Exchange",
                                f"""<!DOCTYPE html>
    <html><body style="margin:0;padding:0;background:#f4f6fb;font-family:Arial,sans-serif">
    <center style="width:100%">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f4f6fb;padding:24px 0">
    <tr><td align="center">
    <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;width:100%">
      <tr>
        <td align="center" style="background:linear-gradient(135deg,#1a3a8f 0%,#2f7cf6 55%,#6c5ce7 100%);border-radius:16px 16px 0 0;padding:32px 24px">
          <div style="font-size:40px;line-height:1">📧</div>
          <h1 style="color:#ffffff;margin:12px 0 6px;font-size:22px;font-weight:800;font-family:Arial,sans-serif">Verify Your Email</h1>
          <p style="color:rgba(255,255,255,.9);margin:0;font-size:14px;font-family:Arial,sans-serif">TWH Link Exchange</p>
        </td>
      </tr>
      <tr>
        <td style="background:#ffffff;border-radius:0 0 16px 16px;padding:28px 24px">
          <p style="font-size:15px;color:#0f1b33;line-height:1.6;margin:0 0 16px;font-family:Arial,sans-serif">Hi there,</p>
          <p style="font-size:14px;color:#3a4a63;line-height:1.7;margin:0 0 20px;font-family:Arial,sans-serif">
            Click the link below to verify your email and start adding your websites to the exchange.
            The link is valid for <b>24 hours</b>.
          </p>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:20px">
            <tr><td align="center">
              <a href="{verify_link}" style="display:inline-block;background:linear-gradient(135deg,#2f7cf6,#6c5ce7);color:#ffffff;text-decoration:none;padding:14px 36px;border-radius:50px;font-size:15px;font-weight:700;font-family:Arial,sans-serif">Verify my email</a>
            </td></tr>
          </table>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f8ff;border:1px solid #e3ebff;border-radius:12px;margin-bottom:20px">
            <tr><td style="padding:16px 20px">
              <div style="font-size:13px;color:#5a6b85;line-height:1.7;margin-bottom:8px;font-family:Arial,sans-serif">If the button doesn't work, copy this link:</div>
              <div style="font-size:12px;color:#2f7cf6;word-break:break-all;font-family:monospace;line-height:1.6">{verify_link}</div>
            </td></tr>
          </table>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e3e8f2;border-radius:12px;margin-bottom:20px">
            <tr><td style="background:#f0f7ff;padding:12px 20px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:#1a3a8f;border-bottom:1px solid #e3e8f2;border-radius:12px 12px 0 0;font-family:Arial,sans-serif">🔑 Your Login Details</td></tr>
            <tr><td style="padding:12px 20px 2px;color:#5a6b85;font-weight:600;font-size:13px;font-family:Arial,sans-serif">Username (email)</td></tr>
            <tr><td style="padding:2px 20px 12px;color:#0f1b33;font-weight:700;font-size:14px;font-family:Arial,sans-serif">{email}</td></tr>
            <tr style="background:#fafbfe"><td style="padding:12px 20px 2px;color:#5a6b85;font-weight:600;font-size:13px;font-family:Arial,sans-serif">Password</td></tr>
            <tr style="background:#fafbfe"><td style="padding:2px 20px 14px;color:#0f1b33;font-weight:700;font-size:14px;font-family:Arial,sans-serif">{password}</td></tr>
          </table>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:12px">
            <tr><td align="center">
              <a href="{SITE_URL}/link-exchange/login" style="display:inline-block;background:#0f1b33;color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:50px;font-size:15px;font-weight:700;font-family:Arial,sans-serif">Login to Your Account →</a>
            </td></tr>
          </table>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-top:1px solid #eef1f7">
            <tr><td align="center" style="padding-top:16px">
              <p style="font-size:12px;color:#8a97ad;margin:0;font-family:Arial,sans-serif">Sent via <b style="color:#2f7cf6">TWH Link Exchange Directory</b></p>
            </td></tr>
          </table>
        </td>
      </tr>
    </table>
    </td></tr>
    </table>
    </center>
    </body></html>""")
    return render_template("submit.html", msg=msg, ok=ok, niches=NICHES,
                            site_url=SITE_URL,
                            site_email_hint=email if email else "")


@app.route("/status/<token>")
def status_page(token):
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE token=?", (token,)).fetchone()
    if not site:
        return render_template("status.html", found=False, site=None, site_url=SITE_URL)
    return render_template("status.html", found=True, site=site, site_url=SITE_URL)


@app.route("/verify/<verify_token>")
def verify(verify_token):
    db = get_db()
    site = db.execute(
        "SELECT * FROM sites WHERE verify_token=?",
        (verify_token,)).fetchone()
    if not site:
        return render_template("verify.html", ok=False, reason="invalid", site_url=SITE_URL)
    # check expiry
    try:
        exp = datetime.fromisoformat(site["verify_expires"])
        if datetime.utcnow() > exp:
            return render_template("verify.html", ok=False, reason="expired", site_url=SITE_URL)
    except (ValueError, TypeError):
        pass
    if site["verified"]:
        return render_template("verify.html", ok=True, reason="already", site=site, site_url=SITE_URL)
    db.execute("UPDATE sites SET verified=1 WHERE id=?", (site["id"],))
    db.commit()
    return render_template("verify.html", ok=True, reason="done", site=site, site_url=SITE_URL)


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    msg, ok = "", False
    if request.method == "POST":
        email_in = request.form.get("email", "").strip().lower()
        pass_in = request.form.get("password", "").strip()
        site = db.execute(
            "SELECT * FROM sites WHERE email=? AND password=? ORDER BY id DESC LIMIT 1",
            (email_in, pass_in)).fetchone()
        if site:
            session["user_email"] = site["email"]
            session["user_site_id"] = site["id"]
            return redirect(url_for("dashboard"))
        msg = "❌ Invalid email or password."
    return render_template("login.html", msg=msg, ok=ok, site_url=SITE_URL)


@app.route("/dashboard")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))
    db = get_db()
    sites = db.execute(
        "SELECT * FROM sites WHERE email=? ORDER BY id DESC",
        (session["user_email"],)).fetchall()
    return render_template("dashboard.html", sites=sites, site_url=SITE_URL)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/exchange/<int:site_id>", methods=["GET", "POST"])
def exchange(site_id):
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        return "Site not found", 404
    msg = ""
    if request.method == "POST":
        f = request.form
        your_name = f.get("your_name", "").strip()
        your_email = f.get("your_email", "").strip()
        your_site = f.get("your_site", "").strip()
        message = f.get("message", "").strip()
        db.execute(
            "INSERT INTO exchanges (from_site_id, to_site_id, message, status, created_at)"
            " VALUES (?,?,?, 'pending', ?)",
            (0, site_id, f"{your_name} | {your_email} | {your_site}: {message}",
             datetime.utcnow().isoformat()))
        db.commit()
        # email notification to site owner
        mail_ok, mail_err = False, ""
        if site["email"] and your_email:
            mail_ok, mail_err = send_mail(
                site["email"],
                f"🔗 New Link Exchange Request – {site['site_name']}",
                f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f6fb;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<center style="width:100%">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f4f6fb;padding:24px 0">
<tr><td align="center">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;width:100%">

  <!-- Header -->
  <tr>
    <td align="center" style="background:linear-gradient(135deg,#1a3a8f 0%,#2f7cf6 55%,#6c5ce7 100%);border-radius:16px 16px 0 0;padding:32px 24px">
      <div style="font-size:44px;line-height:1">🔗</div>
      <h1 style="color:#ffffff;margin:12px 0 6px;font-size:22px;font-weight:800;font-family:Arial,sans-serif">New Link Exchange Request</h1>
      <p style="color:rgba(255,255,255,.9);margin:0;font-size:14px;font-family:Arial,sans-serif">Someone wants to swap backlinks with <b style="color:#ffffff">{site['site_name']}</b></p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="background:#ffffff;border-radius:0 0 16px 16px;padding:28px 24px;box-shadow:0 8px 32px rgba(15,27,51,.08)">

      <!-- Site info card (table layout - email safe) -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f5f8ff;border:1px solid #e3ebff;border-radius:12px;margin-bottom:20px">
        <tr>
          <td style="padding:16px 20px">
            <table role="presentation" cellspacing="0" cellpadding="0" border="0">
              <tr>
                <td valign="middle" style="width:44px">
                  <div style="width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,#2f7cf6,#6c5ce7);color:#ffffff;font-size:18px;font-weight:800;text-align:center;line-height:40px;font-family:Arial,sans-serif">🔗</div>
                </td>
                <td style="padding-left:14px">
                  <div style="font-weight:700;color:#0f1b33;font-size:15px;font-family:Arial,sans-serif">{site['site_name']}</div>
                  <div style="color:#5a6b85;font-size:13px;font-family:Arial,sans-serif;margin-top:2px">{site['site_url']} &nbsp;·&nbsp; DR {site['dr'] or '—'} &nbsp;·&nbsp; DA {site['da'] or '—'}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>

      <!-- Request details -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e3e8f2;border-radius:12px;margin-bottom:20px">
        <tr>
          <td style="background:#f8faff;padding:12px 20px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:#2f7cf6;border-bottom:1px solid #e3e8f2;border-radius:12px 12px 0 0;font-family:Arial,sans-serif">Request Details</td>
        </tr>
        <tr>
          <td style="padding:12px 20px;color:#5a6b85;font-weight:600;font-size:13px;width:110px;font-family:Arial,sans-serif;background:#ffffff">Requested by</td>
        </tr>
        <tr>
          <td style="padding:2px 20px 12px;color:#0f1b33;font-weight:700;font-size:14px;font-family:Arial,sans-serif;background:#ffffff">{your_name or '—'}</td>
        </tr>
        <tr style="background:#fafbfe">
          <td style="padding:12px 20px;color:#5a6b85;font-weight:600;font-size:13px;font-family:Arial,sans-serif">Their website</td>
        </tr>
        <tr style="background:#fafbfe">
          <td style="padding:2px 20px 12px;color:#2f7cf6;font-weight:700;font-size:14px;font-family:Arial,sans-serif"><a href="https://{your_site}" style="color:#2f7cf6;text-decoration:none">{your_site or '—'}</a></td>
        </tr>
        <tr>
          <td style="padding:12px 20px;color:#5a6b85;font-weight:600;font-size:13px;font-family:Arial,sans-serif">Reply to</td>
        </tr>
        <tr>
          <td style="padding:2px 20px 12px;color:#2f7cf6;font-weight:700;font-size:14px;font-family:Arial,sans-serif"><a href="mailto:{your_email}" style="color:#2f7cf6;text-decoration:none">{your_email or '—'}</a></td>
        </tr>
        <tr style="background:#fafbfe">
          <td style="padding:12px 20px;color:#5a6b85;font-weight:600;font-size:13px;font-family:Arial,sans-serif">Message</td>
        </tr>
        <tr style="background:#fafbfe">
          <td style="padding:2px 20px 14px;color:#3a4a63;line-height:1.6;font-size:14px;font-family:Arial,sans-serif">{message or '—'}</td>
        </tr>
      </table>

      <!-- CTA -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom:20px">
        <tr><td align="center">
          <a href="mailto:{your_email}?subject=Re:%20Link%20Exchange%20with%20{your_site}" style="display:inline-block;background:linear-gradient(135deg,#2f7cf6,#6c5ce7);color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:50px;font-size:15px;font-weight:700;font-family:Arial,sans-serif">Reply to Start Exchange →</a>
        </td></tr>
      </table>

      <!-- Tip -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#fff8e6;border:1px solid #ffe4a1;border-radius:10px;margin-bottom:20px">
        <tr><td style="padding:14px 18px;font-size:13px;color:#7a6500;line-height:1.6;font-family:Arial,sans-serif">💡 <b>Tip:</b> Add their link to a relevant page on your site, then ask them to add yours. Keep exchanges niche-relevant and under 10% of your total backlink profile for best SEO results.</td></tr>
      </table>

      <!-- Footer -->
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-top:1px solid #eef1f7;padding-top:16px">
        <tr><td align="center" style="padding-top:16px">
          <p style="font-size:12px;color:#8a97ad;margin:0 0 4px;font-family:Arial,sans-serif">Sent via <b style="color:#2f7cf6">TWH Link Exchange Directory</b></p>
          <p style="font-size:12px;color:#aab4c6;margin:0;font-family:Arial,sans-serif"><a href="{SITE_URL}/link-exchange/" style="color:#aab4c6;text-decoration:none">thewebhospitality.com/link-exchange</a></p>
        </td></tr>
      </table>

    </td>
  </tr>
</table>
</td></tr>
</table>
</center>
</body></html>""")
        msg = "✅ Request sent! The site owner will contact you. (For faster results, try our guest post service below.)"
        if your_email and mail_ok:
            msg = "✅ Request sent! The site owner has been notified by email and will contact you. (For faster results, try our guest post service below.)"
    return render_template("exchange.html", site=site, msg=msg, site_url=SITE_URL)


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    pending_sites = db.execute(
        "SELECT * FROM sites WHERE status='pending' ORDER BY created_at DESC").fetchall()
    exchange_list = db.execute(
        "SELECT * FROM exchanges ORDER BY id DESC LIMIT 50").fetchall()
    active = db.execute("SELECT COUNT(*) n FROM sites WHERE status='active'").fetchone()["n"]
    pending = len(pending_sites)
    exchanges = db.execute("SELECT COUNT(*) n FROM exchanges").fetchone()["n"]
    return render_template("admin.html", pending_sites=pending_sites,
                           exchange_list=exchange_list, active=active,
                           pending=pending, exchanges=exchanges, site_url=SITE_URL)


@app.route("/admin/approve/<int:site_id>")
@admin_required
def admin_approve(site_id):
    db = get_db()
    db.execute("UPDATE sites SET status='active', approved_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), site_id))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/reject/<int:site_id>")
@admin_required
def admin_reject(site_id):
    db = get_db()
    db.execute("UPDATE sites SET status='rejected' WHERE id=?", (site_id,))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/exchange/<int:exchange_id>/done")
@admin_required
def exchange_done(exchange_id):
    db = get_db()
    db.execute("UPDATE exchanges SET status='done' WHERE id=?", (exchange_id,))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/sitemap.xml")
def sitemap():
    db = get_db()
    sites = db.execute(
        "SELECT id, site_name, site_url, dr FROM sites WHERE status='active' ORDER BY dr DESC").fetchall()
    urls = [f"<url><loc>{SITE_URL}/link-exchange/</loc><changefreq>daily</changefreq><priority>0.9</priority></url>"]
    for n in NICHES[1:]:
        slug = n.lower().replace(" / ", "-").replace(" ", "-")
        urls.append(
            f"<url><loc>{SITE_URL}/link-exchange/niche/{slug}/</loc>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>")
        # programmatic SEO pages (2 per niche)
        for pt in ("link-exchange-in", "free-backlinks-for"):
            urls.append(
                f"<url><loc>{SITE_URL}/link-exchange/{pt}-{slug}/</loc>"
                f"<changefreq>weekly</changefreq><priority>0.6</priority></url>")
    for s in sites:
        urls.append(
            f"<url><loc>{SITE_URL}/link-exchange/site/{s['id']}/</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' \
          + "\n".join(urls) + "\n</urlset>"
    return app.response_class(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return app.response_class(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n",
        mimetype="text/plain")


@app.route("/niche/<path:niche_name>")
def niche_page(niche_name):
    db = get_db()
    niche_name = niche_name.rstrip("/")  # strip trailing slash from path converter
    # normalize: "tech-saas" -> "Tech / SaaS"
    pretty = niche_name.replace("-", " ").title()
    for n in NICHES:
        if n.lower().replace(" / ", "-").replace(" ", "-") == niche_name.lower():
            pretty = n
            break
    sites = db.execute(
        "SELECT * FROM sites WHERE status='active' AND niche LIKE ? ORDER BY dr DESC",
        (f"%{pretty}%",)).fetchall()
    exch_counts = {}
    for row in db.execute(
            "SELECT site_url, COUNT(*) c FROM sites WHERE status IN ('active','pending') GROUP BY site_url").fetchall():
        exch_counts[row["site_url"]] = row["c"]
    return render_template("niche.html", sites=sites, pretty=pretty,
                           niche_name=niche_name, exch_counts=exch_counts,
                           site_url=SITE_URL)


# ============ PROGRAMMATIC SEO PAGES ============
# Pattern: /link-exchange-in-<niche>/ , /guest-post-sites-in-<niche>/ , /free-backlinks-for-<niche>/
# 44 niches x 3 = 132 unique SEO pages with unique content per niche.

PROG_TYPES = {
    "link-exchange-in": {
        "h2_1": "Why {name} Sites Need Link Exchange Partners",
        "para_1": "{intro}",
        "h2_2": "Best {name} Link Exchange Practices",
        "bullets": [
            "Match authority — exchange with {name} sites at or slightly above your DR.",
            "Keep it contextual — place partner links inside relevant content, not footers.",
            "Stay under 10% — keep reciprocal links a small part of your overall profile.",
            "Check activity — prefer {name} sites that publish regularly and get real traffic.",
        ],
        "h2_3": "Find {name} Link Exchange Partners Here",
        "para_3": "Browse the directory below, filter by niche and Domain Rating, and send a free exchange request to sites that match your audience. New partners are added every week.",
    },
    "free-backlinks-for": {
        "h2_1": "Free Backlinks for {name} Sites",
        "para_1": "Every {name} site needs backlinks, and not all of them cost money. {intro} Use these free methods to build authority without spending a rupee.",
        "h2_2": "Free Backlink Strategies for {name}",
        "bullets": [
            "Link exchanges — swap relevant links with other {name} sites (our directory makes this free).",
            "Free directories — list your {name} site in niche directories and business listings.",
            "Content promotion — share {name} guides on forums, communities, and social platforms.",
            "Broken link building — find broken links on {name} blogs and offer your content as a replacement.",
        ],
        "h2_3": "Get Free {name} Backlinks Today",
        "para_3": "Start with a free listing in our link exchange directory — it costs nothing, takes 2 minutes, and puts your {name} site in front of potential partners.",
    },
}


@app.route("/link-exchange-in-<niche_slug>/")
def prog_page_linkexchange(niche_slug):
    return _prog_page("link-exchange-in", niche_slug)


@app.route("/free-backlinks-for-<niche_slug>/")
def prog_page_backlinks(niche_slug):
    return _prog_page("free-backlinks-for", niche_slug)


def _prog_page(prog_type, niche_slug):
    data = get_niche_data(niche_slug)
    db = get_db()
    pretty = data["name"]
    t = PROG_TYPES[prog_type]
    # title pattern per type
    if prog_type == "link-exchange-in":
        title = f"Link Exchange in {pretty} – Find {pretty} Link Exchange Sites | TWH"
        desc = f"Find link exchange in {pretty}. Browse {pretty} link exchange sites, list your website free, and build reciprocal backlinks with relevant {pretty} site owners."
    else:
        title = f"Free Backlinks for {pretty} Sites – 2026 Guide | TWH"
        desc = f"Get free backlinks for {pretty} sites in 2026. Link exchanges, free directories, and content strategies to build {pretty} authority at zero cost."
    sites = db.execute(
        "SELECT * FROM sites WHERE status='active' AND niche LIKE ? ORDER BY dr DESC",
        (f"%{pretty}%",)).fetchall()
    exch_counts = {}
    for row in db.execute(
            "SELECT site_url, COUNT(*) c FROM sites WHERE status IN ('active','pending') GROUP BY site_url").fetchall():
        exch_counts[row["site_url"]] = row["c"]
    return render_template("prog.html", data=data, t=t, sites=sites,
                           pretty=pretty, prog_type=prog_type, niche_slug=niche_slug,
                           title=title, desc=desc, exch_counts=exch_counts,
                           site_url=SITE_URL)


@app.route("/site/<int:site_id>/")
def site_page(site_id):
    db = get_db()
    s = db.execute("SELECT * FROM sites WHERE id=? AND status='active'", (site_id,)).fetchone()
    if not s:
        return "Site not found", 404
    # all people who registered/submitted this domain (sellers/partners)
    sellers = db.execute(
        "SELECT site_name, email, dr, da, traffic, status, owner_verified, created_at "
        "FROM sites WHERE site_url=? ORDER BY owner_verified DESC, dr DESC",
        (s["site_url"],)).fetchall()
    return render_template("site.html", s=s, sellers=sellers, site_url=SITE_URL)


@app.route("/api/sites")
def api_sites():
    db = get_db()
    sites = db.execute(
        "SELECT site_name, site_url, niche, description, dr FROM sites WHERE status='active'"
        " ORDER BY dr DESC").fetchall()
    return jsonify([dict(s) for s in sites])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5051))
    app.run(host="0.0.0.0", port=port)
