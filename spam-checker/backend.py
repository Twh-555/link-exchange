#!/usr/bin/env python3
"""Drop-in replacement backend for thewebhospitality.com/spam-score-checker.

Returns the SAME JSON format the page's JS expects:
    Domain Authority, Domain Rating, Spam Score, Page Authority, Site Traffic

Free data sources (no paid keys required):
  - Spam Score  : DNSBL engine (Spamhaus DBL/ZEN, SpamCop, Barracuda) + heuristics
  - Trust Score : Scamadviser (public page scrape)
  - PageRank    : openpagerank.com (needs free API key -> set OPR_API_KEY)
  - DA/DR/PA    : need Moz/Ahrefs paid keys -> set MOZ_ACCESS_ID/MOZ_SECRET_KEY
                  or AHREFS_API_KEY. Without them they report "N/A (needs paid key)".
  - Traffic     : needs SimilarWeb paid key (SIMILARWEB_API_KEY). N/A otherwise.

Endpoints:
  GET /check-metrics?domain=example.com   (same as old backend)
  GET /health
  GET /                                 (HTML test page)

Deploy:  pip install flask flask-cors requests
         python3 backend.py            (default port 8000)
"""
import json
import os
import re
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, request, session
from flask_cors import CORS

import spamcheck

# load .env file if present (project dir)
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")  # same key as link-exchange → shared session
CORS(app)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36"}

# ---------------------------------------------------------------------------
# DA cost control: persistent cache (7d) + per-IP rate limit + monthly budget
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DA_DB = BASE_DIR / "da_cache.db"

DA_CACHE_TTL = int(os.environ.get("DA_CACHE_TTL_SECONDS", 7 * 86400))      # 7 days
DA_RATE_LIMIT_PER_HOUR = int(os.environ.get("DA_RATE_LIMIT_PER_HOUR", 5))  # per IP/hour
DA_MONTHLY_BUDGET = int(os.environ.get("DA_MONTHLY_BUDGET", 250))          # Apify calls/month
APIFY_PRICE_USD = 0.02                                                     # current actor rate
USD_INR = 83.5

DA_SCHEMA = """
CREATE TABLE IF NOT EXISTS da_cache (
    domain TEXT PRIMARY KEY,
    da INTEGER,
    fetched_at REAL
);
CREATE TABLE IF NOT EXISTS da_usage (
    day TEXT NOT NULL,          -- YYYY-MM
    ip TEXT NOT NULL,
    hour TEXT NOT NULL,         -- YYYY-MM-DD-HH
    calls INTEGER DEFAULT 0,
    PRIMARY KEY (day, ip, hour)
);
CREATE TABLE IF NOT EXISTS da_budget (
    month TEXT PRIMARY KEY,
    calls INTEGER DEFAULT 0
);
"""


def _da_db():
    conn = sqlite3.connect(DA_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(DA_SCHEMA)
    conn.commit()
    return conn


def _moz_da_from_cache(domain: str) -> int | None:
    conn = _da_db()
    row = conn.execute("SELECT da, fetched_at FROM da_cache WHERE domain=?", (domain,)).fetchone()
    conn.close()
    if row and time.time() - row["fetched_at"] < DA_CACHE_TTL:
        return row["da"]
    return None


def _da_rate_allowed(ip: str) -> tuple[bool, str]:
    """Per-IP hourly limit. Returns (allowed, reason)."""
    now = time.localtime()
    day = time.strftime("%Y-%m", now)
    hour = time.strftime("%Y-%m-%d-%H", now)
    conn = _da_db()
    row = conn.execute(
        "SELECT calls FROM da_usage WHERE day=? AND ip=? AND hour=?",
        (day, ip, hour)).fetchone()
    calls = row["calls"] if row else 0
    if calls >= DA_RATE_LIMIT_PER_HOUR:
        conn.close()
        return False, f"DA rate limit reached ({DA_RATE_LIMIT_PER_HOUR}/hour). Cached DA still shown."
    # increment
    conn.execute(
        "INSERT INTO da_usage (day, ip, hour, calls) VALUES (?,?,?,?) "
        "ON CONFLICT(day, ip, hour) DO UPDATE SET calls = calls + 1",
        (day, ip, hour, 1))
    conn.commit()
    conn.close()
    return True, ""


def _da_budget_remaining() -> int:
    month = time.strftime("%Y-%m")
    conn = _da_db()
    row = conn.execute("SELECT calls FROM da_budget WHERE month=?", (month,)).fetchone()
    conn.close()
    return max(0, DA_MONTHLY_BUDGET - (row["calls"] if row else 0))


def _da_spend() -> bool:
    """Mark one Apify call against the monthly budget. Returns False if over budget."""
    month = time.strftime("%Y-%m")
    conn = _da_db()
    row = conn.execute("SELECT calls FROM da_budget WHERE month=?", (month,)).fetchone()
    calls = row["calls"] if row else 0
    if calls >= DA_MONTHLY_BUDGET:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO da_budget (month, calls) VALUES (?,?) "
        "ON CONFLICT(month) DO UPDATE SET calls = calls + 1",
        (month, calls + 1))
    conn.commit()
    conn.close()
    return True


