#!/usr/bin/env python3
"""
Ticket-drop tracker for Sean's personal agent.

Once a day it pulls US music + sports on-sales and presales landing in the next
N days from the Ticketmaster Discovery API, scores each one 0-100 for resale
potential, and surfaces only what clears the threshold. New high scorers go out
as ntfy push notifications; everything surfaced is written to a mobile-first
HTML page, a JSON file, and a markdown block for the morning brief.

Standard library only - no pip install, so launchd can run it with system python3.

Usage:
    python3 ticketdrops.py              # normal daily run
    python3 ticketdrops.py --dry-run    # pull + score + write files, send nothing
    python3 ticketdrops.py --test-ping  # send one test notification and exit
    python3 ticketdrops.py --no-state   # ignore state.json, treat everything as new
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
LOGS = HERE / "logs"
STATE_PATH = HERE / "state.json"
PAGE_NAME = "tickets.html"

TM_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
TM_ATTRACTIONS = "https://app.ticketmaster.com/discovery/v2/attractions.json"
NTFY_BASE = "https://ntfy.sh"

# Ticketmaster's own segment ids. classificationName= is fuzzier and pulls in
# adjacent segments, so we pin the ids.
SEGMENTS = {"music": "KZFzniwnSyZfZ7v7nJ", "sports": "KZFzniwnSyZfZ7v7nE"}

USER_AGENT = "sean-ticketdrops/1.0 (personal agent)"

log = logging.getLogger("ticketdrops")


def _ssl_context() -> ssl.SSLContext:
    """python.org builds on macOS ship without a populated CA store, so a plain
    urlopen fails CERTIFICATE_VERIFY_FAILED. Use the system store when it is
    really there, otherwise fall back to certifi. CI runners hit the first path.
    """
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    has_store = bool(
        (paths.openssl_cafile and os.path.exists(paths.openssl_cafile))
        or (paths.openssl_capath and os.path.isdir(paths.openssl_capath))
    )
    if not has_store:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001
            log.warning("No CA store and no certifi - TLS verification will fail. "
                        "Run '/Applications/Python 3.14/Install Certificates.command'.")
    return ctx


SSL_CTX = _ssl_context()


# ----------------------------------------------------------------------------
# config / env
# ----------------------------------------------------------------------------

def load_env(path: Path) -> dict:
    """Minimal .env reader. KEY=VALUE, # comments, optional surrounding quotes."""
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        env[key.strip()] = val
    return env


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logfile = LOGS / f"run-{datetime.now().strftime('%Y-%m-%d')}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------

