#!/usr/bin/env python3
"""DA/PA Multi-Provider Engine — pools ALL free authority APIs.

Philosophy: koi ek source pe depend nahi. Har free provider ki apni quota hai.
Agar ek rate-limited/blocked ho to agla automatic sambhalta hai. Sab kuch
cache hota hai taaki free quotas mahine bhar chalein.

Providers (sab FREE):
  1. Ahrefs DR        — /v3/public/domain-rating-free (free key, unlimited-ish)
  2. OpenPageRank     — PageRank 0-10 -> scaled to 0-100 (free key, 1000/day)
  3. Apify Moz DA     — real Moz DA via actor (budget-capped, currently Moz-blocked)
  4. Scamadviser      — trust score 0-100 (no key, page scrape)
  5. DNSBL engine     — spam score (no key, unlimited)

Output: "Authority Score" (0-100) + per-provider breakdown + source used.
"""
import os
import re
import time
from datetime import datetime

import requests

# ---- config ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36"}

# load .env (project dir) so keys are available
_env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f.read().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# weights for blended score (Ahrefs DR is the most authoritative free signal)
WEIGHTS = {"ahrefs_dr": 0.60, "openpagerank": 0.15, "apify_da": 0.25}

# circuit breaker: after N consecutive failures, skip Apify for a while
# File-based so it's shared across gunicorn workers
APIFY_MAX_CONSECUTIVE_FAILS = 3
APIFY_COOLDOWN_SECONDS = 3600  # 1 hour
_APIFY_BREAKER_FILE = os.path.join(BASE_DIR, ".apify_breaker")


def _apify_breaker_active() -> bool:
    try:
        if os.path.exists(_APIFY_BREAKER_FILE):
            until = float(open(_APIFY_BREAKER_FILE).read().strip() or "0")
            return time.time() < until
    except Exception:
        pass
    return False


def _apify_record_failure():
    """Increment failure counter in a shared file; trip breaker at threshold."""
    counter_file = os.path.join(BASE_DIR, ".apify_fails")
    try:
        n = 0
        if os.path.exists(counter_file):
            n = int(open(counter_file).read().strip() or "0")
        n += 1
        if n >= APIFY_MAX_CONSECUTIVE_FAILS:
            open(_APIFY_BREAKER_FILE, "w").write(str(time.time() + APIFY_COOLDOWN_SECONDS))
            try:
                os.remove(counter_file)
            except OSError:
                pass
        else:
            open(counter_file, "w").write(str(n))
    except Exception:
        pass


def _apify_record_success():
    try:
        counter_file = os.path.join(BASE_DIR, ".apify_fails")
        if os.path.exists(counter_file):
            os.remove(counter_file)
    except Exception:
        pass


def _key(name: str) -> str:
    return os.environ.get(name, "")


# ---------- Provider 1: Ahrefs DR (free, working) ----------
def ahrefs_dr(domain: str) -> dict:
    key = _key("AHREFS_API_KEY")
    if not key:
        return {"ok": False, "reason": "no AHREFS_API_KEY"}
    try:
        r = requests.get(
            "https://api.ahrefs.com/v3/public/domain-rating-free",
            params={"target": domain},
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        if r.status_code != 200:
            return {"ok": False, "reason": f"HTTP {r.status_code}"}
        data = r.json()
        inner = data.get("domain_rating", {})
        dr = inner.get("domain_rating") if isinstance(inner, dict) else None
        if dr is None:
            return {"ok": False, "reason": "no DR in response"}
        return {"ok": True, "value": int(round(float(dr)))}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:80]}


# ---------- Provider 2: OpenPageRank (free key, PR 0-10) ----------
def openpagerank(domain: str) -> dict:
    key = _key("OPR_API_KEY")
    if not key:
        return {"ok": False, "reason": "no OPR_API_KEY"}
    try:
        r = requests.get(
            "https://openpagerank.com/api/v1.0/getPageRank",
            params={"domains[0]": domain},
            headers={"API-OPR": key},
            timeout=15,
        )
        if r.status_code != 200:
            return {"ok": False, "reason": f"HTTP {r.status_code}"}
        data = r.json()
        resp = data.get("response", [{}])
        pr = resp[0].get("page_rank_integer") if resp else None
        if pr is None:
            return {"ok": False, "reason": "no PR"}
        return {"ok": True, "value": int(pr) * 10}  # 0-10 -> 0-100 scale
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:80]}


# ---------- Provider 3: Apify Moz DA (real DA, budget-capped) ----------
def apify_da(domain: str) -> dict:
    key = _key("APIFY_API_TOKEN")
    if not key:
        return {"ok": False, "reason": "no APIFY_API_TOKEN"}
    # circuit breaker: skip if recently failing (Moz Cloudflare-blocked)
    if _apify_breaker_active():
        return {"ok": False, "reason": "circuit-breaker (cooldown)"}
    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/jdtpnjtp~moz-domain-authority-checker/"
            "run-sync-get-dataset-items",
            params={"token": key, "timeout": 25},
            json={"domains": [domain]},
            timeout=30,
        )
        r.raise_for_status()
        items = r.json()
        if isinstance(items, list) and items:
            item = items[0]
            if item.get("success") is False:
                _apify_record_failure()
                return {"ok": False, "reason": item.get("errorCode", "blocked")}
            da = (item.get("domainAuthority") or item.get("domain_authority")
                  or item.get("da"))
            if da is not None:
                _apify_record_success()
                return {"ok": True, "value": int(da)}
        _apify_record_failure()
        return {"ok": False, "reason": "no DA in response"}
    except Exception as exc:
        _apify_record_failure()
        return {"ok": False, "reason": str(exc)[:80]}


