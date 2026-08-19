#!/usr/bin/env python3
"""Pattern Lab — Project Lighthouse.

Builds the CHART-SHAPE DATABASE of every runner we've ever caught:
what the stock's day physically looked like, minute by minute — where the
move started, how it peaked, and how it died. Groups the days into named
archetypes ("pre-market pop then fade", "gap and go", "morning spike"...),
scores each archetype with the same +25%/-10% rule, and measures RUNWAY:
how many minutes passed between the move crossing +20% (our tripwire)
and the top. Runway is the time a scanner has to act.

Inputs:  the Runner Scanner's live data.json  +  the retired ProTicker
         tracker's local data.json (same species of stock, more history).
Outputs: pattern_db.json (the database)  +  a visual catalog HTML with a
         mini-chart of every single day, grouped by archetype.

Observation only. Not investment advice.
"""
import datetime as dt
import html
import json
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
DB = HERE / "pattern_db.json"            # repo copy (bootstrap for the cloud)
SITE = HERE / "site"
LIGHTHOUSE = HERE.parent.parent          # .../lighthouse (exists on the Mac only)
CATALOG_LOCAL = LIGHTHOUSE / "LOOK AT THE DATA" / "5 — Chart pattern catalog.html"
LIVE_DB = "https://willberry21.github.io/auto-screener/pattern_db.json"
SCANNER_DATA = "https://willberry21.github.io/auto-screener/data.json"
PROTICKER_DATA = HERE.parent / "proticker-tracker" / "data.json"
MAX_AGE_DAYS = 55                        # Yahoo's 5-minute history limit

UA = {"User-Agent": "Mozilla/5.0 (Lighthouse pattern lab)"}


def log(m):
    print(f"[{dt.datetime.now(ET):%H:%M:%S}] {m}", flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def bars_with_prev_close(ticker, day_iso):
    """5-min bars (pre-market included) for the day, plus the prior day's
    closing price — every % in the catalog is measured from that close,
    because that's what '+40% today' means."""
    d0 = dt.datetime.strptime(day_iso, "%Y-%m-%d").replace(tzinfo=ET)
    p1 = int((d0 - dt.timedelta(days=6)).timestamp())
    p2 = int((d0 + dt.timedelta(days=1)).timestamp())
    d = fetch_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                   f"?period1={p1}&period2={p2}&interval=5m&includePrePost=true")
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    day, prev_close = [], None
    for t, o, h, lo, c, v in zip(res.get("timestamp") or [], q.get("open", []),
                                 q.get("high", []), q.get("low", []),
                                 q.get("close", []), q.get("volume", [])):
        if c is None:
            continue
        ts = dt.datetime.fromtimestamp(t, ET)
        iso = ts.strftime("%Y-%m-%d")
        if iso < day_iso and ts.time() <= dt.time(16, 0):
            prev_close = c                        # last close before our day
        elif iso == day_iso:
            day.append({"t": ts, "o": o or c, "h": h or c, "l": lo or c,
                        "c": c, "v": v or 0})
    return day, prev_close


def analyze(ticker, day_iso, caught_at_hhmm):
    """One day's shape, as numbers."""
    bars, prev = bars_with_prev_close(ticker, day_iso)
    if not bars or not prev:
        return None
    pc = lambda x: (x - prev) / prev
    mins = lambda b: b["t"].hour * 60 + b["t"].minute

    peak_bar = max(bars, key=lambda b: b["h"])
    peak_pct, peak_min = pc(peak_bar["h"]), mins(peak_bar)
    reg = [b for b in bars if 570 <= mins(b) < 960]          # 9:30-16:00
    pm = [b for b in bars if mins(b) < 570]
    close_pct = pc(reg[-1]["c"]) if reg else pc(bars[-1]["c"])
    gap_pct = pc(reg[0]["o"]) if reg else None
    pm_high_pct = pc(max(b["h"] for b in pm)) if pm else 0.0

    # tripwire: first moment the day's running high crossed +20%
    t20 = None
    for b in bars:
        if pc(b["h"]) >= 0.20:
            t20 = mins(b)
            break
    runway = (peak_min - t20) if (t20 is not None and peak_min >= t20) else None

    # waves: a new high made after giving back >=30% of the run so far
    waves, run_hi, trough, retraced = 1, None, None, False
    for b in bars:
        p = pc(b["h"])
        if run_hi is None or p > run_hi:
            if retraced and run_hi and run_hi > 0.05:
                waves += 1
                retraced = False
            run_hi = max(run_hi or p, p)
            trough = p
        trough = min(trough if trough is not None else p, pc(b["l"]))
        if run_hi and run_hi > 0 and (run_hi - trough) >= 0.30 * run_hi:
            retraced = True

    kept = (close_pct / peak_pct) if peak_pct and peak_pct > 0 else None

    # ---- the archetype label (order matters: most specific first) ----------
    if peak_min < 570:
        label = ("Pre-market pop, then all-day fade" if (kept or 0) < 0.35
                 else "Pre-market pop that held")
    elif (gap_pct or 0) >= 0.15:
        label = ("Gap and go" if (kept or 0) >= 0.6 else "Gap up, then fade")
    elif waves >= 3:
        label = "Multi-wave runner"
    elif peak_min < 660:
        label = ("Morning spike, then collapse" if (kept or 0) < 0.35
                 else "Morning spike that held")
    elif peak_min >= 840:
        label = "Afternoon runner"
    else:
        label = "Midday runner"

    # compact series for the mini-chart: (minute, pct-close) per bar, 4am-4pm
    series = [[mins(b), round(pc(b["c"]), 4)] for b in bars if mins(b) < 960]

    return {"ticker": ticker, "date": day_iso, "label": label,
            "prev_close": prev,
            "pm_high_pct": round(pm_high_pct, 4),
            "gap_pct": round(gap_pct, 4) if gap_pct is not None else None,
            "peak_pct": round(peak_pct, 4), "peak_min": peak_min,
            "close_pct": round(close_pct, 4),
            "kept": round(kept, 3) if kept is not None else None,
            "t20_min": t20, "runway_min": runway, "waves": waves,
            "caught_at": caught_at_hhmm, "series": series}


