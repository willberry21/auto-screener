#!/usr/bin/env python3
"""Runner Scanner — Project Lighthouse, Phase 2 ("our own brain").

Finds low-float stocks making explosive moves, the moment they move —
the same kind of stock Pro Ticker used to "alert" 73% too late.

How it works, in plain English:
  1. Pull the full list of every stock on NASDAQ + NYSE (free, ~8,000 names).
  2. Ask Yahoo for a live quote on ALL of them, in big batches.
  3. Keep the ones that are cheap, have very few shares (a "low float" —
     so little supply that buying pressure rockets the price), and are UP BIG
     right now (pre-market counts — that's where these moves start).
  4. For each catch, look up the company in SEC EDGAR (the government's
     public filings database) for dilution red flags — paperwork that lets
     the company sell NEW shares into the spike.
  5. Log the catch with a timestamp. After the close, score every catch with
     the SAME honest referee we used on Pro Ticker: entry at the first price
     after detection, sell at +25% or cut at -10%, plus held-to-close and
     best-case numbers. No cherry-picking — every catch gets scored, forever.

This is OBSERVATION ONLY. It never trades. It builds a track record first,
exactly like Lighthouse's ground rules demand. Runs on GitHub Actions; keeps
its memory in data.json on the published GitHub Pages site (stateless server,
state lives on the page).
"""
import datetime as dt
import html
import json
import re
import time
import urllib.request
import http.cookiejar
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
LIVE_DATA_URL = "https://willberry21.github.io/auto-screener/data.json"

# ---- the knobs (what counts as a "runner") ---------------------------------
PRICE_MIN, PRICE_MAX = 0.30, 25.0   # the pump zone: cheap stocks
MAX_SHARES = 60e6                    # low float: 60M shares or fewer
ULTRA_LOW = 20e6                     # extra ⚠️ under 20M
MIN_MOVE = 0.20                      # +20% on the day (or pre-market) to count
MIN_DOLLAR_VOL = 1_000_000           # regular hours: at least $1M traded (skip ghosts)
TAKE_PROFIT, STOP_LOSS = 0.25, 0.10  # same honest exit rule we tested on Pro Ticker
SCORE_MAX_AGE_DAYS = 55              # Yahoo only keeps ~60 days of 5-min prices
EDGAR_MAX_PER_RUN = 10

UA = {"User-Agent": "Mozilla/5.0 (Lighthouse research scanner)"}
SEC_UA = {"User-Agent": "Lighthouse personal research scanner will@nanafy.ai"}
DILUTION_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B")

# names that aren't ordinary company shares (funds, warrants, units, notes...)
_SKIP_NAME = re.compile(r"warrant|right|unit|preferred|notes due|% |ETF|Fund|Trust",
                        re.IGNORECASE)


def log(msg):
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S ET}] {msg}", flush=True)


def fetch(url, headers=UA, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))


# ------------------------------------------------------------- the universe
def stock_universe():
    """Every ordinary common stock on NASDAQ + NYSE/AMEX, from the exchanges'
    own free symbol directories."""
    syms = []
    txt = fetch("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt").decode()
    for line in txt.strip().split("\n")[1:]:
        p = line.split("|")
        if len(p) < 7 or p[3] == "Y" or p[6] == "Y":     # test issue / ETF
            continue
        if _SKIP_NAME.search(p[1]) or not p[0].isalpha():
            continue
        syms.append(p[0])
    txt = fetch("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt").decode()
    for line in txt.strip().split("\n")[1:]:
        p = line.split("|")
        if len(p) < 7 or p[4] == "Y" or p[6] == "Y":     # ETF / test issue
            continue
        if _SKIP_NAME.search(p[1]) or not p[0].isalpha():
            continue
        syms.append(p[0])
    return sorted(set(syms))


