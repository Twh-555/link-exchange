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
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, g, jsonify, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "linkexchange.db"
SITE_URL = "https://www.thewebhospitality.com"
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hello@thewebhospitality.com")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

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
    status TEXT DEFAULT 'pending',   -- pending | active | rejected
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
        g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- helpers ----------

def normalize_url(url: str) -> str:
    url = url.strip().replace("http://", "").replace("https://", "").strip("/")
    return url.lower()


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
        "SELECT * FROM sites WHERE status='active' ORDER BY dr DESC").fetchall()
    return render_template("index.html", sites=sites, niches=NICHES,
                           site_url=SITE_URL)


@app.route("/submit", methods=["GET", "POST"])
def submit():
    db = get_db()
    msg, ok = "", False
    if request.method == "POST":
        f = request.form
        site_url = normalize_url(f.get("site_url", ""))
        email = f.get("email", "").strip().lower()
        valid, err = validate_site(site_url, email)
        if not valid:
            msg = f"❌ {err}"
        else:
            # multi-niche: select 3-5 niches (getlist returns list)
            niches = [n for n in f.getlist("niche") if n and n != "All"]
            niches = list(dict.fromkeys(niches))  # dedupe preserve order
            if len(niches) < 3:
                msg = "❌ Please select at least 3 niches."
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
                    db.execute(
                        "INSERT INTO sites (site_name, site_url, email, niche, description, dr, da, status, created_at)"
                        " VALUES (?,?,?,?,?,?,?, 'active', ?)",
                        (f.get("site_name", "").strip()[:60], site_url, email,
                         niche_str, f.get("description", "").strip()[:200],
                         min(dr, 100), min(da, 100), datetime.utcnow().isoformat()))
                    db.commit()
                    msg = "✅ Site added! You're now listed in the directory."
                    ok = True
    return render_template("submit.html", msg=msg, ok=ok, niches=NICHES,
                           site_url=SITE_URL)


@app.route("/exchange/<int:site_id>", methods=["GET", "POST"])
def exchange(site_id):
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        return "Site not found", 404
    msg = ""
    if request.method == "POST":
        f = request.form
        db.execute(
            "INSERT INTO exchanges (from_site_id, to_site_id, message, status, created_at)"
            " VALUES (?,?,?, 'pending', ?)",
            (0, site_id, f"{f.get('your_name','')} | {f.get('your_email','')} | "
             f"{f.get('your_site','')}: {f.get('message','')}",
             datetime.utcnow().isoformat()))
        db.commit()
        msg = "✅ Request sent! The site owner will contact you. (For faster results, try our guest post service below.)"
    return render_template("exchange.html", site=site, msg=msg, site_url=SITE_URL)


@app.route("/admin")
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
def admin_approve(site_id):
    db = get_db()
    db.execute("UPDATE sites SET status='active', approved_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), site_id))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/reject/<int:site_id>")
def admin_reject(site_id):
    db = get_db()
    db.execute("UPDATE sites SET status='rejected' WHERE id=?", (site_id,))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/exchange/<int:exchange_id>/done")
def admin_exchange_done(exchange_id):
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
        urls.append(
            f"<url><loc>{SITE_URL}/link-exchange/{n.lower().replace(' / ', '-').replace(' ', '-')}/</loc>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>")
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
    return render_template("niche.html", sites=sites, pretty=pretty,
                           niche_name=niche_name, site_url=SITE_URL)


@app.route("/site/<int:site_id>/")
def site_page(site_id):
    db = get_db()
    s = db.execute("SELECT * FROM sites WHERE id=? AND status='active'", (site_id,)).fetchone()
    if not s:
        return "Site not found", 404
    return render_template("site.html", s=s, site_url=SITE_URL)


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