# ------------------------------------------------------------------ collect
def collect_catches():
    """(date, ticker, caught_hh:mm, rule_pct, source) for everything we have."""
    out = {}
    d = fetch_json(SCANNER_DATA)
    for k, v in d["detections"].items():
        hhmm = v["detected_at"][11:16]
        rule = (v.get("score") or {}).get("rule_pct")
        out[k] = (v["date"], v["ticker"], hhmm, rule, "scanner")
    if PROTICKER_DATA.exists():
        pt = json.loads(PROTICKER_DATA.read_text())
        for k, v in pt.get("signals", {}).items():
            if k in out:
                continue
            rule = ((v.get("score") or {}).get("realistic") or {}).get("pct")
            out[k] = (v["date"], v["ticker"], v.get("alert_time", ""),
                      rule, "proticker")
    return out


# ------------------------------------------------------------------- render
def spark(entry):
    """One mini-chart: same 4:00am-4:00pm time axis on every card so the
    SHAPES are comparable. y is scaled to each day's own peak (peak printed
    on the card). Grey band = pre-market. Dot = the top. Dashed line = the
    moment we caught it."""
    W, Hh, PAD = 220, 64, 4
    x0, x1 = 240, 960                       # minutes: 4:00am .. 4:00pm
    s = entry["series"]
    if len(s) < 3:
        return ""
    ymax = max(entry["peak_pct"], 0.01)
    ymin = min(0, min(p for _, p in s))
    X = lambda m: PAD + (W - 2 * PAD) * (m - x0) / (x1 - x0)
    Y = lambda p: PAD + (Hh - 2 * PAD) * (1 - (p - ymin) / (ymax - ymin))
    pts = " ".join(f"{X(m):.1f},{Y(p):.1f}" for m, p in s)
    pm_w = X(570) - X(x0)
    caught = ""
    if entry["caught_at"]:
        try:
            ch, cm = map(int, entry["caught_at"].split(":"))
            cx = X(ch * 60 + cm)
            caught = (f'<line x1="{cx:.1f}" y1="2" x2="{cx:.1f}" y2="{Hh - 2}" '
                      f'stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="3,3"/>')
        except ValueError:
            pass
    px, py = X(entry["peak_min"]), Y(entry["peak_pct"])
    zero = Y(0)
    return f"""<svg viewBox="0 0 {W} {Hh}" class="sp" role="img"
 aria-label="{entry['ticker']} {entry['date']} intraday shape">
<rect x="{X(x0):.1f}" y="0" width="{pm_w:.1f}" height="{Hh}" fill="var(--pmband)"/>
<line x1="0" y1="{zero:.1f}" x2="{W}" y2="{zero:.1f}" stroke="var(--line)" stroke-width="1"/>
{caught}
<polyline points="{pts}" fill="none" stroke="var(--ink)" stroke-width="2"
 stroke-linejoin="round" stroke-linecap="round"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="var(--ink)"/>
</svg>"""