def get_json(url: str, params: dict, tries: int = 4) -> dict:
    """GET with backoff. Ticketmaster allows 5 req/sec; we stay well under."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    full = f"{url}?{qs}"
    safe = full.replace(params.get("apikey", "\0"), "***") if params.get("apikey") else full

    for attempt in range(1, tries + 1):
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                wait = min(30, 2 ** attempt)
                log.warning("429 rate limited, sleeping %ss", wait)
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                raise RuntimeError(
                    f"Ticketmaster rejected the API key (HTTP {e.code}). "
                    f"Check TICKETMASTER_API_KEY in .env. Response: {body}"
                ) from e
            if 500 <= e.code < 600 and attempt < tries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code} from {safe}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Network error calling {safe}: {e}") from e
    raise RuntimeError(f"Gave up on {safe}")


# ----------------------------------------------------------------------------
# ticketmaster
# ----------------------------------------------------------------------------

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_onsales(api_key: str, segment: str, days: int, page_size: int,
                  max_pages: int, country: str) -> list:
    """Events whose PUBLIC on-sale starts inside the next `days` days."""
    now = datetime.now(timezone.utc)
    # NB: do NOT add sort=onSaleStartDate,asc here. Ticketmaster stamps events
    # with no public on-sale as 1900-01-01, and that sort puts every one of them
    # first - the whole first page comes back as sentinels. Measured 2026-10-01:
    # onsaleOnAfterStartDate + no sort -> 96/100 in-window; with that sort -> 0/100.
    params = {
        "apikey": api_key,
        "countryCode": country,
        "segmentId": SEGMENTS[segment],
        "onsaleOnAfterStartDate": now.strftime("%Y-%m-%d"),
        "size": page_size,
    }
    events, page = [], 0
    while page < max_pages:
        params["page"] = page
        data = get_json(TM_EVENTS, params)
        embedded = data.get("_embedded", {}).get("events", [])
        events.extend(embedded)
        info = data.get("page", {})
        total_pages = info.get("totalPages", 0)
        log.debug("%s page %d/%s -> %d events", segment, page + 1, total_pages, len(embedded))
        page += 1
        # Discovery caps paging at 1000 results deep.
        if page >= total_pages or page * page_size >= 1000 or not embedded:
            break
        time.sleep(0.25)
    kept = [e for e in events if onsale_in_window(e, now, now + timedelta(days=days))]
    log.info("Ticketmaster %s: %d returned, %d with a real on-sale in the next %dd",
             segment, len(events), len(kept), days)
    return kept


def onsale_in_window(ev: dict, lo: datetime, hi: datetime) -> bool:
    """True only for a genuine public on-sale inside [lo, hi].

    Ticketmaster uses 1900-01-01 to mean "no on-sale date set", and those slip
    past the server-side filter, so this is the gate that actually counts.
    """
    s = ((ev.get("sales") or {}).get("public") or {}).get("startDateTime")
    dt = parse_dt(s)
    return bool(dt and dt.year > 2000 and lo <= dt <= hi)


def fetch_attraction_events(api_key: str, name: str, page_size: int) -> list:
    """Watchlist path: find the attraction, then everything it has on sale.

    Deliberately NOT limited to the 14-day on-sale window - the point of the
    watchlist is to catch these artists whenever they surface.
    """
    data = get_json(TM_ATTRACTIONS, {"apikey": api_key, "keyword": name, "size": 5})
    attractions = data.get("_embedded", {}).get("attractions", [])
    if not attractions:
        log.info("Watchlist: no Ticketmaster attraction found for %r", name)
        return []
    att = attractions[0]
    log.info("Watchlist: %r -> %r (id %s)", name, att.get("name"), att.get("id"))
    time.sleep(0.25)
    data = get_json(TM_EVENTS, {
        "apikey": api_key, "attractionId": att["id"],
        "size": page_size, "sort": "date,asc", "countryCode": "US",
    })
    return data.get("_embedded", {}).get("events", [])


# ----------------------------------------------------------------------------
# eventbrite (watchlist only, optional)
# ----------------------------------------------------------------------------

def fetch_eventbrite(token: str, name: str) -> list:
    """Eventbrite retired its public event search in 2019 (/v3/events/search/
    returns 404/410 for everyone now). We still try when a token is present so
    the code lights up if they ever restore it, but we never treat the failure
    as an error - we log it once and carry on.
    """
    url = "https://www.eventbriteapi.com/v3/events/search/"
    qs = urllib.parse.urlencode({"q": name, "sort_by": "date"})
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("events", [])
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            log.info("Eventbrite public search is retired (HTTP %d) - skipping %r", e.code, name)
        else:
            log.warning("Eventbrite error %d for %r", e.code, name)
        return []
    except Exception as e:  # noqa: BLE001 - never let Eventbrite break the run
        log.warning("Eventbrite call failed for %r: %s", name, e)
        return []


# ----------------------------------------------------------------------------
# normalise + score
# ----------------------------------------------------------------------------

def parse_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_VENUES = None


def venue_capacity(name: str):
    """(capacity, source). Ticketmaster returns no capacity for any venue -
    verified against MSG, SoFi, Sphere - so it comes from venues.json: an exact
    table first, then a coarse keyword guess from the room's name."""
    global _VENUES
    if _VENUES is None:
        _VENUES = load_json(HERE / "venues.json", {"exact": {}, "keywords": {}})
    if not name:
        return None, "unknown"
    low = name.lower()
    for known, cap in _VENUES.get("exact", {}).items():
        if known in low:
            return int(cap), "exact"
    for word, cap in sorted(_VENUES.get("keywords", {}).items(), key=lambda kv: -len(kv[0])):
        if word in low:
            return int(cap), "venue type"
    return None, "unknown"