def _da_refund():
    """Refund one Apify call (on failure/block). Never below zero."""
    month = time.strftime("%Y-%m")
    conn = _da_db()
    row = conn.execute("SELECT calls FROM da_budget WHERE month=?", (month,)).fetchone()
    calls = row["calls"] if row else 0
    if calls > 0:
        conn.execute("UPDATE da_budget SET calls = calls - 1 WHERE month=?", (month,))
        conn.commit()
    conn.close()


def _da_cache_put(domain: str, da: int):
    conn = _da_db()
    conn.execute(
        "INSERT INTO da_cache (domain, da, fetched_at) VALUES (?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET da=?, fetched_at=?",
        (domain, da, time.time(), da, time.time()))
    conn.commit()
    conn.close()


def _moz_da(domain: str) -> int | None:
    """Moz Domain Authority via Apify actor 'moz-domain-authority-checker'.

    Real Moz data scraped from Moz.com free domain analysis — NO Moz Pro needed.
    Current rate: $0.02/domain (Apify FREE tier, updated Feb 2026).
    Free Apify token: https://console.apify.com/account#/integrations
    """
    key = os.environ.get("APIFY_API_TOKEN", "")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/jdtpnjtp~moz-domain-authority-checker/"
            "run-sync-get-dataset-items",
            params={"token": key, "timeout": 55},
            json={"domains": [domain]},
            timeout=60,
        )
        r.raise_for_status()
        items = r.json()
        if isinstance(items, list) and items:
            item = items[0]
            da = (item.get("domainAuthority") or item.get("domain_authority")
                  or item.get("da") or item.get("DomainAuthority"))
            return int(da) if da is not None else None
    except Exception:
        return None
    return None


def get_da_cost_controlled(domain: str, ip: str) -> dict:
    """Cost-controlled DA lookup: cache -> rate limit -> budget -> Apify.

    Returns {"da": int|None, "source": "cache"|"apify"|"rate_limited"|"budget"|"no_key",
             "note": str}
    """
    # 1) cache first (never costs money)
    cached = _moz_da_from_cache(domain)
    if cached is not None:
        return {"da": cached, "source": "cache",
                "note": f"cached (TTL {DA_CACHE_TTL//86400}d)"}

    # 1b) key check first — don't burn rate-limit slots when no key is configured
    if not os.environ.get("APIFY_API_TOKEN"):
        return {"da": None, "source": "no_key",
                "note": "free Apify token needed (https://console.apify.com/account#/integrations) — no charge"}

    # 2) rate limit (protect budget from abuse)
    allowed, reason = _da_rate_allowed(ip)
    if not allowed:
        return {"da": None, "source": "rate_limited", "note": reason}

    # 3) monthly budget cap
    remaining = _da_budget_remaining()
    if remaining <= 0:
        return {"da": None, "source": "budget",
                "note": f"monthly DA budget ({DA_MONTHLY_BUDGET}) exhausted — cached DA still served"}

    # 4) live Apify call (counts against budget + rate limit already counted)
    if not _da_spend():
        return {"da": None, "source": "budget", "note": "monthly DA budget exhausted"}
    da = _moz_da(domain)
    if da is None:
        _da_refund()  # blocked/failed — don't charge budget for nothing
        return {"da": None, "source": "apify_failed",
                "note": "Apify blocked or returned no data — budget refunded"}
    _da_cache_put(domain, da)
    return {"da": da, "source": "apify",
            "note": f"live (Apify) — {remaining - 1} budget calls left this month"}