def render(entries):
    groups = {}
    for e in entries:
        groups.setdefault(e["label"], []).append(e)
    # biggest group first
    order = sorted(groups, key=lambda k: -len(groups[k]))
    total = len(entries)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None

    sections = []
    for label in order:
        g = sorted(groups[label], key=lambda e: -e["peak_pct"])
        scored = [e for e in g if e.get("rule_pct") is not None]
        wins = sum(1 for e in scored if e["rule_pct"] > 0)
        runs = [e["runway_min"] for e in g if e.get("runway_min") is not None]
        stat = ""
        if scored:
            avg = sum(e["rule_pct"] for e in scored) / len(scored)
            cls = "good" if avg > 0 else "bad"
            stat = (f'<span class="chip {cls}">rule: {wins}/{len(scored)} wins · '
                    f'{avg * 100:+.1f}% avg</span>')
        rw = f'<span class="chip">runway: ~{med(runs)} min</span>' if runs else ""
        cards = []
        for e in g:
            sc = e.get("rule_pct")
            pill = ""
            if sc is not None:
                pcls = "win" if sc > 0 else "loss"
                pill = f'<span class="pill {pcls}">{sc * 100:+.0f}%</span>'
            # Everything here used to live in a title="" tooltip — hover-only, so it never
            # showed on a phone and keyboard users never got it at all. It's visible text now.
            cards.append(f"""<div class="card">
<div class="chead"><b>{html.escape(e['ticker'])}</b>
<span class="dt">{e['date'][5:]}</span>{pill}</div>
{spark(e)}
<div class="cfoot">peak +{e['peak_pct'] * 100:.0f}% at {e['peak_min'] // 60}:{e['peak_min'] % 60:02d} · kept {f"{e['kept'] * 100:.0f}%" if e['kept'] is not None else "—"}</div>
<div class="cfoot">closed {e['close_pct'] * 100:+.0f}%</div>
</div>""")
        sections.append(f"""<h2>{html.escape(label)}
<span class="count">{len(g)} of {total}</span> {stat} {rw}</h2>
<div class="grid">{''.join(cards)}</div>""")

    all_run = [e["runway_min"] for e in entries if e.get("runway_min") is not None]
    now = dt.datetime.now(ET).strftime("%B %-d, %Y at %-I:%M %p ET")
    doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart Pattern Catalog — Project Lighthouse</title>
<style>
:root {{ color-scheme: light dark;
  --bg:#0b0e14; --card:#151a24; --line:#232b3a; --txt:#e6edf3; --dim:#8b98a9;
  --accent:#4da3ff; --good:#3fb950; --bad:#f85149; --ink:#e6edf3;
  --pmband:rgba(139,152,169,.12); }}
@media (prefers-color-scheme:light) {{ :root {{
  --bg:#f4f6fa; --card:#fff; --line:#e2e8f0; --txt:#1a2230; --dim:#5b6675;
  --accent:#1f6feb; --good:#1a7f37; --bad:#cf222e; --ink:#1a2230;
  --pmband:rgba(91,102,117,.10); }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--txt);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1200px; margin:0 auto; padding:30px 20px 70px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.02em; }}
