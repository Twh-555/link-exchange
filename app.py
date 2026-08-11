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
from flask import Flask, g, jsonify, redirect, render_template_string, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "linkexchange.db"
SITE_URL = "https://www.thewebhospitality.com"
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hello@thewebhospitality.com")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

NICHES = [
    "All", "Business", "Tech / SaaS", "SEO / Marketing", "Finance", "Health",
    "Travel", "Food", "Fashion", "Education", "Real Estate", "News / Blog",
    "E-commerce", "Entertainment", "Other",
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

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free Link Exchange Directory - Find Link Exchange Partners (2026)</title>
<meta name="description" content="Free link exchange directory for website owners. List your site, find link exchange partners in your niche, and build backlinks organically. No signup needed to browse.">
<link rel="canonical" href="{{site_url}}/link-exchange/">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "name": "Free Link Exchange Directory",
  "description": "List your website free and find link exchange partners by niche.",
  "numberOfItems": {{sites|length}},
  "itemListElement": [
    {% for s in sites %}
    {
      "@type": "ListItem",
      "position": {{loop.index}},
      "url": "{{s.site_url}}",
      "name": "{{s.site_name}}"
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  ]
}
</script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f6fb;color:#1a2333}
  .top{background:#0f1b33;color:#fff;padding:48px 20px;text-align:center}
  .top h1{font-size:2rem;margin-bottom:10px}
  .top p{color:#9fb3d1;max-width:640px;margin:0 auto 24px}
  .btn{display:inline-block;background:#2f7cf6;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600;margin:4px}
  .btn.ghost{background:transparent;border:1px solid #4a5b7a}
  .container{max-width:1100px;margin:0 auto;padding:24px 16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:20px}
  .card{background:#fff;border:1px solid #e3e8f2;border-radius:12px;padding:18px;transition:.2s}
  .card:hover{box-shadow:0 6px 20px rgba(15,27,51,.08);transform:translateY(-2px)}
  .card .niche{display:inline-block;font-size:.75rem;background:#e8f0fe;color:#2f7cf6;padding:3px 10px;border-radius:20px;margin-bottom:8px}
  .card h3{font-size:1.05rem;margin-bottom:6px}
  .card a{color:#2f7cf6;text-decoration:none}
  .card p{font-size:.85rem;color:#5a6b85;margin-bottom:10px}
  .meta{font-size:.78rem;color:#8a97ad}
  .badge{display:inline-block;padding:3px 8px;border-radius:6px;font-size:.72rem;font-weight:700}
  .b-dr{background:#fff3cd;color:#8a6d00}
  .b-free{background:#d4f5e0;color:#0a7a3d}
  .b-paid{background:#ffe0e0;color:#b00000}
  .filters{margin:20px 0;display:flex;gap:10px;flex-wrap:wrap}
  select,.search{padding:10px 14px;border:1px solid #ccd4e4;border-radius:8px;font-size:.95rem}
  .search{flex:1;min-width:200px}
  .hint{background:#eef4ff;border:1px solid #cfe0ff;border-radius:10px;padding:14px 18px;margin:20px 0;font-size:.9rem}
  .hint b{color:#2f7cf6}
  footer{text-align:center;padding:30px;color:#8a97ad;font-size:.85rem}
</style>
</head>
<body>
<div class="top">
  <h1>🔗 Free Link Exchange Directory</h1>
  <p>List your website free, find link-exchange partners in your niche, or get a
     quality <b>guest post</b> on high-DR sites. No signup required to browse.</p>
  <a class="btn" href="/submit">+ Add Your Site (Free)</a>
  <a class="btn ghost" href="/admin">Admin</a>
</div>
<div class="container">
  <div class="hint">💡 <b>How it works:</b> Add your site free → get listed with your
    niche & DR → other site owners contact you for link exchange → or order a
    <b>guest post</b> on premium sites (starting $10) for guaranteed one-way links.</div>

  <div class="filters">
    <input class="search" id="q" placeholder="Search sites...">
    <select id="niche">
      {% for n in niches %}<option value="{{n}}">{{n}}</option>{% endfor %}
    </select>
  </div>

  <div class="grid" id="grid">
    {% for s in sites %}
    <div class="card" data-name="{{s.site_name.lower()}}" data-niche="{{s.niche}}">
      <span class="niche">{{s.niche}}</span>
      <h3><a href="{{s.site_url}}" target="_blank" rel="nofollow">{{s.site_name}}</a></h3>
      <p>{{s.description[:90]}}</p>
      <div class="meta">
        <span class="badge b-dr">DR {{s.dr}}</span>
        <span class="badge b-free">Link Exchange</span>
        <a href="/exchange/{{s.id}}" style="float:right;font-size:.8rem">Request Exchange →</a>
      </div>
    </div>
    {% else %}
    <p>No sites yet — be the first to <a href="/submit">add your site</a>!</p>
    {% endfor %}
  </div>
</div>
<footer>Powered by <a href="{{site_url}}">The Web Hospitality</a> • Free Link Exchange • Guest Posts Available</footer>
<script>
  const q=document.getElementById('q'), n=document.getElementById('niche');
  function filter(){
    const query=q.value.toLowerCase();
    const niche=n.value;
    document.querySelectorAll('#grid .card').forEach(c=>{
      const okQ=c.dataset.name.includes(query)||!query;
      const okN=niche==='All'||c.dataset.niche===niche;
      c.style.display=(okQ&&okN)?'':'none';
    });
  }
  q.addEventListener('input',filter); n.addEventListener('change',filter);
</script>
</body>
</html>
"""

SUBMIT_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Add Your Site - Free Link Exchange</title>
<style>
  body{font-family:'Segoe UI',sans-serif;background:#f4f6fb;color:#1a2333;display:flex;justify-content:center;padding:40px 16px}
  .card{background:#fff;border:1px solid #e3e8f2;border-radius:14px;padding:32px;max-width:560px;width:100%}
  h1{font-size:1.5rem;margin-bottom:6px} .sub{color:#5a6b85;font-size:.9rem;margin-bottom:24px}
  label{display:block;font-weight:600;margin:14px 0 6px;font-size:.9rem}
  input,select,textarea{width:100%;padding:11px 14px;border:1px solid #ccd4e4;border-radius:8px;font-size:.95rem}
  .btn{width:100%;background:#2f7cf6;color:#fff;border:0;padding:14px;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:22px}
  .err{background:#ffecec;color:#b00000;padding:10px 14px;border-radius:8px;margin-top:14px;font-size:.85rem}
  .ok{background:#e7f9ee;color:#0a7a3d;padding:10px 14px;border-radius:8px;margin-top:14px;font-size:.85rem}
  a{color:#2f7cf6}
</style></head><body>
<div class="card">
  <h1>➕ Add Your Website — Free</h1>
  <div class="sub">Listed in our <b>link exchange directory</b>. Other site owners will contact you for exchanges. Want a <b>guaranteed guest post</b> instead? <a href="{{site_url}}/guest-post-price-checker/">Check guest post prices here</a>.</div>
  {% if msg %}<div class="{{'ok' if ok else 'err'}}">{{msg}}</div>{% endif %}
  <form method="post">
    <label>Site Name</label><input name="site_name" required placeholder="My Awesome Blog">
    <label>Site URL</label><input name="site_url" required placeholder="myblog.com">
    <label>Email (for exchange requests)</label><input type="email" name="email" required placeholder="you@email.com">
    <label>Niche</label>
    <select name="niche">{% for n in niches %}{% if n != 'All' %}<option>{{n}}</option>{% endif %}{% endfor %}</select>
    <label>Short Description (max 200)</label><textarea name="description" maxlength="200" rows="3"></textarea>
    <label>Domain Rating (DR) — optional</label><input type="number" name="dr" min="0" max="100" placeholder="0-100">
    <button class="btn" type="submit">Submit Listing — Free</button>
  </form>
</div></body></html>
"""

EXCHANGE_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link Exchange Request</title>
<style>
  body{font-family:'Segoe UI',sans-serif;background:#f4f6fb;color:#1a2333;display:flex;justify-content:center;padding:40px 16px}
  .card{background:#fff;border:1px solid #e3e8f2;border-radius:14px;padding:32px;max-width:560px;width:100%}
  h1{font-size:1.4rem} .sub{color:#5a6b85;font-size:.9rem;margin-bottom:20px}
  .tgt{background:#eef4ff;border-radius:10px;padding:14px 18px;margin-bottom:20px}
  .tgt h3{margin-bottom:4px}
  label{display:block;font-weight:600;margin:14px 0 6px;font-size:.9rem}
  textarea,input{width:100%;padding:11px 14px;border:1px solid #ccd4e4;border-radius:8px;font-size:.95rem}
  .btn{width:100%;background:#2f7cf6;color:#fff;border:0;padding:14px;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer;margin-top:22px}
  .guest{background:#fff3e6;border:1px solid #ffd9ad;border-radius:10px;padding:14px 18px;margin-top:18px;font-size:.9rem}
  a{color:#2f7cf6}
</style></head><body>
<div class="card">
  <h1>🔁 Link Exchange Request</h1>
  <div class="sub">Send a free exchange request to this site owner. They'll get your email and contact you.</div>
  <div class="tgt">
    <h3>{{site.site_name}}</h3>
    <div style="font-size:.85rem;color:#5a6b85">{{site.site_url}} • DR {{site.dr}} • {{site.niche}}</div>
  </div>
  {% if msg %}<div class="ok" style="background:#e7f9ee;color:#0a7a3d;padding:10px 14px;border-radius:8px">{{msg}}</div>{% endif %}
  <form method="post">
    <label>Your Name</label><input name="your_name" required>
    <label>Your Email</label><input type="email" name="your_email" required>
    <label>Your Website</label><input name="your_site" required placeholder="yoursite.com">
    <label>Message (what links do you want to exchange?)</label>
    <textarea name="message" rows="4" required placeholder="Hi! I run a tech blog with DR 25. I'd like to exchange a homepage link with you..."></textarea>
    <button class="btn" type="submit">Send Exchange Request — Free</button>
  </form>
  <div class="guest">💼 <b>Don't want to wait?</b> Order a <b>guaranteed guest post</b> on this site or
    other premium sites — <a href="{{site_url}}/guest-post-price-checker/">see prices here</a>. One-way
    link, published in 24-48h.</div>
</div></body></html>
"""

ADMIN_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin - Link Exchange</title>
<style>
  body{font-family:'Segoe UI',sans-serif;background:#f4f6fb;color:#1a2333;padding:30px 16px}
  .container{max-width:1000px;margin:0 auto}
  table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #eef1f7;font-size:.9rem}
  th{background:#0f1b33;color:#fff}
  .btn{display:inline-block;background:#2f7cf6;color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:.8rem;font-weight:600;margin-right:4px}
  .btn.green{background:#0a7a3d}.btn.red{background:#b00000}
  h1{font-size:1.5rem;margin-bottom:4px}.sub{color:#5a6b85;font-size:.9rem;margin-bottom:20px}
  .stat{display:inline-block;background:#fff;border:1px solid #e3e8f2;border-radius:10px;padding:12px 22px;margin:0 8px 16px 0}
  .stat b{font-size:1.4rem;display:block;color:#0f1b33}
</style></head><body>
<div class="container">
  <h1>🛠️ Link Exchange Admin</h1>
  <div class="sub">Approve new listings & review exchange requests. New members = new reciprocal backlinks.</div>
  <div class="stat"><b>{{active}}</b>Active Sites</div>
  <div class="stat"><b>{{pending}}</b>Pending Approval</div>
  <div class="stat"><b>{{exchanges}}</b>Exchange Requests</div>

  <h2 style="margin:20px 0 10px;font-size:1.1rem">Pending Approvals</h2>
  <table>
    <tr><th>Site</th><th>Niche</th><th>DR</th><th>Email</th><th>Date</th><th>Actions</th></tr>
    {% for s in pending_sites %}
    <tr>
      <td><a href="{{s.site_url}}" target="_blank">{{s.site_name}}</a></td>
      <td>{{s.niche}}</td><td>{{s.dr}}</td><td>{{s.email}}</td>
      <td>{{s.created_at[:10]}}</td>
      <td>
        <a class="btn green" href="/admin/approve/{{s.id}}">Approve</a>
        <a class="btn red" href="/admin/reject/{{s.id}}">Reject</a>
      </td>
    </tr>
    {% else %}<tr><td colspan="6">Nothing pending 🎉</td></tr>{% endfor %}
  </table>

  <h2 style="margin:30px 0 10px;font-size:1.1rem">Exchange Requests</h2>
  <table>
    <tr><th>From</th><th>To</th><th>Message</th><th>Status</th><th>Actions</th></tr>
    {% for e in exchange_list %}
    <tr>
      <td>#{{e.from_site_id}}</td><td>#{{e.to_site_id}}</td>
      <td style="max-width:300px">{{e.message[:80]}}</td>
      <td>{{e.status}}</td>
      <td><a class="btn" href="/admin/exchange/{{e.id}}/done">Mark Done</a></td>
    </tr>
    {% else %}<tr><td colspan="5">No exchange requests yet</td></tr>{% endfor %}
  </table>
</div></body></html>
"""


@app.route("/")
def index():
    db = get_db()
    sites = db.execute(
        "SELECT * FROM sites WHERE status='active' ORDER BY dr DESC").fetchall()
    return render_template_string(INDEX_HTML, sites=sites, niches=NICHES,
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
            dr = int(f.get("dr") or 0)
            if dr == 0:
                dr, _ = fetch_dr_da(site_url)
            db.execute(
                "INSERT INTO sites (site_name, site_url, email, niche, description, dr, da, status, created_at)"
                " VALUES (?,?,?,?,?,?,?, 'active', ?)",
                (f.get("site_name", "").strip()[:60], site_url, email,
                 f.get("niche", "Other"), f.get("description", "").strip()[:200],
                 min(dr, 100), 0, datetime.utcnow().isoformat()))
            db.commit()
            msg = "✅ Site added! You're now listed in the directory."
            ok = True
    return render_template_string(SUBMIT_HTML, msg=msg, ok=ok, niches=NICHES,
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
    return render_template_string(EXCHANGE_HTML, site=site, msg=msg, site_url=SITE_URL)


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
    return render_template_string(ADMIN_HTML, pending_sites=pending_sites,
                                  exchange_list=exchange_list, active=active,
                                  pending=pending, exchanges=exchanges)


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
    # normalize: "tech-saas" -> "Tech / SaaS"
    pretty = niche_name.replace("-", " ").title()
    for n in NICHES:
        if n.lower().replace(" / ", "-").replace(" ", "-") == niche_name.lower():
            pretty = n
            break
    sites = db.execute(
        "SELECT * FROM sites WHERE status='active' AND niche=? ORDER BY dr DESC",
        (pretty,)).fetchall()
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Link Exchange Partners in {pretty} - Free Directory</title>
<meta name="description" content="Find free link exchange partners in the {pretty} niche. List your website and connect with other {pretty} site owners.">
<link rel="canonical" href="{SITE_URL}/link-exchange/{niche_name}/">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"ItemList","name":"{pretty} link exchange partners","numberOfItems":{len(sites)}}}</script>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}}a{{color:#2f7cf6}}table{{width:100%;border-collapse:collapse;margin-top:20px}}td,th{{padding:10px;border-bottom:1px solid #eee;text-align:left}}h1{{font-size:1.6rem}}</style></head>
<body><h1>🔗 Link Exchange Partners in {pretty}</h1>
<p>Free link exchange directory for the <b>{pretty}</b> niche. {len(sites)} sites listed. <a href="{SITE_URL}/link-exchange/">Browse all niches</a> or <a href="{SITE_URL}/submit">add your site free</a>.</p>
<table><tr><th>Site</th><th>DR</th><th>Action</th></tr>"""
    for s in sites:
        html += f"""<tr><td><a href="{s['site_url']}" target="_blank" rel="nofollow">{s['site_name']}</a></td>
        <td>DR {s['dr']}</td><td><a href="{SITE_URL}/exchange/{s['id']}">Request Exchange</a></td></tr>"""
    html += "</table></body></html>"
    return html


@app.route("/site/<int:site_id>/")
def site_page(site_id):
    db = get_db()
    s = db.execute("SELECT * FROM sites WHERE id=? AND status='active'", (site_id,)).fetchone()
    if not s:
        return "Site not found", 404
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{s['site_name']} - DR {s['dr']} | Link Exchange Profile</title>
<meta name="description" content="{s['site_name']} ({s['site_url']}) - {s['niche']} niche, DR {s['dr']}. Request a free link exchange.">
<link rel="canonical" href="{SITE_URL}/link-exchange/site/{s['id']}/">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"WebPage","name":"{s['site_name']}","url":"{s['site_url']}","about":"{s['niche']}"}}</script>
<style>body{{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 16px;line-height:1.6}}a{{color:#2f7cf6}}.card{{border:1px solid #e3e8f2;border-radius:12px;padding:24px;background:#fff}}.badge{{display:inline-block;background:#fff3cd;color:#8a6d00;padding:3px 10px;border-radius:20px;font-size:.8rem;font-weight:700}}.btn{{display:inline-block;background:#2f7cf6;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:16px}}</style></head>
<body><div class="card">
<h1>{s['site_name']}</h1>
<p><span class="badge">DR {s['dr']}</span> <span class="badge" style="background:#e8f0fe;color:#2f7cf6">{s['niche']}</span></p>
<p>{s['description']}</p>
<p><a href="{s['site_url']}" target="_blank" rel="nofollow">Visit site</a> • Listed on The Web Hospitality free link exchange directory.</p>
<a class="btn" href="{SITE_URL}/exchange/{s['id']}">Request Link Exchange — Free</a>
</div></body></html>"""
    return html


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