def normalise(ev: dict) -> dict:
    emb = ev.get("_embedded", {})
    venues = emb.get("venues") or [{}]
    v = venues[0]
    attractions = emb.get("attractions") or []

    cls = (ev.get("classifications") or [{}])[0]
    genre = (cls.get("genre") or {}).get("name") or ""
    segment = (cls.get("segment") or {}).get("name") or ""

    sales = ev.get("sales") or {}
    public = sales.get("public") or {}
    presales = sales.get("presales") or []

    prices = ev.get("priceRanges") or []
    pmin = min((p.get("min") for p in prices if p.get("min") is not None), default=None)
    pmax = max((p.get("max") for p in prices if p.get("max") is not None), default=None)

    cap = v.get("capacity")
    try:
        cap = int(cap) if cap else None
    except (TypeError, ValueError):
        cap = None
    cap_source = "ticketmaster" if cap else None
    if cap is None:
        cap, cap_source = venue_capacity(v.get("name") or "")

    upcoming = 0
    for a in attractions:
        tot = ((a.get("upcomingEvents") or {}).get("_total")) or 0
        try:
            upcoming = max(upcoming, int(tot))
        except (TypeError, ValueError):
            pass

    return {
        "id": ev.get("id"),
        "name": ev.get("name") or "(untitled)",
        "url": ev.get("url") or "",
        "segment": segment,
        "genre": genre,
        "artists": [a.get("name") for a in attractions if a.get("name")],
        "artist_upcoming": upcoming,
        "venue": v.get("name") or "",
        "city": ((v.get("city") or {}).get("name")) or "",
        "state": ((v.get("state") or {}).get("stateCode")) or "",
        "capacity": cap,
        "capacity_source": cap_source,
        "event_date": ((ev.get("dates") or {}).get("start") or {}).get("dateTime")
                      or ((ev.get("dates") or {}).get("start") or {}).get("localDate"),
        "onsale_start": public.get("startDateTime"),
        "onsale_end": public.get("endDateTime"),
        "presales": [
            {"name": p.get("name"), "start": p.get("startDateTime"), "end": p.get("endDateTime")}
            for p in presales
        ],
        "price_min": pmin,
        "price_max": pmax,
    }


def score_event(e: dict, cfg: dict, watch_names: set, seatgeek: dict | None) -> dict:
    """0-100 resale-potential score. Every component is reported so a score can
    be argued with rather than trusted blindly."""
    w = cfg["weights"]
    reasons, parts = [], {}

    # --- demand: how much work the act is doing + whether it's a known flipper
    u = e["artist_upcoming"]
    demand = min(w["demand_max"] - w["watchlist_bonus"], 14 * math.log10(u + 1)) if u else 4.0
    on_watch = any(a.lower() in watch_names for a in e["artists"])
    if on_watch:
        demand += w["watchlist_bonus"]
        reasons.append("on your watchlist")
    if u >= 40:
        reasons.append(f"{u} upcoming dates (heavy routing)")
    parts["demand"] = round(min(demand, w["demand_max"]), 1)

    # --- scarcity: small room + real demand is where markup lives
    cap = e["capacity"]
    if cap:
        lo, hi = math.log10(500), math.log10(80000)
        frac = (math.log10(max(cap, 500)) - lo) / (hi - lo)
        scarcity = w["scarcity_max"] * max(0.0, 1.0 - frac)
        if cap <= 3000:
            reasons.append(f"small room ({cap:,} cap)")
    else:
        scarcity = 0.0  # no capacity at all -> no points. Absence is not evidence.
    parts["scarcity"] = round(scarcity, 1)
    if cap and e.get("capacity_source") == "venue type":
        reasons.append(f"~{cap:,} cap (from venue type)")

    # --- markup: the honest weak spot without a resale feed
    mult_lo, mult_hi, conf = estimate_resale_multiple(e, cfg, seatgeek, on_watch)
    mid = (mult_lo + mult_hi) / 2
    markup = max(0.0, min(w["markup_max"], (mid - 1.0) / 1.2 * w["markup_max"]))
    parts["markup"] = round(markup, 1)
    if mid >= 1.6:
        reasons.append(f"est. {mult_lo:.1f}-{mult_hi:.1f}x face")

    # --- timing: presales and imminence
    timing = 0.0
    if e["presales"]:
        timing += 4
        reasons.append(f"{len(e['presales'])} presale(s)")
    start = parse_dt(e["onsale_start"])
    if start:
        days = (start - datetime.now(timezone.utc)).days
        if days <= 3:
            timing += 6
        elif days <= 7:
            timing += 3
    parts["timing"] = round(min(timing, w["timing_max"]), 1)

    total = sum(parts.values())

    confidence = conf
    if not cap:
        confidence = "low" if confidence != "low" else "low"

    return {
        **e,
        "score": int(round(max(0, min(100, total)))),
        "score_parts": parts,
        "confidence": confidence,
        "on_watchlist": on_watch,
        "resale_multiple": [round(mult_lo, 2), round(mult_hi, 2)],
        "est_resale_min": round(e["price_min"] * mult_lo) if e["price_min"] else None,
        "est_resale_max": round(e["price_max"] * mult_hi) if e["price_max"] else None,
        "reasons": reasons,
    }