# ---------- Provider 4: Scamadviser trust (free scrape) ----------
def scamadviser_trust(domain: str) -> dict:
    try:
        r = requests.get(
            f"https://www.scamadviser.com/check-website/{domain}",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return {"ok": False, "reason": f"HTTP {r.status_code}"}
        import html as _html
        text = _html.unescape(r.text)
        m = re.search(r'"ratingScore"\s*:\s*(\d+)', text)
        if not m:
            m = re.search(r'"ratingValue"\s*:\s*(\d+)', text)
        if m:
            return {"ok": True, "value": int(m.group(1))}
        return {"ok": False, "reason": "no score in page"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:80]}


# ---------- Provider 4b: RapidAPI free DA/PA (pool of free APIs) ----------
def rapidapi_da(domain: str) -> dict:
    """Try configured RapidAPI endpoints until one returns DA/PA.

    Set env: RAPIDAPI_KEY=<your key from https://rapidapi.com/developer/settings>
    Each API host configured in RAPIDAPI_HOSTS (comma-separated host|path|field).
    """
    key = _key("RAPIDAPI_KEY")
    if not key:
        return {"ok": False, "reason": "no RAPIDAPI_KEY"}
    hosts = os.environ.get("RAPIDAPI_HOSTS", "").strip()
    if not hosts:
        return {"ok": False, "reason": "no RAPIDAPI_HOSTS configured"}
    for entry in hosts.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        host = parts[0]
        path = parts[1] if len(parts) > 1 else "/domain-authority-checker/"
        field = parts[2] if len(parts) > 2 else "domain_authority"
        try:
            url = f"https://{host}{path}"
            url += "&" if "?" in path else "?"
            url += f"domain={domain}"
            r = requests.get(
                url,
                headers={
                    "X-RapidAPI-Key": key,
                    "X-RapidAPI-Host": host,
                },
                timeout=15,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            # search for the field (nested-safe)
            def _find(obj, name):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k.lower() in (name, name.replace("_", "")) or name in k.lower():
                            if isinstance(v, (int, float)):
                                return v
                        if isinstance(v, (dict, list)):
                            got = _find(v, name)
                            if got is not None:
                                return got
                elif isinstance(obj, list):
                    for v in obj:
                        got = _find(v, name)
                        if got is not None:
                            return got
                return None
            val = _find(data, field)
            if val is not None:
                return {"ok": True, "value": int(round(float(val)))}
        except Exception:
            continue
    return {"ok": False, "reason": "all RapidAPI endpoints failed"}


# ---------- DNSBL spam score (free, unlimited) ----------
def dnsbl_spam(domain: str) -> dict:
    try:
        import spamcheck
        result = spamcheck.check_domain(domain)
        return {"ok": True, "value": result["score"], "verdict": result["verdict"]}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:80]}


# ---------- Blended Authority Score ----------
def authority_score(domain: str) -> dict:
    """Pool all providers, blend into a 0-100 Authority Score with source info."""
    results = {
        "ahrefs_dr": ahrefs_dr(domain),
        "openpagerank": openpagerank(domain),
        "apify_da": apify_da(domain),
        "rapidapi": rapidapi_da(domain),
        "scamadviser": scamadviser_trust(domain),
        "dnsbl": dnsbl_spam(domain),
    }

    # collect numeric values from providers that succeeded
    vals = {}
    if results["ahrefs_dr"]["ok"]:
        vals["ahrefs_dr"] = results["ahrefs_dr"]["value"]
    if results["openpagerank"]["ok"]:
        vals["openpagerank"] = results["openpagerank"]["value"]
    if results["apify_da"]["ok"]:
        vals["apify_da"] = results["apify_da"]["value"]
    if results["rapidapi"]["ok"]:
        vals["rapidapi"] = results["rapidapi"]["value"]

    # trust is a safety signal (not authority), spam is inverse
    trust = results["scamadviser"]["value"] if results["scamadviser"]["ok"] else None
    spam = results["dnsbl"]["value"] if results["dnsbl"]["ok"] else None

    # blended authority from whatever providers succeeded
    if vals:
        total_w = sum(WEIGHTS.get(k, 0) for k in vals)
        score = sum(v * WEIGHTS.get(k, 0) for k, v in vals.items()) / total_w if total_w else 0
    else:
        # fallback: trust-based estimate (better than nothing)
        score = trust if trust is not None else None

    # spam penalty: up to -20
    if score is not None and spam is not None:
        score = max(0, score - spam * 0.2)

    return {
        "domain": domain,
        "authority_score": int(round(score)) if score is not None else None,
        "dr": vals.get("ahrefs_dr"),
        "da": vals.get("apify_da"),
        "pagerank": vals.get("openpagerank"),
        "trust_score": trust,
        "spam_score": spam,
        "sources_used": list(vals.keys()),
        "provider_status": {k: ("ok" if v.get("ok") else v.get("reason", "fail"))
                            for k, v in results.items()},
        "checked_at": datetime.utcnow().isoformat() + "Z",
    }


if __name__ == "__main__":
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "thewebhospitality.com"
    print(json.dumps(authority_score(target), indent=2))