# ------------------------------------------------- Yahoo batch quotes (crumb)
def yahoo_session():
    """Yahoo's quote API wants a session cookie + a 'crumb' token. Free, no
    account — just the same handshake a browser does."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = list(UA.items())
    try:
        op.open("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass                                   # that URL 404s by design; we just want its cookie
    crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb",
                    timeout=20).read().decode()
    return op, crumb


FIELDS = ("symbol,longName,regularMarketPrice,regularMarketChangePercent,"
          "regularMarketVolume,preMarketPrice,preMarketChangePercent,"
          "sharesOutstanding,marketCap,fullExchangeName")


def batch_quotes(symbols):
    op, crumb = yahoo_session()
    out = {}
    for i in range(0, len(symbols), 150):
        chunk = symbols[i:i + 150]
        url = (f"https://query1.finance.yahoo.com/v7/finance/quote"
               f"?symbols={','.join(chunk)}&fields={FIELDS}&crumb={crumb}")
        for attempt in range(3):
            try:
                d = json.load(op.open(url, timeout=30))
                for q in d["quoteResponse"]["result"]:
                    out[q["symbol"]] = q
                break
            except Exception:
                if attempt == 2:
                    log(f"quote batch {i // 150} failed; skipping")
                else:
                    time.sleep(5)
        time.sleep(0.4)
    return out


# ------------------------------------------------------------ SEC red flags
def edgar_flags(ticker, cik_table):
    cik = cik_table.get(ticker.upper())
    if cik is None:
        return {"status": "no SEC record"}
    out = {"status": "ok"}
    try:
        d = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                             headers=SEC_UA))
        rec = d["filings"]["recent"]
        cutoff = (dt.datetime.now(ET) - dt.timedelta(days=90)).strftime("%Y-%m-%d")
        dil = [f for f, fd in zip(rec["form"], rec["filingDate"])
               if fd >= cutoff and (f.startswith(DILUTION_FORMS) or f == "EFFECT")]
        out["dilution_90d"] = len(dil)
    except Exception:
        out["dilution_90d"] = None
    return out


def cik_map():
    d = json.loads(fetch("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_UA))
    return {v["ticker"].upper(): v["cik_str"] for v in d.values()}


# ----------------------------------------------------------------- scoring
def day_bars(symbol, day_iso):
    """5-minute prices for one day, pre-market and after-hours included."""
    d0 = dt.datetime.strptime(day_iso, "%Y-%m-%d").replace(tzinfo=ET)
    p1, p2 = int(d0.timestamp()), int((d0 + dt.timedelta(days=1)).timestamp())
    d = json.loads(fetch(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={p1}&period2={p2}&interval=5m&includePrePost=true"))
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for t, h, lo, c in zip(res.get("timestamp") or [], q.get("high", []),
                           q.get("low", []), q.get("close", [])):
        if None in (h, lo, c):
            continue
        ts = dt.datetime.fromtimestamp(t, ET)
        if ts.strftime("%Y-%m-%d") == day_iso:
            bars.append((ts, float(h), float(lo), float(c)))
    return bars


def score_detection(det):
    """The honest referee, same rules as the Pro Ticker tracker: buy at the
    first traded price AFTER our detection, then +25% target / -10% stop /
    else out at the close. Also record held-to-close and the best case."""
    out = {"status": "no data"}
    try:
        bars = day_bars(det["ticker"], det["date"])
    except Exception:
        return {"status": "price fetch failed"}
    if not bars:
        return out
    seen = dt.datetime.fromisoformat(det["detected_at"])
    post = [b for b in bars if b[0] >= seen]
    if len(post) < 2:
        return {"status": "detected too late in the day"}
    entry = post[0][3]
    seg = post[1:]
    rule, reason = None, "close"
    for _, h, lo, c in seg:
        tp = h >= entry * (1 + TAKE_PROFIT)
        sl = lo <= entry * (1 - STOP_LOSS)
        if tp and sl:
            rule, reason = -STOP_LOSS, "stop"      # both in one bar -> assume the stop (honest)
            break
        if tp:
            rule, reason = TAKE_PROFIT, "target"
            break
        if sl:
            rule, reason = -STOP_LOSS, "stop"
            break
    close = seg[-1][3]
    at_close = (close - entry) / entry
    if rule is None:
        rule = at_close
    return {"status": "ok", "entry": round(entry, 4),
            "at_close": at_close,
            "best_case": (max(h for _, h, _, _ in seg) - entry) / entry,
            "drawdown": (min(lo for _, _, lo, _ in seg) - entry) / entry,
            "rule_pct": rule, "rule_reason": reason,
            "result": "WIN" if rule > 0 else "LOSS"}


# -------------------------------------------------------------------- state
def load_state():
    """Yesterday's memory lives on the published page itself."""
    try:
        return json.loads(fetch(LIVE_DATA_URL).decode())
    except Exception:
        log("No previous data.json on the live site (first run?) — starting fresh.")
        return {"detections": {}}