def estimate_resale_multiple(e: dict, cfg: dict, seatgeek: dict | None, on_watch: bool):
    """Returns (low, high, confidence).

    With a SeatGeek client id this would be measured against live listings. It is
    off, so this is a genre-table estimate nudged by demand signals - useful for
    ranking, NOT a number to underwrite a purchase with. Confidence says so.
    """
    if seatgeek:
        lo = seatgeek.get("resale_lo")
        hi = seatgeek.get("resale_hi")
        if lo and hi:
            return lo, hi, "high"

    table = cfg["genre_multiples"]
    key = e["genre"] if e["genre"] in table else ("_sports" if e["segment"] == "Sports" else "_default")
    lo, hi = table[key]

    if e["artist_upcoming"] >= 40:
        lo, hi = lo + 0.1, hi + 0.2
    if e["capacity"] and e["capacity"] <= 3000:
        lo, hi = lo + 0.15, hi + 0.3
    if on_watch:
        lo, hi = lo + 0.1, hi + 0.25

    return lo, hi, "low"


# ----------------------------------------------------------------------------
# ntfy
# ----------------------------------------------------------------------------

def ntfy_post(topic: str, title: str, body: str, tags: str = "", click: str = "",
              priority: str = "default", dry: bool = False) -> bool:
    if dry:
        log.info("[dry-run] ntfy %s | %s | %s", topic, title, body.replace("\n", " / ")[:120])
        return True
    headers = {
        "User-Agent": USER_AGENT,
        "Title": title.encode("ascii", "replace").decode(),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            return resp.status < 300
    except Exception as e:  # noqa: BLE001
        log.error("ntfy post failed: %s", e)
        return False


# ----------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------

def fmt_money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "-"


def face_str(e):
    """Face value. Ticketmaster usually withholds priceRanges until the on-sale
    actually opens, so 'TBA' is the common case, not an error."""
    if e["price_min"] and e["price_max"]:
        return f"{fmt_money(e['price_min'])}-{fmt_money(e['price_max'])}"
    if e["price_min"]:
        return f"from {fmt_money(e['price_min'])}"
    return "TBA"


def resale_str(e):
    if e["est_resale_min"] and e["est_resale_max"]:
        return f"{fmt_money(e['est_resale_min'])}-{fmt_money(e['est_resale_max'])}"
    lo, hi = e["resale_multiple"]
    return f"{lo:g}-{hi:g}x face"


_TZ = None


def set_timezone(name: str, fallback_offset: int) -> None:
    """Resolve the display timezone once. A real tz keeps the clock honest
    across the DST flip; the fixed offset is only a last resort."""
    global _TZ
    if ZoneInfo:
        try:
            _TZ = ZoneInfo(name)
            return
        except Exception:  # noqa: BLE001
            log.warning("Unknown timezone %r, falling back to UTC%+d", name, fallback_offset)
    _TZ = timezone(timedelta(hours=fallback_offset))


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(_TZ or timezone.utc)


def local_str(iso, _tz=None):
    dt = parse_dt(iso)
    if not dt:
        return "TBA"
    return to_local(dt).strftime("%a %-d %b, %-I:%M %p")


def when_str(iso):
    dt = parse_dt(iso)
    if not dt:
        return ""
    d = (dt - datetime.now(timezone.utc)).days
    if d < 0:
        return "passed"
    return "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d}d")


# ----------------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------------

def write_json(events: list, meta: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tickets.json").write_text(
        json.dumps({"meta": meta, "events": events}, indent=2), encoding="utf-8"
    )