def _scamadviser_score(domain: str) -> tuple[int | None, str | None]:
    """Scrape trust score 0-100 from Scamadviser. Returns (score, last_update).

    Extracts from the embedded JSON-LD (ratingScore) — reliable, avoids HTML noise.
    """
    try:
        r = requests.get(
            f"https://www.scamadviser.com/check-website/{domain}",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return None, None
        import html as _html
        text = _html.unescape(r.text)  # decode &quot; etc so JSON-LD regex works
        score = None
        # 1) JSON-LD AggregateRating ratingValue (most reliable)
        m = re.search(r'"ratingScore"\s*:\s*(\d+)', text)
        if not m:
            m = re.search(r'"ratingValue"\s*:\s*(\d+)', text)
        if m:
            score = int(m.group(1))
        # 2) visible "Trust Score N" element (page sometimes shows a different value)
        if score is None:
            m = re.search(r"Trust Score\s*</\w+>\s*<[^>]*>\s*(\d+)", text)
            if not m:
                m = re.search(r"Trust Score\s*(\d+)", text)
            score = int(m.group(1)) if m else None
        # last update — clean date like "10 months ago"
        m2 = re.search(r"(?:Last Update[:\s]*|last_update[:\s]*)[^<\"]{0,40}?(\d+\s+\w+\s+ago)", text, re.I)
        update = m2.group(1).strip() if m2 else None
        return score, update
    except Exception:
        return None, None


def _openpagerank(domain: str) -> int | None:
    """PageRank 0-10 via openpagerank.com (free API key: https://openpagerank.com/)."""
    key = os.environ.get("OPR_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(
            "https://openpagerank.com/api/v1.0/getPageRank",
            params={"domains[0]": domain},
            headers={"API-OPR": key},
            timeout=15,
        )
        data = r.json()
        pr = data.get("response", [{}])[0].get("page_rank_integer")
        return int(pr) if pr is not None else None
    except Exception:
        return None


def _ahrefs_dr(domain: str) -> int | None:
    """Ahrefs Domain Rating via the FREE public endpoint (v3 /domain-rating-free).

    Free APIv3 key: sign up at https://ahrefs.com/ (free account), then
    generate key at https://app.ahrefs.com/account/api-keys
    Attribution required: "Domain Rating by Ahrefs" (https://ahrefs.com/)
    """
    key = os.environ.get("AHREFS_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.ahrefs.com/v3/public/domain-rating-free",
            params={"target": domain},
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        # shape: {"domain_rating": {"domain_rating": 45.5, "license": "..."}}
        if isinstance(data, dict):
            inner = data.get("domain_rating")
            if isinstance(inner, dict):
                dr = inner.get("domain_rating")
                if dr is not None:
                    return int(round(float(dr)))
            return data.get("domain_rating") or data.get("dr")
        return None
    except Exception:
        return None


def _dapachecker(domain: str) -> dict:
    """DA + PA + Spam Score via dapachecker.org API (key from DAPACHECKER_API_KEY)."""
    key = os.environ.get("DAPACHECKER_API_KEY", "")
    if not key:
        return {}
    try:
        r = requests.post(
            "https://www.dapachecker.org/api/user/dapa-checker",
            json={"urls": [domain]},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0",
                "Origin": "https://www.dapachecker.org",
                "Referer": "https://www.dapachecker.org/api-docs",
            },
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            if item.get("domain", "").strip().lower() == domain.lower():
                return item
        if data.get("data"):
            return data["data"][0]
    except Exception:
        return {}
    return {}


def _traffic(domain: str) -> int | None:
    """Monthly visits via SimilarWeb API (paid key: developer.similarweb.com)."""
    key = os.environ.get("SIMILARWEB_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.similarweb.com/v4/site/{}/total-traffic-and-engagement/".format(domain),
            params={"api_key": key, "start_date": "2026-01-01", "end_date": "2026-02-01", "main_domain_only": "false"},
            timeout=15,
        )
        visits = r.json().get("visits", {})
        total = visits.get("total")
        return int(total) if total is not None else None
    except Exception:
        return None


# ---------- daily limit: 1 check per IP per day ----------
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "1"))
_LIMIT_DB = Path(__file__).resolve().parent / "limit.db"


def _daily_count(ip: str, day: str) -> int:
    try:
        db = sqlite3.connect(_LIMIT_DB)
        row = db.execute("SELECT cnt FROM daily_limits WHERE ip=? AND day=?", (ip, day)).fetchone()
        db.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _daily_allowed(key: str, limit: int = DAILY_LIMIT) -> bool:
    try:
        db = sqlite3.connect(_LIMIT_DB)
        db.execute("CREATE TABLE IF NOT EXISTS daily_limits (ip TEXT, day TEXT, cnt INTEGER, PRIMARY KEY(ip, day))")
        db.commit()
        day = datetime.utcnow().strftime("%Y-%m-%d")
        row = db.execute("SELECT cnt FROM daily_limits WHERE ip=? AND day=?", (key, day)).fetchone()
        db.close()
        return (row[0] if row else 0) < limit
    except Exception:
        return True  # fail-open