# ---------------------------------------------------------------- detection
def detect(state, now):
    quotes = batch_quotes(stock_universe())
    log(f"Quoted {len(quotes)} stocks.")
    premarket = now.time() < dt.time(9, 30)
    dets = state["detections"]
    found = 0
    for sym, q in quotes.items():
        shares = q.get("sharesOutstanding")
        if not shares or shares > MAX_SHARES:
            continue
        if premarket:
            price = q.get("preMarketPrice")
            move = (q.get("preMarketChangePercent") or 0) / 100
            session = "pre-market"
        else:
            price = q.get("regularMarketPrice")
            move = (q.get("regularMarketChangePercent") or 0) / 100
            session = "regular"
            dollar_vol = (price or 0) * (q.get("regularMarketVolume") or 0)
            if dollar_vol < MIN_DOLLAR_VOL:
                continue
        if not price or not (PRICE_MIN <= price <= PRICE_MAX) or move < MIN_MOVE:
            continue
        k = f"{now:%Y-%m-%d}|{sym}"
        if k in dets:
            continue
        dets[k] = {"ticker": sym, "date": f"{now:%Y-%m-%d}",
                   "detected_at": now.isoformat(timespec="seconds"),
                   "session": session,
                   "name": (q.get("longName") or "")[:60],
                   "price_at_detection": price,
                   "move_at_detection": move,
                   "shares_out": shares,
                   "exchange": q.get("fullExchangeName", "")}
        found += 1
        log(f"CAUGHT {sym} +{move * 100:.0f}% at {price:g} ({session}, "
            f"{shares / 1e6:.1f}M shares)")
    log(f"{found} new detection(s) this run.")

    # EDGAR red flags for detections that don't have them yet (a few per run)
    need = [d for d in dets.values() if "flags" not in d]
    if need:
        table = cik_map()
        for d in need[:EDGAR_MAX_PER_RUN]:
            d["flags"] = edgar_flags(d["ticker"], table)
            time.sleep(0.3)


def score_pending(state, now):
    """After the close (or for past days), score every unscored detection."""
    today = now.date()
    market_done = now.time() >= dt.time(16, 10)
    n = 0
    for det in state["detections"].values():
        if det.get("score", {}).get("status") == "ok":
            continue
        day = dt.date.fromisoformat(det["date"])
        if (today - day).days > SCORE_MAX_AGE_DAYS:
            continue
        if day == today and not market_done:
            continue
        det["score"] = score_detection(det)
        n += 1
        time.sleep(0.7)
    if n:
        log(f"Scored {n} detection(s).")


# ---------------------------------------------------------------- rendering
def pct(x):
    return f"{x * 100:+.1f}%" if x is not None else "—"


def render(state, now):
    dets = sorted(state["detections"].values(),
                  key=lambda d: d["detected_at"], reverse=True)
    scored = [d for d in dets if d.get("score", {}).get("status") == "ok"]
    rows = []
    for d in dets[:400]:
        sc = d.get("score") or {}
        fl = d.get("flags") or {}
        flags = []
        if d["shares_out"] <= ULTRA_LOW:
            flags.append("⚠️ ultra-low float")
        if fl.get("dilution_90d"):
            flags.append(f'<span class="neg">{fl["dilution_90d"]} dilution filing(s)</span>')
        badge = ""
        if sc.get("result") == "WIN":
            badge = '<span class="pill win">WIN</span>'
        elif sc.get("result") == "LOSS":
            badge = '<span class="pill loss">LOSS</span>'
        elif d["date"] == f"{now:%Y-%m-%d}":
            badge = '<span class="pill live">live</span>'
        t = dt.datetime.fromisoformat(d["detected_at"]).strftime("%-I:%M %p")
        rp = sc.get("rule_pct")
        rule_cls = "pos" if (rp or 0) > 0 else ("neg" if rp is not None else "")
        rows.append(f"""<tr><td>{d['date']}</td><td class="tk">{html.escape(d['ticker'])}</td>
<td class="nm">{html.escape(d.get('name', ''))}</td><td>{t} <span class="note">{d['session']}</span></td>
<td class="num">{d['price_at_detection']:g}</td>
<td class="num pos">+{d['move_at_detection'] * 100:.0f}%</td>
<td class="num">{d['shares_out'] / 1e6:.1f}M</td>
<td class="ctx">{' · '.join(flags) or '—'}</td>
<td class="num {rule_cls}">{pct(rp)}<span class="note">{sc.get('rule_reason', '')}</span></td>
<td class="num">{pct(sc.get('at_close'))}</td>
<td class="num best">{pct(sc.get('best_case'))}</td>
<td>{badge}</td></tr>""")

    n = len(scored)
    stats_html = ""
    if n:
        rule_vals = [d["score"]["rule_pct"] for d in scored]
        wins = sum(1 for v in rule_vals if v > 0)
        closes = [d["score"]["at_close"] for d in scored]
        stats_html = f"""<div class="stats">
<div class="stat"><b>{n}</b><span>catches scored</span></div>
<div class="stat"><b>{100 * wins / n:.0f}%</b><span>win rate with the +25%/−10% rule</span></div>
<div class="stat"><b>{pct(sum(rule_vals) / n)}</b><span>avg per catch, with the rule</span></div>
<div class="stat"><b>{pct(sum(closes) / n)}</b><span>avg if held to the close</span></div>
</div>"""
    verdict = ("Too early to judge — building the track record."
               if n < 30 else "Track record live — see the table.")

    body = "".join(rows) or ('<tr><td colspan="12" class="dimc">Nothing caught yet — '
                             'the scanner is watching.</td></tr>')
    doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Runner Scanner — Project Lighthouse</title>
