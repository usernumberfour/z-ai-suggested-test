#!/usr/bin/env python3
"""Sam's daily wrap up - Path C V2.6 (deterministic fetch + AI editorial + push
+ delivery self-report: every run writes its delivery results into the edition
footer and the Actions step summary, keeping 'Saved to:' as the last line).

GitHub Actions runs this nightly via an evening grid of 10-minute slots
(21:05-21:55 plus 22:05-22:55 Melbourne, DST-safe): the first slot that fires
produces the edition, every other slot self-skips (duplicate + DST guards).
Reads config/feeds.yaml, fetches RSS feeds + market endpoints, optionally lets
an LLM (Google Gemini) perform editorial selection/threading on the fetched pool,
writes editions/daily-wrapup-YYYY-MM-DD.md, optionally delivers via Discord /
Telegram / WhatsApp / Email if the corresponding secrets are present.

AI editorial layer (V2):
  - only selects from fetched items; never invents items, URLs, timestamps or numbers
  - markets block is 100% deterministic and never passes through the LLM
  - output is validated (region headers, every link must exist in the pool);
    invalid or unreachable AI -> honest deterministic fallback, cadence never depends on it

Market data tiers (per index):
  1. TradingView scanner  - one POST returns ~16 indices (primary)
  2. TradingEconomics     - ASX 200 only (server-rendered page, date-checked)
  3. Yahoo v8 chart       - kept as fallback (rate-limited on some cloud IPs)
  4. Stooq CSV            - last resort
  5. honest "unavailable (...)" - never faked

Guards:
  DST guard       - only produces during the 21:00 Melbourne hour window
                    (handles AEDT/AEST automatically via double cron).
  Duplicate guard - if today's edition already exists complete, skip cleanly.
"""
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import yaml

MELB = ZoneInfo("Australia/Melbourne")
UTC = timezone.utc
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EDITIONS_DIR = os.environ.get("EDITIONS_DIR") or os.path.join(ROOT, "editions")
FOOTER_MARK = "Saved to:"


def log(msg):
    print(msg, flush=True)


def http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- items

def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s):
    return set(re.findall(r"[a-z0-9]{4,}", s.lower()))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def entry_ts(e):
    st = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if not st:
        return None
    try:
        return datetime(*st[:6], tzinfo=UTC)
    except Exception:
        return None