def write_brief(events: list, meta: dict, tz: int) -> str:
    lines = [f"{len(events)} ticket drops scoring {meta['threshold']}+ in the next {meta['days']} days."]
    if not events:
        lines = [f"No ticket drops cleared {meta['threshold']} in the next {meta['days']} days."]
    for e in events[:12]:
        who = ", ".join(e["artists"][:2]) or e["name"]
        lines.append("")
        lines.append(f"  {e['score']} - {who}")
        lines.append(f"  {e['venue']}, {e['city']} {e['state']}".rstrip())
        lines.append(f"  On sale {local_str(e['onsale_start'], tz)} ({when_str(e['onsale_start'])})")
        lines.append(f"  Face {face_str(e)} - est. resale {resale_str(e)} ({e['confidence']} conf)")
    text = "\n".join(lines)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tickets-brief.md").write_text(text, encoding="utf-8")
    return text


PAGE_CSS = """
*{box-sizing:border-box}
:root{--bg:#F5F6F8;--card:#FFF;--ink:#14181F;--ink2:#515A68;--ink3:#8A94A2;
--rule:#E2E6EB;--hot:#B3341F;--warm:#9A6410;--cool:#2F6B8F;--chipbg:#EEF1F4}
@media(prefers-color-scheme:dark){:root{--bg:#0F1216;--card:#181C22;--ink:#E8ECF1;
--ink2:#A2ACB9;--ink3:#79838F;--rule:#272E37;--hot:#E4765E;--warm:#D2A054;
--cool:#6FA8CC;--chipbg:#20262E}}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:720px;margin:0 auto;padding:18px 16px 56px}
header{margin-bottom:18px}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--ink3);font-size:13px}
.ev{background:var(--card);border:1px solid var(--rule);border-radius:12px;
padding:14px;margin-bottom:12px;display:grid;grid-template-columns:46px 1fr;gap:12px}
.sc{font-weight:700;font-size:19px;text-align:center;padding-top:1px;font-variant-numeric:tabular-nums}
.s90{color:var(--hot)}.s75{color:var(--warm)}.s60{color:var(--cool)}
.ttl{font-weight:600;font-size:16px;line-height:1.25;margin:0 0 2px}
.ttl a{color:inherit;text-decoration:none}
.vn{color:var(--ink2);font-size:13.5px;margin-bottom:8px}
.when{font-size:14px;font-weight:600;margin-bottom:2px}
.ago{color:var(--ink3);font-weight:400}
.money{font-size:13.5px;color:var(--ink2);font-variant-numeric:tabular-nums}
.chips{margin-top:9px;display:flex;flex-wrap:wrap;gap:5px}
.chip{background:var(--chipbg);color:var(--ink2);font-size:11px;padding:3px 7px;
border-radius:5px;white-space:nowrap}
.chip.w{background:var(--hot);color:#fff}
footer{margin-top:28px;color:var(--ink3);font-size:12px;line-height:1.6}
.empty{background:var(--card);border:1px dashed var(--rule);border-radius:12px;
padding:28px 16px;text-align:center;color:var(--ink3)}
"""


def write_page(events: list, meta: dict, tz: int) -> None:
    rows = []
    for e in events:
        cls = "s90" if e["score"] >= 90 else ("s75" if e["score"] >= 75 else "s60")
        who = html.escape(", ".join(e["artists"][:2]) or e["name"])
        link = html.escape(e["url"] or "")
        title = f'<a href="{link}" target="_blank" rel="noopener">{who}</a>' if link else who
        where = html.escape(f"{e['venue']}, {e['city']} {e['state']}".strip().strip(","))
        chips = []
        if e["on_watchlist"]:
            chips.append('<span class="chip w">watchlist</span>')
        # the watchlist chip above already says this - don't say it twice
        for r in [r for r in e["reasons"] if r != "on your watchlist"][:3]:
            chips.append(f'<span class="chip">{html.escape(r)}</span>')
        chips.append(f'<span class="chip">{html.escape(e["confidence"])} conf</span>')
        rows.append(f"""
      <article class="ev">
        <div class="sc {cls}">{e['score']}</div>
        <div>
          <p class="ttl">{title}</p>
          <div class="vn">{where}</div>
          <div class="when">{html.escape(local_str(e['onsale_start'], tz))}
            <span class="ago">· {html.escape(when_str(e['onsale_start']))}</span></div>
          <div class="money">Face {html.escape(face_str(e))} &nbsp;·&nbsp;
            est. resale {html.escape(resale_str(e))}</div>
          <div class="chips">{''.join(chips)}</div>
        </div>
      </article>""")

    body = "\n".join(rows) if rows else (
        f'<div class="empty">Nothing scored {meta["threshold"]}+ today.<br>'
        f'Checked {meta["scanned"]} on-sales across the next {meta["days"]} days.</div>'
    )

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Ticket Drops</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Drops">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0F1216">
<link rel="apple-touch-icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 180 180\'%3E%3Crect width=\'180\' height=\'180\' fill=\'%2314181F\'/%3E%3Ctext x=\'90\' y=\'124\' font-size=\'104\' text-anchor=\'middle\' fill=\'%23E4765E\' font-family=\'system-ui\'%3E&#127915;%3C/text%3E%3C/svg%3E">
<style>{PAGE_CSS}</style>
</head><body><div class="wrap">
  <header>
    <h1>Ticket drops</h1>
    <div class="sub">{meta['generated_local']} · {len(events)} scoring {meta['threshold']}+
      of {meta['scanned']} on-sales · sorted by on-sale time</div>
  </header>
{body}
  <footer>
    Scores are research, not guarantees. Resale estimates come from a genre table,
    not live listings &mdash; add a SeatGeek client id to <code>.env</code> to score
    against real resale prices. Ticket-resale rules vary by state, venue and tour;
    check transferability before buying.
  </footer>