<style>
:root {{ color-scheme: light dark;
  --bg:#0b0e14; --card:#151a24; --line:#232b3a; --txt:#e6edf3; --dim:#8b98a9;
  --accent:#4da3ff; --good:#3fb950; --bad:#f85149; }}
@media (prefers-color-scheme:light) {{ :root {{
  --bg:#f4f6fa; --card:#fff; --line:#e2e8f0; --txt:#1a2230; --dim:#5b6675;
  --accent:#1f6feb; --good:#1a7f37; --bad:#cf222e; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--txt);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1150px; margin:0 auto; padding:28px 20px 60px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.02em; }}
.sub {{ color:var(--dim); margin:0 0 20px; font-size:13.5px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:18px 0; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
.stat b {{ display:block; font-size:24px; letter-spacing:-.02em; }}
.stat span {{ font-size:12px; color:var(--dim); }}
.scroll {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:13px; }}
th,td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim); }}
tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.tk {{ font-weight:700; }} .nm {{ color:var(--dim); max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
.pos {{ color:var(--good); }} .neg {{ color:var(--bad); }} .best {{ color:var(--good); }}
.pill {{ padding:2px 8px; border-radius:999px; font-size:10.5px; font-weight:700; }}
.pill.win {{ background:color-mix(in srgb,var(--good) 20%,transparent); color:var(--good); }}
.pill.loss {{ background:color-mix(in srgb,var(--bad) 20%,transparent); color:var(--bad); }}
.pill.live {{ background:color-mix(in srgb,var(--accent) 20%,transparent); color:var(--accent); }}
.note {{ color:var(--dim); font-size:10.5px; margin-left:5px; }}
.ctx {{ font-size:11.5px; color:var(--dim); white-space:normal; max-width:200px; }}
.dimc {{ color:var(--dim); text-align:center; padding:18px; }}
.verdict {{ display:inline-block; padding:6px 14px; border-radius:999px; font-weight:600;
  font-size:13px; margin:4px 0 10px; background:color-mix(in srgb,var(--accent) 16%,transparent); color:var(--accent); }}
.foot {{ margin-top:30px; color:var(--dim); font-size:12px; line-height:1.7; }}
.foot b {{ color:var(--txt); }}
</style></head><body><div class="wrap">
<h1>🔦 Runner Scanner</h1>
<p class="sub">Catches low-float stocks the moment they move — no middleman, no late alerts ·
scans every NASDAQ/NYSE stock every 15 minutes, pre-market included ·
updated {now:%A, %B %-d %Y at %-I:%M %p ET}</p>
<div class="verdict">{verdict}</div>
{stats_html}
<h2>Every catch, scored honestly</h2>
<div class="scroll"><table>
<thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>Caught at</th>
<th class="num">Price</th><th class="num">Move</th><th class="num">Shares</th>
<th>Red flags</th><th class="num">Rule (+25/−10)</th><th class="num">At close</th>
<th class="num">Best case</th><th></th></tr></thead>
<tbody>{body}</tbody></table></div>
<div class="foot">
<b>What this is.</b> Project Lighthouse's own detector. It watches every stock on NASDAQ and NYSE
and logs the moment a cheap, low-float stock (60&nbsp;million shares or fewer — so little supply that
buying pressure rockets the price) jumps 20%+, including in pre-market. Each catch gets red-flag
checks from SEC EDGAR (the government's public filings database): recent <b>dilution filings</b> are
paperwork that lets the company sell brand-new shares into the spike.<br>
<b>Scoring is automatic and unskippable.</b> After the close, every catch is scored as if bought at
the first price after detection, then sold at +25% profit or cut at −10% loss ("the rule"), plus the
honest held-to-close result and the perfect-exit best case. Wins and losses both stay on the board
forever.<br>
<b>Observation only. This scanner does not trade, and this is not investment advice.</b>
A pattern this scanner catches is usually a pump: someone else got in before the pop, and late buyers
are the exit. The whole point is to measure whether anything here is real — before any money is.
</div>
</div></body></html>"""
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(doc)
    (SITE / "data.json").write_text(json.dumps(state, indent=1))
    log(f"Rendered site with {len(dets)} detection(s), {n} scored.")


def main():
    now = dt.datetime.now(ET)
    state = load_state()
    weekday = now.isoweekday() <= 5
    in_window = dt.time(4, 0) <= now.time() <= dt.time(16, 10)
    if weekday and in_window:
        detect(state, now)
    else:
        log("Outside market window — skipping detection, scoring/rendering only.")
    score_pending(state, now)
    render(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
