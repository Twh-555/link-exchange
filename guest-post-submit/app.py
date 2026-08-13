"""Guest Post Site Submission App — thewebhospitality.com/guest-posting-sites-list/ ke liye.

Alag SQLite DB (guestposts.db) — link-exchange se completely separate.
Users apni website submit karte hain guest post ke liye; admin approve/reject karta hai.
"""
import os
import re
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "guestposts.db"
SITE_URL = os.environ.get("SITE_URL", "https://www.thewebhospitality.com")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hello@thewebhospitality.com")
# $5 Instant Add payment link (Razorpay/PayPal/Stripe payment link URL)
PAYMENT_LINK = os.environ.get("PAYMENT_LINK", "")
INSTANT_PRICE = os.environ.get("INSTANT_PRICE", "$5")

# --- Database mode: Supabase (Postgres) when DATABASE_URL set, else local SQLite ---
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        USE_POSTGRES = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "guest-post-submit-secret")

NICHES = [
    "All", "SaaS", "Tech / SaaS", "Business / Finance", "SEO / Marketing",
    "Health / Fitness", "Travel", "Fashion / Beauty", "Food",
    "Real Estate", "Cryptocurrencies", "Web Design", "Web Development",
    "Education / Science", "Finance", "Lifestyle", "Entertainment / Hobbies",
    "Photography / Videography", "Marketing", "Other",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name TEXT NOT NULL,
    site_url TEXT NOT NULL,
    email TEXT NOT NULL,
    niche TEXT NOT NULL,
    dr INTEGER DEFAULT 0,
    da INTEGER DEFAULT 0,
    content_types TEXT DEFAULT '',
    pricing TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',   -- pending | approved | rejected
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    password TEXT DEFAULT ''         -- auto-generated on submit; used for login
);
"""


def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            conn.autocommit = False
            g.db = _PGDB(conn)
            g.db.row_factory = sqlite3.Row
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
            g.db.executescript(SCHEMA)
            # migration: ensure password column exists (older DBs)
            cols = [r[1] for r in g.db.execute("PRAGMA table_info(submissions)").fetchall()]
            if "password" not in cols:
                g.db.execute("ALTER TABLE submissions ADD COLUMN password TEXT DEFAULT ''")
            g.db.commit()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_user():
    return {"user_email": session.get("user_email", "")}


@app.route("/login", methods=["GET", "POST"])
def login():
    msg, ok = "", False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        db = get_db()
        if USE_POSTGRES:
            # link-exchange users: Supabase sites table (email + password)
            row = db.execute(
                "SELECT * FROM sites WHERE email=? AND password=? ORDER BY id DESC LIMIT 1",
                (email, password)).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM submissions WHERE email=? AND password=? ORDER BY id DESC LIMIT 1",
                (email, password)).fetchone()
        if row:
            session["user_email"] = row["email"]
            return redirect(url_for("index"))
        msg = "❌ Invalid email or password. Register free by adding your website — or use your Link Exchange login if you already have an account."
    return render_template("login.html", msg=msg, ok=ok, site_url=SITE_URL)


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    return redirect(url_for("index"))


def normalize_url(url: str) -> str:
    url = url.strip().replace("http://", "").replace("https://", "").strip("/")
    return url.lower()


def validate_site(site_url: str, email: str) -> tuple[bool, str]:
    if not re.match(r"^[a-z0-9\-\.]+\.[a-z]{2,}$", site_url):
        return False, "Invalid domain URL"
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return False, "Invalid email address"
    return True, ""


def load_site_data():
    """Load the original guest posting sites list content (same as WP page)."""
    try:
        with open(BASE_DIR / "gp_sites_data.json") as f:
            return json.load(f)
    except Exception:
        return {"tables": [], "guides": {}, "faq": [], "updated": ""}


@app.route("/")
def index():
    db = get_db()
    data = load_site_data()
    approved = db.execute(
        "SELECT * FROM submissions WHERE status='approved' ORDER BY da DESC").fetchall()
    return render_template("index.html", data=data, approved=approved,
                           niches=NICHES, site_url=SITE_URL,
                           payment_link=PAYMENT_LINK, instant_price=INSTANT_PRICE,
                           user_email=session.get("user_email", ""))


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
            niches = [n for n in f.getlist("niche") if n and n != "All"]
            niches = list(dict.fromkeys(niches))
            if len(niches) < 3:
                msg = "❌ Please select at least 3 niches."
            elif len(niches) > 5:
                msg = "❌ Please select at most 5 niches."
            else:
                try:
                    dr = int(f.get("dr", "").strip())
                    da = int(f.get("da", "").strip())
                except ValueError:
                    dr = da = None
                if dr is None or da is None:
                    msg = "❌ DR and DA are mandatory. Please enter both values (0-100)."
                elif not (0 <= dr <= 100 and 0 <= da <= 100):
                    msg = "❌ DR and DA must be between 0 and 100."
                else:
                    # $5 instant add? (payment link se aaya ho to auto-approve)
                    instant = f.get("instant", "") == "1"
                    status = "approved" if instant else "pending"
                    import secrets
                    password = secrets.token_urlsafe(6)
                    db.execute(
                        "INSERT INTO submissions (site_name, site_url, email, niche, dr, da, content_types, pricing, status, created_at, password)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f.get("site_name", "").strip()[:60], site_url, email,
                         ", ".join(niches), min(dr, 100), min(da, 100),
                         f.get("content_types", "").strip()[:300],
                         f.get("pricing", "").strip()[:60],
                         status, datetime.utcnow().isoformat(), password))
                    db.commit()
                    session["user_email"] = email
                    if USE_POSTGRES:
                        # also ensure a link-exchange account exists for this email (same DB login)
                        try:
                            db.execute(
                                "INSERT INTO sites (site_name, site_url, email, niche, description, dr, da, traffic, status, token, password, verified, verify_token, verify_expires, owner_verified, created_at, notify)"
                                " VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?, 0, ?, ?, 0, ?, 1)",
                                (f.get("site_name", "").strip()[:60], site_url, email,
                                 ", ".join(niches), "",
                                 min(dr, 100), min(da, 100), "",
                                 secrets.token_urlsafe(16), password,
                                 secrets.token_urlsafe(24),
                                 (datetime.utcnow() + timedelta(hours=24)).isoformat(),
                                 datetime.utcnow().isoformat()))
                            db.commit()
                        except Exception:
                            pass  # duplicate URL etc — login already covered by sites table
                    if instant:
                        msg = "✅ Payment received — your site is LIVE now! 🎉"
                    else:
                        msg = "✅ Submission received! Our team will review your site and contact you."
                    msg += f" 🔑 Your login password: <b>{password}</b> — save it! Use it to unlock the full directory."
                    ok = True
    return render_template("submit.html", msg=msg, ok=ok, niches=NICHES,
                           site_url=SITE_URL, payment_link=PAYMENT_LINK,
                           instant_price=INSTANT_PRICE)


@app.route("/admin")
def admin():
    db = get_db()
    pending = db.execute(
        "SELECT * FROM submissions WHERE status='pending' ORDER BY created_at DESC").fetchall()
    approved = db.execute(
        "SELECT * FROM submissions WHERE status='approved' ORDER BY da DESC").fetchall()
    rejected = db.execute(
        "SELECT * FROM submissions WHERE status='rejected' ORDER BY reviewed_at DESC LIMIT 20").fetchall()
    return render_template("admin.html", pending=pending, approved=approved,
                           rejected=rejected, site_url=SITE_URL)


@app.route("/admin/approve/<int:sid>")
def admin_approve(sid):
    db = get_db()
    db.execute("UPDATE submissions SET status='approved', reviewed_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), sid))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/reject/<int:sid>")
def admin_reject(sid):
    db = get_db()
    db.execute("UPDATE submissions SET status='rejected', reviewed_at=? WHERE id=?",
               (datetime.utcnow().isoformat(), sid))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/api/sites")
def api_sites():
    db = get_db()
    sites = db.execute(
        "SELECT site_name, site_url, niche, dr, da, content_types, pricing FROM submissions"
        " WHERE status='approved' ORDER BY da DESC").fetchall()
    return jsonify([dict(s) for s in sites])


@app.route("/sitemap.xml")
def sitemap():
    db = get_db()
    sites = db.execute(
        "SELECT id, site_name, site_url FROM submissions WHERE status='approved'").fetchall()
    urls = [f"<url><loc>{SITE_URL}/guest-posting-sites-list/</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>"]
    for s in sites:
        urls.append(
            f"<url><loc>{SITE_URL}/guest-post-submission/site/{s['id']}/</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' \
          + "\n".join(urls) + "\n</urlset>"
    return app.response_class(xml, mimetype="application/xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5053))
    app.run(host="0.0.0.0", port=port)


# ---------- Postgres adapter (same as link-exchange) ----------
class _PGDB:
    """Postgres adapter — same interface as sqlite3 (execute, executemany, commit, row_factory)."""

    def __init__(self, conn):
        self.conn = conn
        self.row_factory = None

    def _cur(self):
        if self.row_factory == sqlite3.Row:
            return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.conn.cursor()

    def _fix_sql(self, sql):
        sql = sql.replace("INSERT OR IGNORE", "INSERT")
        sql = sql.replace("? ", "%s ")
        sql = sql.replace("?,", "%s,")
        sql = sql.replace("(? )", "(%s)")
        sql = sql.replace("(?)", "(%s)")
        sql = sql.replace("=? ", "=%s ")
        sql = sql.replace("=?", "=%s")
        sql = sql.replace("?)", "%s)")
        sql = sql.replace("LIKE ?", "LIKE %s")
        sql = sql.replace("IN (?)", "IN (%s)")
        sql = sql.replace("NOT IN (?)", "NOT IN (%s)")
        return sql

    def execute(self, sql, params=()):
        cur = self._cur()
        cur.execute(self._fix_sql(sql), params if params else None)
        return _PGRow(cur)

    def executemany(self, sql, seq):
        cur = self._cur()
        cur.executemany(self._fix_sql(sql), seq)
        return _PGRow(cur)

    def executescript(self, sql):
        return None

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


class _PGRow:
    def __init__(self, cur):
        self.cur = cur

    def fetchone(self):
        r = self.cur.fetchone()
        if r is None:
            return None
        d = dict(r)
        return _HybridRow(d) if len(d) == 1 else d

    def fetchall(self):
        out = []
        for r in self.cur.fetchall():
            d = dict(r)
            out.append(_HybridRow(d) if len(d) == 1 else d)
        return out

    @property
    def rowcount(self):
        return self.cur.rowcount


class _HybridRow(dict):
    """dict row that also supports integer index access (for COUNT(*) -> row[0])."""

    def __getitem__(self, key):
        if isinstance(key, int):
            vals = list(self.values())
            return vals[key]
        return dict.__getitem__(self, key)