def _daily_use(ip: str) -> None:
    try:
        db = sqlite3.connect(_LIMIT_DB)
        db.execute("CREATE TABLE IF NOT EXISTS daily_limits (ip TEXT, day TEXT, cnt INTEGER, PRIMARY KEY(ip, day))")
        day = datetime.utcnow().strftime("%Y-%m-%d")
        db.execute("INSERT INTO daily_limits (ip, day, cnt) VALUES (?,?,1) "
                   "ON CONFLICT(ip, day) DO UPDATE SET cnt = cnt + 1", (ip, day))
        db.commit()
        db.close()
    except Exception:
        pass


@app.route("/check-metrics")
def check_metrics():
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "Missing 'domain' parameter"}), 400

    spam = spamcheck.check_domain(domain)
    if spam.get("verdict") == "invalid":
        return jsonify({"error": spam["error"]}), 400

    # ---- daily limit: 2 checks per IP per day + logged-in users get 3 EXTRA ----
    ip = request.remote_addr or "unknown"
    logged_in = bool(session.get("user_email"))
    if logged_in:
        # logged-in users: 3 extra checks/day (keyed by email)
        user_key = f"user:{session['user_email']}"
        if not _daily_allowed(user_key, limit=3):
            return jsonify({
                "error": "Daily limit reached — logged-in users get 3 extra checks per day.",
                "login_required": True,
                "login_url": "https://thewebhospitality.com/link-exchange/login",
            }), 429
        _daily_use(user_key)
    else:
        # free users: 1 check/day (keyed by IP)
        if not _daily_allowed(ip, limit=DAILY_LIMIT):
            return jsonify({
                "error": "Daily limit reached — 1 free check per day. Login for full access.",
                "login_required": True,
                "login_url": "https://thewebhospitality.com/link-exchange/login",
            }), 429
        _daily_use(ip)

    dapa = _dapachecker(domain)
    da = dapa.get("site_da")
    pa = dapa.get("site_pa")
    dapa_spam = dapa.get("spam_score")
    dr = _ahrefs_dr(domain)

    # Spam Score: dapachecker's spam_score (1-100-ish) or DNSBL fallback
    spam_pct = spam["score"]
    if dapa_spam is not None and dapa_spam > 0:
        spam_pct = int(dapa_spam)

    def fmt(v, label):
        if v is None:
            return "N/A"
        return str(v)

    return jsonify({
        "domain": domain,
        "Domain Authority": fmt(da, "DA"),
        "Domain Rating": fmt(dr, "DR") + ("" if dr is not None else " (free Ahrefs key needed)"),
        "Spam Score": f"{spam_pct}%" if spam_pct else "0%",
        "Page Authority": fmt(pa, "PA"),
        "DA Source": "dapachecker.org" if da is not None else "N/A",
        "Verdict": spam["verdict"].upper(),
        "DNSBL Hits": ", ".join(spam["dnsbl_hits"]) or "None",
        "IP": spam.get("ip") or "N/A",
        "Checks": spam["checks"],
        "logged_in": logged_in,
        "checked_at": datetime.utcnow().isoformat() + "Z",
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "da": {
            "cache_ttl_days": DA_CACHE_TTL // 86400,
            "rate_limit_per_hour": DA_RATE_LIMIT_PER_HOUR,
            "monthly_budget": DA_MONTHLY_BUDGET,
            "budget_remaining": _da_budget_remaining(),
            "apify_token": "set" if os.environ.get("APIFY_API_TOKEN") else "MISSING",
            "price_per_check_usd": APIFY_PRICE_USD,
        },
        "time": datetime.utcnow().isoformat() + "Z",
    })


@app.route("/api/authority")
def api_authority():
    """Multi-provider DA/PA authority score (pools all free APIs)."""
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "Missing 'domain' parameter"}), 400
    try:
        import authority
        return jsonify(authority.authority_score(domain))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/")
def index():
    return """<h2>Spam Checker Backend is running</h2>
<p>Try: <code>/check-metrics?domain=thewebhospitality.com</code></p>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