def fetch_region(region, cfg, now_utc, win_start):
    """Return (items, feeds_ok, feeds_failed). Items: fresh, deduped, capped."""
    items, fed_ok, fed_fail = [], 0, []
    n_feeds = 0
    for feed in region.get("feeds", []):
        n_feeds += 1
        name, url = feed["name"], feed["url"]
        require = feed.get("require_any") or region.get("require_any")
        fresh = 0
        try:
            data = http_get(url)
            d = feedparser.parse(data)
            for e in d.entries:
                pub = entry_ts(e)
                if pub is None:
                    continue  # no verifiable timestamp -> excluded (timestamp discipline)
                if pub < win_start or pub > now_utc + timedelta(minutes=15):
                    continue
                title = strip_tags(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                if not title or not link.startswith("http"):
                    continue
                blob = title + " " + strip_tags(getattr(e, "summary", "") or "")
                if require and not any(re.search(re.escape(k), blob, re.I) for k in require):
                    continue
                items.append({"title": title[:150], "url": link, "source": name, "pub": pub})
                fresh += 1
            fed_ok += 1
            log(f"    feed {name}: {fresh} fresh / {len(d.entries)} total")
        except Exception as ex:
            fed_fail.append(f"{name} ({type(ex).__name__})")
            log(f"    feed {name}: FAILED {type(ex).__name__}")
    # dedupe by title similarity, keep first-seen (priority order)
    kept = []
    for it in items:
        nt = norm_title(it["title"])
        if any(jaccard(nt, norm_title(k["title"])) >= 0.55 for k in kept):
            continue
        kept.append(it)
    kept.sort(key=lambda x: x["pub"], reverse=True)
    cap = cfg.get("items_per_region", 5)
    return kept[:cap], n_feeds, fed_ok, fed_fail


def fmt_item(it):
    t = it["pub"].astimezone(MELB)
    return f"- [{it['title']}]({it['url']}) - {it['source']} - {t.strftime('%H:%M, %a %d %b')} Melbourne"


# ---------------------------------------------------------------- markets

def yahoo_quote(sym):
    """v8 chart endpoint; alternates hosts and retries transient failures
    (429/5xx/network) with backoff. Permanent failures (404) raise fast."""
    import urllib.request
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com",
             "query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for attempt, host in enumerate(hosts):
        if attempt:
            time.sleep(4 * attempt)  # backoff 4s, 8s, 12s
        url = (f"https://{host}/v8/finance/chart/"
               + urllib.request.quote(sym) + "?interval=1d&range=5d")
        try:
            j = json.loads(http_get(url).decode())
            meta = j["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if not price or not prev:
                raise ValueError("missing price fields")
            return float(price), float(prev)
        except Exception as ex:
            code = getattr(ex, "code", None)
            if code not in (429, 500, 502, 503) and code is not None:
                raise  # permanent (e.g. 404 bad symbol) - fail fast
            last = ex
            continue
    raise last


def stooq_quote(sym):
    rows = http_get(f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
                    timeout=15).decode().strip().splitlines()
    close = float(rows[1].split(",")[3])
    d1 = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y%m%d")
    d2 = datetime.now(UTC).strftime("%Y%m%d")
    hist = http_get(f"https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d",
                    timeout=15).decode().strip().splitlines()
    closes = [float(r.split(",")[4]) for r in hist[1:] if len(r.split(",")) > 4]
    prev = closes[-2] if len(closes) >= 2 else closes[-1]
    return close, prev


# TradingView tickers keyed by the market name in config/feeds.yaml.
# A market may override via `tv_symbol:` in config (config stays the source of truth).
TV_SYMBOLS = {
    "S&P/NZX 50": "NZX:NZ50G",
    "SET": "SET:SET",
    "STI": "TVC:STI",
    "JCI": "IDX:COMPOSITE",
    "Nifty 50": "NSE:NIFTY",
    "Sensex": "BSE:SENSEX",
    "Tadawul TASI": "TADAWUL:TASI",
    "FTSE 100": "TVC:UKX",
    "DAX": "XETR:DAX",
    "EURO STOXX 50": "TVC:SX5E",
    "JSE Top 40 (Satrix 40 proxy)": "JSE:STX40",
    "EGX 30": "EGX:EGX30",
    "Ibovespa": "BMFBOVESPA:IBOV",
    "S&P 500": "SP:SPX",
    "Dow": "DJ:DJI",
    "Nasdaq": "NASDAQ:IXIC",
}


def tv_scan(symbols):
    """One POST to TradingView's public scanner for all index symbols.
    Returns {ticker: (close, pct)}; absent/unpriced symbols omitted."""
    import urllib.request
    url = "https://scanner.tradingview.com/global/scan"
    payload = {"symbols": {"tickers": sorted(set(symbols)), "query": {"types": []}},
               "columns": ["close", "change"]}
    last = None
    for attempt in range(2):
        if attempt:
            time.sleep(3)
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={**UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode())
            out = {}
            for row in j.get("data", []):
                sym, d = row.get("s"), row.get("d") or []
                if sym and len(d) >= 2 and d[0] is not None and d[1] is not None:
                    out[sym] = (float(d[0]), float(d[1]))
            return out
        except Exception as ex:
            last = ex
    raise last


_MONTHS = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
           "July": 7, "August": 8, "September": 9, "October": 10,
           "November": 11, "December": 12}
_TE_NEG = ("losing", "falling", "dropping", "slipping")
_TE_NEG_VERBS = ("fell", "slid", "dropped", "tumbled", "lost", "declined")


def te_asx_quote():
    """ASX 200 from TradingEconomics (server-rendered page).
    The meta description carries a rounded level + % change + the session date;
    the page body carries the exact level. Returns (close, pct).
    Raises if the page is stale (> 6 days), WAF-blocked, or unparseable -
    a stale page must never enter an edition."""
    t = http_get("https://tradingeconomics.com/australia/stock-market",
                 timeout=25).decode("utf-8", "ignore")
    m = re.search(r"the ASX\s?200,?\s+(\w+) to ([\d,]+) points on (\w+) (\d{1,2}), (\d{4})"
                  r"(?:, (losing|gaining|falling|rising|dropping|climbing) ([\d.]+)%)?", t)
    if not m:
        raise ValueError("te meta description not found")
    verb, level_s, mon, day, year, signword, pct_s = m.groups()
    # date sanity: TE must be showing the last few sessions, not a stale page
    try:
        te_date = datetime(int(year), _MONTHS[mon], int(day), tzinfo=UTC)
    except (KeyError, ValueError):
        raise ValueError("te date unparseable")
    if datetime.now(UTC) - te_date > timedelta(days=6):
        raise ValueError(f"te page stale (session {mon} {day}, {year})")
    target = float(level_s.replace(",", ""))
    # exact level: pick the page candidate closest to the rounded meta level
    close = target
    best = None
    for c in re.findall(r"(\d{1,2},\d{3}\.\d{2})", t):
        v = float(c.replace(",", ""))
        if best is None or abs(v - target) < abs(best - target):
            best = v
    if best is not None and abs(best - target) <= 0.6:
        close = best
    if signword:
        pct = float(pct_s) * (-1.0 if signword in _TE_NEG else 1.0)
    elif verb in _TE_NEG_VERBS:
        pct = -float(pct_s)
    else:
        pct = float(pct_s)
    return close, pct


# Fallback market-state entries for indices that were once marked 'unavailable'
# in config and therefore carry no tz/hours (config keys always win when present).
_STATE_DEFAULTS = {
    "EGX 30": {"tz": "Africa/Cairo", "open": "10:00", "close": "14:30",
               "days": [6, 0, 1, 2, 3]},
}


def market_state(m):
    st = dict(_STATE_DEFAULTS.get(m["name"], {}))
    st.update({k: v for k, v in m.items() if k in ("tz", "open", "close", "days")})
    if "tz" not in st:
        return "closed"
    now_local = datetime.now(ZoneInfo(st["tz"]))
    days = st.get("days", [0, 1, 2, 3, 4])
    o = datetime.strptime(st["open"], "%H:%M").time()
    c = datetime.strptime(st["close"], "%H:%M").time()
    if now_local.weekday() in days and o <= now_local.time() <= c:
        return "still trading"
    return "closed"


def pct_str(price, prev):
    p = (price / prev - 1.0) * 100.0
    flag = " (unusually large move - verify)" if abs(p) >= 12 else ""
    return f"{'+' if p >= 0 else '-'}{abs(p):.2f}%{flag}"


def fetch_markets(cfg):
    """Return (lines, markets_ok, markets_failed).
    Tiers per index: TradingView scan -> TradingEconomics (ASX 200) ->
    Yahoo (circuit-breaked) -> Stooq -> honest unavailable."""
    lines, ok, failed = [], 0, 0
    consec_429, cooldown = 0, False
    # Tier 1: one TradingView request covers most indices
    tv = {}
    wanted = []
    for m in cfg["markets"]:
        sym = m.get("tv_symbol") or TV_SYMBOLS.get(m["name"])
        if sym:
            wanted.append(sym)
    if wanted:
        try:
            tv = tv_scan(wanted)
            log(f"    TradingView scan: {len(tv)}/{len(wanted)} symbols OK")
        except Exception as ex:
            log(f"    TradingView scan FAILED {type(ex).__name__} - per-index fallbacks engaged")
    groups = {}
    for m in cfg["markets"]:
        groups.setdefault(m["group"], []).append(m)
    for gname, ms in groups.items():
        lines.append(f"**{gname}**")
        for m in ms:
            price = prev = None
            # Tier 1: TradingView (a live TV quote also overrides a stale
            # 'unavailable' note in config, e.g. EGX 30 now carried by TV)
            tvsym = m.get("tv_symbol") or TV_SYMBOLS.get(m["name"])
            if tvsym and tvsym in tv:
                price, pct = tv[tvsym]
                prev = price / (1.0 + pct / 100.0)
            if price is None and m.get("unavailable"):
                lines.append(f"- {m['name']}: unavailable ({m['unavailable']})")
                failed += 1
                continue
            # Tier 2: TradingEconomics - ASX 200 only
            if price is None and m["name"] == "S&P/ASX 200":
                try:
                    price, pct = te_asx_quote()
                    prev = price / (1.0 + pct / 100.0)
                    log(f"    market {m['name']}: TradingEconomics OK")
                except Exception as ex:
                    log(f"    market {m['name']}: TradingEconomics failed {type(ex).__name__}: {ex}")
            # Tier 3: Yahoo (circuit-breaked; 429s are common from cloud IPs)
            if price is None and not cooldown:
                try:
                    time.sleep(1.2)  # spacing between Yahoo calls: avoid burst rate limits
                    price, prev = yahoo_quote(m["yahoo"])
                    consec_429 = 0
                except Exception as ex:
                    code = getattr(ex, "code", None)
                    if code == 429:
                        consec_429 += 1
                        if consec_429 >= 2:
                            cooldown = True
                            log("    MARKET COOLDOWN: Yahoo rate-limited (2 consecutive 429s) - "
                                "remaining indices will fall back to Stooq or be disclosed unavailable")
                    log(f"    market {m['name']}: yahoo failed {type(ex).__name__}")
            # Tier 4: Stooq
            if price is None and m.get("stooq"):
                try:
                    price, prev = stooq_quote(m["stooq"])
                except Exception as ex2:
                    log(f"    market {m['name']}: stooq failed {type(ex2).__name__}")
            if price is None:
                reason = "rate limited - endpoint cooldown" if cooldown else "feed failure"
                lines.append(f"- {m['name']}: unavailable ({reason})")
                failed += 1
                continue
            ok += 1
            lines.append(f"- {m['name']}: {price:,.2f} ({pct_str(price, prev)}) - {market_state(m)}")
        lines.append("")
    return lines, ok, failed


# ---------------------------------------------------------------- AI editorial

TRANSIENT_BACKOFF_S = 10   # wait before retrying after 429/5xx (high demand)

AI_PROMPT = """You are the editor of "Sam's daily wrap up", a nightly 21:00 Melbourne digest.\nBelow is a JSON pool of today's freshly fetched news items, grouped by region,\nplus nothing else. Produce the edited edition body.\n\nRULES\n1. For each region, select 0 to 5 items (up to 5, fewer if only a few deserve it).\n   Prefer: developing stories, fresh angles, regional significance, recency.\n2. If two regions carry essentially the same story, keep the stronger one; in the\n   weaker region either drop it or keep it with an indented note\n   "  _↳ same story: see <other region>_".\n3. You may add at most ONE indented context note after a kept item:\n   "  _↳ <max 12 words of context>_" - use sparingly (at most 4 per region).\n4. HARD RULES: never invent or alter items, titles, URLs, sources, timestamps or\n   numbers. Every kept item must use the EXACT title, url, source and time given.\n   No item may appear that is not in the pool.\n5. Output format EXACTLY, nothing before or after:\n## <region title>\n\n- [<exact title>](<exact url>) - <exact source> - <exact time> Melbourne\n\n(one blank line after each region; when a region has nothing worth keeping,\nwrite "None." alone where the items would go; do not add any other headings,\nno markets, no footer, no commentary)\n\nPOOL JSON:\n"""


def validate_ai_regions(ai_text, pool_urls, region_titles):
    """Return cleaned body text if the AI output is structurally valid, else None.
    Checks: exact region headers in order, no stray top-level headings,
    every linked URL must exist in the fetched pool."""
    if not ai_text or "<" in ai_text[:3]:
        return None
    headers = [l[3:].strip() for l in ai_text.splitlines() if l.startswith("## ")]
    if headers != region_titles:
        return None
    if re.search(r"(?m)^#(?!#)", ai_text):  # any single-# heading is forbidden
        return None
    urls = re.findall(r"\]\((https?://[^)\s]+)\)", ai_text)
    if any(u not in pool_urls for u in urls):
        return None
    for l in ai_text.splitlines():
        if l.startswith("- [") and ("](" not in l or " - " not in l):
            return None
    return ai_text.strip() + "\n"


def _gemini_call(model, payload, key, timeout=240):
    """One generateContent request. Raises urllib.error.HTTPError on HTTP errors
    and TimeoutError/URLError when the model does not answer in time."""
    import urllib.request
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**UA, "Content-Type": "application/json",
                 "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _gemini_discover_model(key, exclude=None):
    """Query the Gemini ListModels endpoint and pick the best available text
    model for this key. Preference: gemini-4-hydrogen, the flash-latest alias,
    then newest flash, then flash-lite tiers, then any gemini model supporting
    generateContent. `exclude` takes one model id or a set: no model in it is
    ever re-picked, so a second fallback can never return to an already-failed
    model. Returns the model id string, or None if nothing usable is found."""
    import urllib.request
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={**UA, "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=60) as resp:
        j = json.loads(resp.read().decode())
    excl = {exclude} if isinstance(exclude, str) else set(exclude or ())
    names = []
    for m in j.get("models") or []:
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        n = (m.get("name") or "").replace("models/", "")
        if n.startswith("gemini-") and n not in excl:
            names.append(n)
    for pref in ("gemini-4-hydrogen", "gemini-flash-latest", "gemini-3.8-flash",
                 "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
        if pref in names:
            return pref
    cands = [n for n in names if not any(
        t in n for t in ("embed", "tts", "image", "vlm", "thinking"))] or names
    return sorted(cands)[-1] if cands else None


def ai_edition(cfg, regions_out, now_melb):
    """Call the Google Gemini API to edit the edition body from the fetched pool.
    Resilience queue (max 4 attempts; each failure class enqueues its recovery):
      404 (model retired)   -> ListModels discovery, retry on another model
      429/5xx (high demand) -> wait, retry same model, then switch models
      timeout               -> retry with thinking disabled
    Discovery never re-picks a model that already failed this run.
    Returns (body_lines, model_string). Raises on any failure - the caller
    falls back to the deterministic edition. Markets never enter this function."""
    import urllib.request
    import urllib.error
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise ValueError("no LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model or model.startswith("glm-"):
        # Google-recommended for new projects (models docs, Nov 2026). Any 404
        # self-heals via ListModels discovery, so a wrong pin cannot cost a night.
        model = "gemini-4-hydrogen"
    pool = {
        "edition_date": now_melb.strftime("%a %d %b %Y"),
        "regions": [],
    }
    pool_urls = set()
    for r in cfg["regions"]:
        items, _, _, _ = regions_out[r["id"]]
        entry = {"title": r["title"], "items": []}
        for it in items:
            entry["items"].append({
                "title": it["title"],
                "source": it["source"],
                "time": it["pub"].astimezone(MELB).strftime("%H:%M, %a %d %b"),
                "url": it["url"],
            })
            pool_urls.add(it["url"])
        pool["regions"].append(entry)
    prompt = AI_PROMPT + json.dumps(pool, ensure_ascii=False)
    base_cfg = {"temperature": 0.4, "maxOutputTokens": 8000}
    TRANSIENT = {429, 500, 502, 503, 504}   # high-demand / server-hiccup codes

    def _payload(extra_cfg):
        return {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": dict(base_cfg, **extra_cfg)}

    # Attempt queue - each failure class enqueues its own recovery attempt:
    #   404                  -> discover a working model (failed model excluded)
    #   429/5xx (high demand)-> wait, retry same model, then switch models
    #   timeout              -> retry with thinking disabled, then plain retry
    #   400 on no-thinking   -> plain retry (thinkingConfig schema drift)
    # Capped at 4 attempts; the caller's deterministic fallback covers exhaustion.
    queue = [(model, {}, "standard")]
    queued = {(model, "standard")}
    failed = set()   # models already rejected this run - discovery never re-picks
    j, error_note = None, "unknown"
    attempts = 0
    while queue and attempts < 4:
        m, extra, tag = queue.pop(0)
        attempts += 1
        try:
            log(f"  AI editorial: attempt {attempts}/4 ({tag}, model={m})")
            j = _gemini_call(m, _payload(extra), key)
            model = m
            break
        except urllib.error.HTTPError as ex:
            error_note = f"HTTP {ex.code}"
            if ex.code == 404:
                reason = ""
                try:
                    reason = ex.read().decode("utf-8", "replace")
                except Exception:
                    pass
                log(f"  AI editorial: {m} returned HTTP 404 "
                    f"({reason.strip()[:120] or 'no body'}) - discovering models")
                failed.add(m)
                alt = _gemini_discover_model(key, exclude=failed)
                if not alt:
                    raise RuntimeError(
                        "Gemini model " + m + " unavailable (404) and "
                        "ListModels returned no usable alternative") from None
                log(f"  AI editorial: switching to {alt}")
                if (alt, "discovered") not in queued:
                    queued.add((alt, "discovered"))
                    queue.insert(0, (alt, {}, "discovered"))
            elif ex.code in TRANSIENT:
                body = ""
                try:
                    body = ex.read().decode("utf-8", "replace")
                except Exception:
                    pass
                log(f"  AI editorial: {m} transient HTTP {ex.code} "
                    f"({body.strip()[:100]}) - backing off {TRANSIENT_BACKOFF_S}s")
                time.sleep(TRANSIENT_BACKOFF_S)
                if tag != "transient-retry":
                    if (m, "transient-retry") not in queued:
                        queued.add((m, "transient-retry"))
                        queue.insert(0, (m, extra, "transient-retry"))
                else:
                    failed.add(m)
                    alt = _gemini_discover_model(key, exclude=failed)
                    if not alt:
                        raise RuntimeError(
                            "Gemini " + error_note + " on " + m +
                            " after retry and no alternative model found") from None
                    log(f"  AI editorial: {m} still busy - switching to {alt}")
                    if (alt, "discovered") not in queued:
                        queued.add((alt, "discovered"))
                        queue.insert(0, (alt, {}, "discovered"))
            elif ex.code == 400 and tag == "no-thinking":
                log("  AI editorial: thinking-disabled rejected (HTTP 400) - plain retry")
                if (m, "plain-retry") not in queued:
                    queued.add((m, "plain-retry"))
                    queue.insert(0, (m, {}, "plain-retry"))
            else:
                raise
        except (TimeoutError, urllib.error.URLError):
            error_note = "timeout"
            if "thinkingConfig" not in extra:
                log(f"  AI editorial: attempt {attempts} timed out - "
                    "retrying with thinking disabled")
                if (m, "no-thinking") not in queued:
                    queued.add((m, "no-thinking"))
                    queue.insert(0, (m, {"thinkingConfig": {"thinkingBudget": 0}},
                                     "no-thinking"))
            else:
                raise
    if j is None:
        err = RuntimeError("all AI editorial attempts exhausted; last error: "
                           + error_note)
        err.code = error_note + " - retries exhausted"
        raise err
    cands = j.get("candidates") or []
    if not cands:
        block = (j.get("promptFeedback") or {}).get("blockReason", "")
        raise ValueError("Gemini returned no candidates"
                         + (f" (blocked: {block})" if block else ""))
    content = "".join(
        p.get("text", "") for p in (cands[0].get("content") or {}).get("parts") or []
    ).strip()
    region_titles = [r["title"] for r in cfg["regions"]]
    body = validate_ai_regions(content, pool_urls, region_titles)
    if body is None:
        raise ValueError("AI output failed validation")
    return body.splitlines(), model


# ---------------------------------------------------------------- WhatsApp push

_WA_REGION_SHORT = {
    "Melbourne & Victoria": "VIC", "Australia (national)": "AU",
    "Pacific": "Pacific", "Southeast Asia": "SEA", "India": "India",
    "Middle East": "Mideast", "Europe": "Europe", "Africa": "Africa",
    "South America": "S.America", "North America": "N.America",
}
_WA_MARKET_PREF = ["S&P/ASX 200", "S&P/NZX 50", "Nifty 50", "Tadawul TASI",
                   "FTSE 100", "S&P 500"]


def wa_parts(text, now_melb):
    """Build the compact WhatsApp digest (1 headline per region + markets + link).
    Pure function: returns a list of message strings (max ~800 chars each)."""
    heads, cur = [], None
    mk = {}
    for l in text.splitlines():
        if l.startswith("## "):
            cur = l[3:].strip()
        elif cur and l.startswith("- [") and "](" in l:
            heads.append((cur, l[3:l.index("](")].strip()))
            cur = None
        else:
            m = re.match(r"- (.+?): [\d,.]+ \(([+\-][\d.]+)%", l)
            if m:
                mk[m.group(1)] = m.group(2) + "%"
    hdr = f"*Sam's daily wrap up* - {now_melb:%a %d %b, %H:%M} Melbourne"
    def short(r):
        return _WA_REGION_SHORT.get(r, r)
    p1 = hdr + "\n" + "\n".join(f"{short(r)}: {t}" for r, t in heads[:5])
    rest = "\n".join(f"{short(r)}: {t}" for r, t in heads[5:])
    sel = [f"{n.replace(' (Satrix 40 proxy)', '')} {mk[n]}" for n in _WA_MARKET_PREF if n in mk]
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    link = f"\nFull edition: https://github.com/{repo}/blob/main/editions/{fname_of(now_melb)}" if repo else ""
    p2 = rest + "\n\nMarkets: " + (", ".join(sel) if sel else "n/a") + link
    return [p[:800] for p in (p1, p2) if p.strip()]


def wa_push(text, now_melb):
    """Send the digest via CallMeBot WhatsApp (free, personal use).
    Secrets: WA_PHONE (with country code) + WA_API_KEY."""
    import urllib.request
    import urllib.parse
    phone = os.environ.get("WA_PHONE", "").strip()
    apikey = os.environ.get("WA_API_KEY", "").strip()
    if not (phone and apikey):
        return 0
    sent = 0
    for msg in wa_parts(text, now_melb):
        q = urllib.parse.urlencode({"phone": phone, "text": msg, "apikey": apikey})
        url = "https://api.callmebot.com/whatsapp.php?" + q
        with urllib.request.urlopen(url, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"callmebot HTTP {r.status}")
        sent += 1
        time.sleep(2)
    return sent


# ---------------------------------------------------------------- render

def render(cfg, regions_out, markets_lines, now_utc, now_melb, feeds_total, feeds_ok, feeds_fail_list, mkt_ok, mkt_failed,
           regions_body=None, ai_note=None):
    start = now_utc - timedelta(hours=cfg.get("window_hours", 24),
                                minutes=cfg.get("tolerance_minutes", 30))
    out = []
    out.append(f"# Sam's daily wrap up - {now_melb.strftime('%a %d %b %Y')} (Melbourne)")
    out.append("")
    out.append("V2 pipeline (GitHub Actions) - RSS feeds + market endpoints "
               "+ AI editorial layer (Gemini, when configured).")
    out.append("")
    out.append(f"Generated: {now_melb.strftime('%H:%M')} Melbourne - "
               f"Coverage window: {start.astimezone(MELB).strftime('%a %d %b %H:%M')} -> "
               f"{now_melb.strftime('%a %d %b %H:%M')} Melbourne")
    out.append("")
    if regions_body is not None:
        out.extend(regions_body)
    else:
        for r in cfg["regions"]:
            items, n_feeds, fed_ok, fed_fail = regions_out[r["id"]]
            out.append(f"## {r['title']}")
            out.append("")
            if items:
                for it in items:
                    out.append(fmt_item(it))
            else:
                out.append("None.")
            notes = []
            if fed_fail:
                notes.append("feeds failed: " + ", ".join(fed_fail))
            if notes:
                out.append("")
                out.append("_(" + "; ".join(notes) + ")_")
            out.append("")
    out += markets_lines
    out.append("---")
    out.append(f"Feeds: {feeds_ok}/{feeds_total} fetched OK"
               + (f" - failed: {', '.join(feeds_fail_list)}" if feeds_fail_list else ""))
    out.append(f"Markets: {mkt_ok} sources OK"
               + (f", {mkt_failed} unavailable" if mkt_failed else ""))
    out.append("Pipeline: Path C V2 - deterministic fetch + market endpoints + AI editorial, "
               "AI sees only fetched items, markets stay deterministic. Sources: config/feeds.yaml.")
    if ai_note:
        out.append(ai_note)
    out.append(f"Saved to: editions/{fname_of(now_melb)}")
    out.append("")
    return "\n".join(out)


def fname_of(now_melb):
    return f"daily-wrapup-{now_melb:%Y-%m-%d}.md"


# ---------------------------------------------------------------- delivery

def deliver(path, text, now_melb):
    sent = []
    title = f"Sam's daily wrap up - {now_melb:%a %d %b %Y} (Melbourne)"
    dw = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if dw:
        try:
            import requests
            r = requests.post(
                dw,
                data={"payload_json": json.dumps({"content": title + " - V1 pipeline"})},
                files={"file": (os.path.basename(path), text.encode("utf-8"), "text/markdown")},
                timeout=30)
            sent.append(f"Discord {r.status_code}")
        except Exception as ex:
            sent.append(f"Discord FAILED {type(ex).__name__}")
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        try:
            import requests
            r = requests.post(
                f"https://api.telegram.org/bot{tok}/sendDocument",
                data={"chat_id": chat, "caption": title},
                files={"document": (os.path.basename(path), text.encode("utf-8"), "text/markdown")},
                timeout=30)
            sent.append(f"Telegram {r.status_code}"
                        + ("" if r.status_code == 200 else " - " + r.text[:150]))
        except Exception as ex:
            sent.append(f"Telegram FAILED {type(ex).__name__}: {str(ex)[:100]}")
    wa = os.environ.get("WA_PHONE", "").strip()
    wak = os.environ.get("WA_API_KEY", "").strip()
    if wa and wak:
        try:
            n = wa_push(text, now_melb)
            sent.append(f"WhatsApp {n} msgs" if n else "WhatsApp skipped (no secrets)")
        except Exception as ex:
            sent.append(f"WhatsApp FAILED {type(ex).__name__}: {str(ex)[:60]}")
    guser = os.environ.get("GMAIL_USER", "").strip()
    gpass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    eto = os.environ.get("EMAIL_TO", "").strip()
    if guser and gpass and eto:
        try:
            import smtplib, ssl
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = guser
            msg["To"] = eto
            msg.set_content(text)
            msg.add_attachment(text.encode("utf-8"), maintype="text", subtype="markdown",
                               filename=os.path.basename(path))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                                  context=ssl.create_default_context()) as s:
                s.login(guser, gpass)
                s.send_message(msg)
            sent.append("Email OK")
        except Exception as ex:
            sent.append(f"Email FAILED {type(ex).__name__}: {str(ex)[:60]}")
    return sent


def append_delivery(path, delivery):
    """Self-report delivery results into the artifact. 'Saved to:' must remain
    the last non-empty line (duplicate-guard contract: FOOTER_MARK lives there)."""
    dl = "Delivery: " + ("; ".join(delivery) if delivery
                         else "none - no delivery secrets set (artifact + commit only)")
    with open(path, "r", encoding="utf-8") as f:
        cur = f.read()
    lines = cur.rstrip("\n").split("\n")
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return False
    lines.insert(idx, dl)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


# ---------------------------------------------------------------- main

def main():
    force = os.environ.get("FORCE_RUN", "").strip().lower() in ("true", "1", "yes")
    now_utc = datetime.now(UTC)
    now_melb = now_utc.astimezone(MELB)

    cfg_path = os.path.join(ROOT, "config", "feeds.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    if not force:
        off = round(now_melb.utcoffset().total_seconds() / 3600)  # 11 (AEDT) or 10 (AEST)
        allowed = {(10 + off) % 24, (11 + off) % 24}
        if now_melb.hour not in allowed:
            log(f"SKIP DST guard: Melbourne hour {now_melb.hour:02d} not in {sorted(allowed)} "
                f"(this is the AEST/AEDT twin cron that self-skips)")
            return 0
        log(f"DST guard OK: Melbourne {now_melb.strftime('%H:%M')}, offset +{off}")

    os.makedirs(EDITIONS_DIR, exist_ok=True)
    fname = fname_of(now_melb)
    path = os.path.join(EDITIONS_DIR, fname)

    if os.path.exists(path) and not force:
        with open(path) as f:
            old = f.read()
        lines = [l for l in old.splitlines() if l.strip()]
        if lines and FOOTER_MARK in lines[-1]:
            log(f"SKIP duplicate guard: {fname} already exists complete. Nothing done.")
            return 0
    if force and os.path.exists(path):
        log("FORCE_RUN: regenerating today's edition (manual dispatch overrides duplicate guard)")

    win_start = now_utc - timedelta(hours=cfg.get("window_hours", 24),
                                    minutes=cfg.get("tolerance_minutes", 30))
    log(f"Producing {fname} | coverage {win_start.astimezone(MELB).strftime('%a %d %b %H:%M')} "
        f"-> {now_melb.strftime('%a %d %b %H:%M')} Melbourne")

    regions_out, feeds_total, feeds_ok, feeds_fail_all = {}, 0, 0, []
    for r in cfg["regions"]:
        log(f"  region: {r['title']}")
        items, n_feeds, fed_ok, fed_fail = fetch_region(r, cfg, now_utc, win_start)
        regions_out[r["id"]] = (items, n_feeds, fed_ok, fed_fail)
        feeds_total += n_feeds
        feeds_ok += fed_ok
        for fail in fed_fail:
            feeds_fail_all.append(fail)
        log(f"  -> {len(items)} items")

    log("  markets:")
    mkt_lines, mkt_ok, mkt_failed = fetch_markets(cfg)

    regions_body, ai_note = None, None
    if os.environ.get("LLM_API_KEY", "").strip():
        log("  AI editorial: calling Gemini...")
        try:
            regions_body, model_used = ai_edition(cfg, regions_out, now_melb)
            ai_note = f"AI editorial: on ({model_used}) - validated against fetched pool"
            log(f"  AI editorial: OK ({model_used})")
        except Exception as ex:
            code = getattr(ex, "code", "")
            body = ""
            try:
                body = ex.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            tag = type(ex).__name__ + (f" HTTP {code}" if code != "" else "")
            ai_note = (f"AI editorial: FAILED this run ({tag}"
                       + (f": {body}" if body else "") + ") - "
                       "deterministic edition served, cadence unaffected")
            log(f"  AI editorial FAILED: {ex} {body}")
    else:
        ai_note = "AI editorial: no LLM_API_KEY configured - deterministic edition"
        log("  AI editorial: skipped (no LLM_API_KEY)")

    text = render(cfg, regions_out, mkt_lines, now_utc, now_melb,
                  feeds_total, feeds_ok, feeds_fail_all, mkt_ok, mkt_failed,
                  regions_body=regions_body, ai_note=ai_note)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log(f"WROTE {path} ({len(text)} chars)")

    delivery = deliver(path, text, now_melb)
    log("DELIVERY: " + ("; ".join(delivery) if delivery else "commit-only (no delivery secrets set)"))
    try:
        append_delivery(path, delivery)
    except Exception as ex:
        log(f"  delivery self-report append failed: {ex}")
    gs = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if gs:
        try:
            with open(gs, "a", encoding="utf-8") as f:
                f.write("\n## Delivery\n\n"
                        + ("; ".join(delivery) if delivery
                           else "none - no delivery secrets set (artifact + commit only)") + "\n")
        except Exception:
            pass
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
