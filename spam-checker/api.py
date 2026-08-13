#!/usr/bin/env python3
"""Free Spam Checker API — runs on your server, no paid keys.

Endpoints:
  GET /api/check?domain=example.com   -> JSON verdict
  GET /                               -> demo/test page

Run:  python3 api.py   (default port 5050)
"""
import argparse
from flask import Flask, jsonify, request
from flask_cors import CORS
import spamcheck

app = Flask(__name__)
CORS(app)  # allow embedding from any site

@app.route("/api/check")
def api_check():
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "Missing 'domain' parameter",
                        "example": "/api/check?domain=example.com"}), 400
    try:
        result = spamcheck.check_domain(domain)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    status = 200 if result.get("verdict") != "invalid" else 400
    return jsonify(result), status


@app.route("/")
def index():
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free Spam Checker</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;background:#0f1117;color:#e6e6e6}
  h1{font-size:1.6rem} input{padding:10px 14px;font-size:1rem;border-radius:8px;border:1px solid #444;background:#1a1d27;color:#fff;width:60%}
  button{padding:10px 18px;font-size:1rem;border-radius:8px;border:0;background:#4f8cff;color:#fff;cursor:pointer}
  .card{background:#1a1d27;border:1px solid #2a2e3d;border-radius:12px;padding:20px;margin-top:20px}
  .clean{color:#3ddc84}.spam{color:#ff5c5c}.suspicious{color:#ffb020}
  table{width:100%;border-collapse:collapse;margin-top:12px}
  td,th{text-align:left;padding:8px;border-bottom:1px solid #2a2e3d;font-size:.92rem}
  .badge{display:inline-block;padding:4px 12px;border-radius:20px;font-weight:700;font-size:.85rem}
  .badge.clean{background:#123524;color:#3ddc84}.badge.spam{background:#3a1216;color:#ff5c5c}
  .badge.suspicious{background:#3a2d12;color:#ffb020}
</style>
</head>
<body>
<h1>🛡️ Free Spam Checker</h1>
<p>Check any domain against Spamhaus, SpamCop, Barracuda + reputation heuristics. No API key needed.</p>
<input id="d" placeholder="example.com" value="thewebhospitality.com">
<button onclick="run()">Check</button>
<div id="out"></div>
<script>
async function run(){
  const d=document.getElementById('d').value.trim();
  const out=document.getElementById('out');
  out.innerHTML='<p>Checking…</p>';
  const r=await fetch('/api/check?domain='+encodeURIComponent(d));
  const j=await r.json();
  if(j.error){out.innerHTML='<div class="card">❌ '+j.error+'</div>';return;}
  const v=j.verdict;
  const badge=v==='clean'?'<span class="badge clean">CLEAN</span>':v==='spam'?'<span class="badge spam">SPAM</span>':'<span class="badge suspicious">SUSPICIOUS</span>';
  let rows=j.checks.map(c=>`<tr><td>${c.check}</td><td>${c.listed?'⚠️':'✅'}</td><td>${c.detail}</td></tr>`).join('');
  out.innerHTML=`<div class="card">
    <h2>${j.domain} ${badge}</h2>
    <p>Spam score: <b>${j.score}%</b> | IP: ${j.ip||'n/a'} | DNSBL hits: ${j.dnsbl_hits.join(', ')||'none'}</p>
    <table><tr><th>Check</th><th></th><th>Detail</th></tr>${rows}</table>
  </div>`;
}
run();
</script>
</body>
</html>"""