.sub {{ color:var(--dim); font-size:13.5px; margin:0 0 6px; max-width:820px; }}
h2 {{ font-size:17px; margin:34px 0 12px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.count {{ color:var(--dim); font-weight:400; font-size:13px; }}
.chip {{ font-size:11.5px; font-weight:600; padding:3px 10px; border-radius:999px;
  background:color-mix(in srgb,var(--dim) 15%,transparent); color:var(--dim); }}
.chip.good {{ background:color-mix(in srgb,var(--good) 16%,transparent); color:var(--good); }}
.chip.bad {{ background:color-mix(in srgb,var(--bad) 16%,transparent); color:var(--bad); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(236px,1fr)); gap:10px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 10px 8px; }}
.chead {{ display:flex; align-items:center; gap:8px; font-size:13px; margin-bottom:4px; }}
.dt {{ color:var(--dim); font-size:11.5px; }}
.pill {{ margin-left:auto; padding:1px 8px; border-radius:999px; font-size:10.5px; font-weight:700; }}
.pill.win {{ background:color-mix(in srgb,var(--good) 20%,transparent); color:var(--good); }}
.pill.loss {{ background:color-mix(in srgb,var(--bad) 20%,transparent); color:var(--bad); }}
.sp {{ width:100%; height:64px; display:block; }}
.cfoot {{ color:var(--dim); font-size:11px; margin-top:3px; }}
.legend {{ color:var(--dim); font-size:12.5px; margin:10px 0 0; }}
.legend b {{ color:var(--txt); }}
.foot {{ margin-top:40px; color:var(--dim); font-size:12px; line-height:1.7; max-width:860px; }}
.back {{ display:inline-block; color:var(--dim); text-decoration:none; font-size:13px;
  font-weight:600; margin-bottom:10px; }}
.back:hover {{ color:var(--accent); }}
</style></head><body><div class="wrap">
<a class="back" href="index.html">&larr; Back to the scanner</a>
<h1>📈 Chart Pattern Catalog</h1>
<p class="sub">Every runner Lighthouse has ever caught, drawn as its actual day — one mini-chart per
stock per day, all on the <b>same clock</b> (4:00 AM on the left edge, 4:00 PM on the right) so the
shapes are honestly comparable. Grouped by the shape of the move. Rebuilt {now} ·
{total} days charted · typical runway across everything: ~{med(all_run)} minutes.</p>
<p class="legend"><b>How to read a card:</b> the grey band on the left is pre-market (before the
9:30 open). The line is the stock's % move vs yesterday's close. The dot is the top of the day.
The blue dashed line is the moment we caught it — dashed line LEFT of the dot means we were early;
RIGHT of the dot means we arrived after the party. "Kept" = how much of the peak gain survived to
the close. The +/-% pill is the honest rule result (+25% target / −10% stop from our catch).</p>
{''.join(sections)}
<div class="foot">
<b>What "runway" means.</b> The minutes between the move first crossing +20% (the scanner's tripwire)
and the top of the day. That's the maximum time ANY scanner has to catch the move while there's still
something left — the number that decides how fast our detection needs to be.<br>
<b>Sources.</b> The Runner Scanner's own catches plus the retired Pro Ticker tracker's historical
signals (same species of stock). Prices from Yahoo Finance 5-minute data, pre-market included; every
% is measured from the previous day's close. Yahoo keeps ~60 days of minute data, so this catalog is
rebuilt while the history is still available — the database (pattern_db.json) keeps the numbers forever.<br>
<b>Observation only — not investment advice.</b>
</div>
</div></body></html>"""
    SITE.mkdir(exist_ok=True)
    (SITE / "patterns.html").write_text(doc)
    if CATALOG_LOCAL.parent.is_dir():                 # Mac convenience copy
        CATALOG_LOCAL.write_text(doc)
    log(f"Catalog written: site/patterns.html ({total} charts in {len(order)} groups)")


def main():
    import sys
    force = "--force" in sys.argv
    now = dt.datetime.now(ET)
    today = dt.date.today()

    # existing database: the live site's copy first, the repo copy as bootstrap
    entries = []
    try:
        entries = fetch_json(LIVE_DB)["days"]
        log(f"Loaded {len(entries)} days from the live database.")
    except Exception:
        if DB.exists():
            entries = json.loads(DB.read_text())["days"]
            log(f"Live db unavailable — loaded {len(entries)} days from the repo copy.")

    catches = collect_catches()
    # keep scores fresh on already-charted days (scoring can arrive later)
    for e in entries:
        c = catches.get(f"{e['date']}|{e['ticker']}")
        if c and c[3] is not None:
            e["rule_pct"] = c[3]

    have = {(e["date"], e["ticker"]) for e in entries}
    new = [(k, v) for k, v in sorted(catches.items())
           if (v[0], v[1]) not in have
           and (today - dt.date.fromisoformat(v[0])).days <= MAX_AGE_DAYS
           and v[0] != f"{today:%Y-%m-%d}"]           # chart a day once it's over

    # heavy fetching only in the evening (or --force): daytime runs just re-render
    if new and (force or now.time() >= dt.time(16, 15) or now.isoweekday() > 5):
        skipped = 0
        for k, (date, ticker, hhmm, rule, source) in new:
            try:
                e = analyze(ticker, date, hhmm)
            except Exception:
                e = None
            if e:
                e["rule_pct"] = rule
                e["source"] = source
                entries.append(e)
            else:
                skipped += 1
            time.sleep(0.35)
        log(f"Charted {len(new) - skipped} new day(s) ({skipped} had no data).")
    elif new:
        log(f"{len(new)} day(s) waiting to be charted this evening.")

    payload = json.dumps({"built": dt.datetime.now(ET).isoformat(timespec='seconds'),
                          "days": entries}, indent=1)
    SITE.mkdir(exist_ok=True)
    (SITE / "pattern_db.json").write_text(payload)
    try:
        DB.write_text(payload)                        # repo copy (bootstrap)
    except OSError:
        pass
    log(f"Database saved: {len(entries)} days.")
    render(entries)
    from collections import Counter
    for label, cnt in Counter(e["label"] for e in entries).most_common():
        grp = [e for e in entries if e["label"] == label]
        sc = [e["rule_pct"] for e in grp if e.get("rule_pct") is not None]
        runs = [e["runway_min"] for e in grp if e.get("runway_min") is not None]
        msg = f"{label}: {cnt}"
        if sc:
            msg += f" | rule avg {100 * sum(sc) / len(sc):+.1f}% ({sum(1 for x in sc if x > 0)}/{len(sc)} wins)"
        if runs:
            msg += f" | runway ~{sorted(runs)[len(runs) // 2]}m"
        log(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