</div></body></html>
"""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / PAGE_NAME).write_text(page, encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Daily ticket-drop tracker")
    ap.add_argument("--dry-run", action="store_true", help="write files, send no notifications")
    ap.add_argument("--test-ping", action="store_true", help="send one test notification and exit")
    ap.add_argument("--no-state", action="store_true", help="treat every surfaced event as new")
    ap.add_argument("--out-dir", default="out",
                    help="where to write the page and data (default: out)")
    ap.add_argument("--page-name", default="tickets.html",
                    help="filename for the page (use index.html for GitHub Pages)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    global OUT, PAGE_NAME
    out_arg = Path(args.out_dir)
    OUT = out_arg if out_arg.is_absolute() else HERE / out_arg
    PAGE_NAME = args.page_name

    setup_logging(args.verbose)
    # In CI there is no .env - the secrets arrive as real environment variables.
    env = {**load_env(HERE / ".env"), **{k: v for k, v in os.environ.items() if v}}
    cfg = load_json(HERE / "config.json", {})
    if not cfg:
        log.error("config.json missing or unreadable")
        return 1

    topic = env.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.error("NTFY_TOPIC is not set in .env")
        return 1

    if args.test_ping:
        ok = ntfy_post(
            topic,
            "Ticket drops: test",
            "Your ticket-drop tracker is wired up correctly.\n"
            "Real alerts carry artist, venue, on-sale time and est. face vs resale.",
            tags="ticket,white_check_mark", priority="default",
        )
        log.info("Test notification %s (topic %s)", "sent" if ok else "FAILED", topic)
        return 0 if ok else 1

    api_key = env.get("TICKETMASTER_API_KEY", "").strip()
    if not api_key:
        log.error("TICKETMASTER_API_KEY is not set in .env - nothing to pull.")
        return 1

    set_timezone(cfg.get("timezone", "America/Denver"),
                 int(cfg.get("fallback_utc_offset_hours", -7)))
    tz = None
    days = int(cfg.get("lookahead_days", 14))
    threshold = int(cfg.get("score_threshold", 60))
    page_size = int(cfg.get("page_size", 200))
    max_pages = int(cfg.get("max_pages", 5))
    country = cfg.get("country", "US")

    watchlist = load_json(HERE / "watchlist.json", {"artists": []})
    watch_names = set()
    for a in watchlist.get("artists", []):
        watch_names.add(a["name"].lower())
        for alias in a.get("aliases", []):
            watch_names.add(alias.lower())

    # --- pull
    raw = []
    for seg in cfg.get("segments", ["music", "sports"]):
        try:
            raw += fetch_onsales(api_key, seg, days, page_size, max_pages, country)
        except RuntimeError as e:
            log.error("%s pull failed: %s", seg, e)
            if "API key" in str(e):
                return 1

    # --- watchlist pass, separate from the window
    eb_token = env.get("EVENTBRITE_TOKEN", "").strip()
    for a in watchlist.get("artists", []):
        names = [a["name"]] + a.get("aliases", [])
        for n in names:
            try:
                got = fetch_attraction_events(api_key, n, 50)
            except RuntimeError as e:
                log.warning("watchlist pull failed for %r: %s", n, e)
                got = []
            if got:
                raw += got
                break
            time.sleep(0.25)
        if eb_token:
            fetch_eventbrite(eb_token, a["name"])

    # --- dedupe, normalise, score
    seen_ids, norm = set(), []
    for ev in raw:
        if ev.get("id") in seen_ids:
            continue
        seen_ids.add(ev.get("id"))
        norm.append(normalise(ev))

    scored = [score_event(e, cfg, watch_names, None) for e in norm]
    surfaced = [e for e in scored if e["score"] >= threshold]
    surfaced.sort(key=lambda e: (e["onsale_start"] or "9999", -e["score"]))
    log.info("Scanned %d events, %d scored %d+", len(scored), len(surfaced), threshold)

    # --- what's actually new
    state = {} if args.no_state else load_json(STATE_PATH, {})
    known = state.get("notified", {})
    fresh = [e for e in surfaced if e["id"] not in known]
    log.info("%d of those are new since the last run", len(fresh))

    # --- outputs
    now_local = to_local(datetime.now(timezone.utc)).strftime("%a %-d %b, %-I:%M %p")
    meta = {
        "generated_utc": iso_z(datetime.now(timezone.utc)),
        "generated_local": now_local,
        "scanned": len(scored), "surfaced": len(surfaced), "new": len(fresh),
        "threshold": threshold, "days": days,
        "seatgeek": bool(env.get("SEATGEEK_CLIENT_ID", "").strip()),
    }
    write_json(surfaced, meta)
    write_page(surfaced, meta, tz)
    brief = write_brief(surfaced, meta, tz)
    log.info("Wrote %s/{tickets.json,%s,tickets-brief.md}", OUT, PAGE_NAME)

    # --- notify: push is a tighter filter than the page. One push per ARTIST,
    # best-scoring date, because five pushes for the same act on one tour is noise.
    notify_at = int(cfg.get("notify_threshold", cfg.get("score_threshold", 60)))
    by_artist, extra_dates = {}, {}
    for e in sorted(fresh, key=lambda x: -x["score"]):
        if e["score"] < notify_at:
            continue
        key = (e["artists"][0] if e["artists"] else e["name"]).lower()
        if key in by_artist:
            extra_dates[key] = extra_dates.get(key, 0) + 1
        else:
            by_artist[key] = e
    to_send = sorted(by_artist.values(), key=lambda e: (e["onsale_start"] or "9999", -e["score"]))
    log.info("%d new >= %d, %d after collapsing to one per artist",
             sum(1 for e in fresh if e["score"] >= notify_at), notify_at, len(to_send))

    cap = int(cfg.get("max_notifications_per_run", 6))
    for e in to_send[:cap]:
        who = ", ".join(e["artists"][:2]) or e["name"]
        title = f"[{e['score']}] {who}"
        body = (
            f"{e['venue']}, {e['city']} {e['state']}\n"
            f"On sale {local_str(e['onsale_start'], tz)} ({when_str(e['onsale_start'])})\n"
            f"Face {face_str(e)} -> est. resale {resale_str(e)} ({e['confidence']} conf)"
        )
        n_extra = extra_dates.get((e["artists"][0] if e["artists"] else e["name"]).lower(), 0)
        if n_extra:
            body += f"\n+{n_extra} more date(s) for this act"
        tags = "ticket,fire" if e["score"] >= 75 else "ticket"
        ntfy_post(topic, title, body, tags=tags, click=e["url"],
                  priority="high" if e["score"] >= 75 else "default", dry=args.dry_run)
        time.sleep(0.3)

    if len(to_send) > cap:
        ntfy_post(topic, f"+{len(to_send) - cap} more acts",
                  f"{len(to_send)} acts scored {notify_at}+ today, {len(surfaced)} events cleared "
                  f"{threshold}+. Full list on the tickets page.",
                  tags="ticket", dry=args.dry_run)

    # --- digest for the morning brief to read back off the topic
    if not args.dry_run:
        ntfy_post(f"{topic}-brief", f"Tickets digest {datetime.now().strftime('%Y-%m-%d')}",
                  brief, tags="memo")

    # --- persist
    if not args.dry_run:
        known.update({e["id"]: {"score": e["score"], "seen": meta["generated_utc"]} for e in surfaced})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        known = {k: v for k, v in known.items() if v.get("seen", "") >= cutoff}
        STATE_PATH.write_text(json.dumps({"notified": known}, indent=2), encoding="utf-8")

    log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
