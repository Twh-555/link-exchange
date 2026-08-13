#!/usr/bin/env python3
"""Free Spam Checker Engine — keyless, DNS-based + heuristics.

Checks:
  1. Spamhaus DBL  — domain blocklist (spam domains)
  2. Spamhaus ZEN  — IP blocklist (SBL/XBL/PBL)
  3. SpamCop       — IP blocklist
  4. Barracuda     — IP blocklist
  5. Heuristics    — domain age, MX records, HTTPS, suspicious TLD/keywords

Returns a JSON verdict: clean / suspicious / spam + per-check details + score.
No API keys required. Rate limit friendly (DNS lookups only).
"""
import json
import re
import socket
import ssl

import requests
from datetime import datetime

# ---- DNSBL definitions: (label, query builder, explanation) ----
DNSBL_DOMAIN = [
    ("Spamhaus DBL", lambda d: f"{d}.dbl.spamhaus.org", "Domain listed on Spamhaus DBL (spam domains)"),
]
DNSBL_IP = [
    ("Spamhaus ZEN", lambda ip: f"{_rev(ip)}.zen.spamhaus.org", "IP listed on Spamhaus ZEN (SBL/XBL/PBL)"),
    ("SpamCop",      lambda ip: f"{_rev(ip)}.bl.spamcop.net",  "IP listed on SpamCop (spam sources)"),
    ("Barracuda",    lambda ip: f"{_rev(ip)}.b.barracudacentral.org", "IP listed on Barracuda Reputation"),
]

SUSPICIOUS_TLDS = {"tk", "ml", "ga", "cf", "gq", "xyz", "top", "icu", "club",
                   "work", "download", "stream", "review", "loan", "date"}
SUSPICIOUS_KEYWORDS = ["casino", "porn", "xxx", "sex", "viagra", "crypto", "bitcoin",
                       "free-money", "lottery", "win", "prize", "gambl", "loan",
                       "pharma", "pill", "discount", "cheap", "buy-online"]

MAX_IP_DNS_TIMEOUT = 6  # seconds


def _rev(ip: str) -> str:
    return ".".join(reversed(ip.split(".")))


def _resolve(host: str, timeout: float = 5.0) -> bool:
    """Returns True if hostname resolves (i.e. listed)."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.gethostbyname(host)
        return True
    except (socket.gaierror, OSError):
        return False
    finally:
        socket.setdefaulttimeout(old)


def _mx_records(domain: str) -> list:
    """MX records via DNS-over-HTTPS (Cloudflare) — no dnspython dependency.

    Returns list of exchange hostnames. Empty = no MX (spam signal).
    """
    try:
        r = requests.get(
            f"https://cloudflare-dns.com/dns-query?name={domain}&type=MX",
            headers={"Accept": "application/dns-json"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        answers = data.get("Answer", [])
        mx = []
        for a in answers:
            if a.get("type") == 15:  # MX record
                # rdata like "10 aspmx.l.google.com."
                parts = a.get("data", "").split()
                if len(parts) >= 2:
                    mx.append(parts[-1].rstrip("."))
        return mx
    except Exception:
        return []


def _domain_ip(domain: str) -> str | None:
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return None


def _https_works(domain: str) -> bool:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                return bool(ssock.getpeercert())
    except Exception:
        return False


def _whois_age_days(domain: str) -> int | None:
    try:
        import whois
        w = whois.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = created[0]
        if created:
            return (datetime.now() - created).days
    except Exception:
        pass
    return None


def check_domain(domain: str) -> dict:
    """Run all checks on a domain. Returns structured verdict."""
    domain = domain.strip().lower().replace("http://", "").replace("https://", "")
    domain = re.sub(r"^(www\.)", "", domain).strip("/").split("/")[0]
    if not re.match(r"^[a-z0-9\-\.]+\.[a-z]{2,}$", domain):
        return {"error": f"Invalid domain: {domain}", "verdict": "invalid"}

    checks = []
    dnsbl_hits = []

    # --- DNSBL: domain-based ---
    for label, builder, desc in DNSBL_DOMAIN:
        try:
            hit = _resolve(builder(domain))
        except Exception:
            hit = False
        checks.append({
            "check": label, "type": "domain", "listed": hit,
            "detail": desc + (" — LISTED!" if hit else " — not listed"),
        })
        if hit:
            dnsbl_hits.append(label)

    # --- IP-based DNSBLs ---
    ip = _domain_ip(domain)
    if ip:
        for label, builder, desc in DNSBL_IP:
            try:
                hit = _resolve(builder(ip), timeout=MAX_IP_DNS_TIMEOUT)
            except Exception:
                hit = False
            checks.append({
                "check": label, "type": "ip", "listed": hit,
                "detail": f"{desc} (IP {ip})" + (" — LISTED!" if hit else " — not listed"),
            })
            if hit:
                dnsbl_hits.append(label)

    # --- Heuristics ---
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    suspicious_tld = tld in SUSPICIOUS_TLDS
    suspicious_kw = [k for k in SUSPICIOUS_KEYWORDS if k in domain]

    mx = _mx_records(domain)
    has_mx = bool(mx)
    https_ok = _https_works(domain)
    age_days = _whois_age_days(domain)
    too_young = age_days is not None and age_days < 90

    checks.append({"check": "TLD reputation", "type": "heuristic",
                   "listed": suspicious_tld,
                   "detail": f"TLD .{tld} {'is high-risk (free/spam-heavy TLD)' if suspicious_tld else 'is normal'}"})
    checks.append({"check": "Domain keywords", "type": "heuristic",
                   "listed": bool(suspicious_kw),
                   "detail": f"Spammy keywords in domain: {suspicious_kw}" if suspicious_kw else "No spammy keywords in domain"})
    checks.append({"check": "MX records", "type": "heuristic",
                   "listed": not has_mx,
                   "detail": f"MX: {', '.join(mx[:3]) if mx else 'NONE — cannot receive email (common for throwaway spam domains)'}"})
    checks.append({"check": "HTTPS / SSL", "type": "heuristic",
                   "listed": not https_ok,
                   "detail": "Valid SSL certificate — trustworthy" if https_ok else "No/expired SSL — often a sign of abandoned or spam site"})
    if age_days is not None:
        checks.append({"check": "Domain age", "type": "heuristic",
                       "listed": too_young,
                       "detail": f"Domain age: {age_days} days" + (" — very young (spam risk)" if too_young else "")})

    # --- Score ---
    listed_count = sum(1 for c in checks if c["listed"])
    total = len(checks)
    ratio = listed_count / total if total else 0

    if dnsbl_hits:
        verdict = "spam"
    elif ratio >= 0.5 or (too_young and not https_ok):
        verdict = "suspicious"
    else:
        verdict = "clean"

    return {
        "domain": domain,
        "ip": ip,
        "verdict": verdict,
        "score": round(ratio * 100),  # 0 = clean, 100 = max spam signals
        "dnsbl_hits": dnsbl_hits,
        "checks": checks,
        "checked_at": datetime.utcnow().isoformat() + "Z",
    }


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "thewebhospitality.com"
    print(json.dumps(check_domain(target), indent=2))
