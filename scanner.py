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

# ---- the knobs for the "In Play" list (real companies, liquid, alive) ------
# A different animal from the runner: not a pump lottery ticket, but a stock
# that is genuinely "in play" today — enough real money trading that you could
# get in AND out. William's "safish high-volume ticker of the day" idea.
IP_PRICE_MIN, IP_PRICE_MAX = 5.0, 100.0   # real companies, not delisting bait
IP_MIN_DOLLAR_VOL = 20_000_000            # $20M+ traded = liquid enough to exit
IP_MIN_RVOL = 3.0                         # 3x its own normal volume = something's up
IP_MOVE_MIN, IP_MOVE_MAX = 0.03, 0.15     # alive (3%+) but not a 50% pump
IP_TP, IP_SL = 0.08, 0.05                 # gentler rule for the calmer band
# a small, realistic day-trade target — William's actual goal is 0.5-5%/day,
# not moonshots. This measures: caught early enough that a +5% limit fills
# before a -5% stop? First touch decides, same honest rule as everything else.
SMALL_TP, SMALL_SL = 0.05, 0.05
EARLY_RUNWAY = 0.05                        # >=5% left after we caught it = "early"

# real-world friction. Commissions on stocks are ~$0 at modern brokers, but you
# lose the bid-ask spread + slippage on BOTH entry and exit. Thin, cheap,
# low-float runners have wide spreads; liquid $20M+ names much tighter. These
# are round-trip estimates subtracted from every realized trade so the numbers
# reflect what you'd actually keep — not a fantasy fill.
COST_RUNNER = 0.010                        # ~0.5% each side on cheap low-float names
COST_INPLAY = 0.003                        # ~0.15% each side on liquid names

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
          "regularMarketDayHigh,sharesOutstanding,marketCap,fullExchangeName,"
          "averageDailyVolume3Month,averageDailyVolume10Day")


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
    """5-minute prices for one day, pre-market and after-hours included.
    Returns (timestamp, high, low, close, volume) bars."""
    d0 = dt.datetime.strptime(day_iso, "%Y-%m-%d").replace(tzinfo=ET)
    p1, p2 = int(d0.timestamp()), int((d0 + dt.timedelta(days=1)).timestamp())
    d = json.loads(fetch(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={p1}&period2={p2}&interval=5m&includePrePost=true"))
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for t, h, lo, c, v in zip(res.get("timestamp") or [], q.get("high", []),
                              q.get("low", []), q.get("close", []),
                              q.get("volume", [])):
        if None in (h, lo, c):
            continue
        ts = dt.datetime.fromtimestamp(t, ET)
        if ts.strftime("%Y-%m-%d") == day_iso:
            bars.append((ts, float(h), float(lo), float(c), float(v or 0)))
    return bars


def premarket_read(bars, shares_out=None):
    """William's insight (2026-08-17): pre-market isn't the enemy, it's the
    X-ray. Two readings from it:
      pm_held      — 9:30 open vs the pre-market high. Defended (>=0.90) days
                     went on to new highs 28/31 times in our database;
                     abandoned (<0.75) days only 10/44.
      pm_turnover  — pre-market volume as a fraction of the whole float
                     (our visible proxy for real liquidity/participation:
                     a full-float churn is a crowd, a trickle is a ghost)."""
    pm = [b for b in bars if b[0].time() < dt.time(9, 30)]
    reg = [b for b in bars if dt.time(9, 30) <= b[0].time() < dt.time(16, 0)]
    out = {"pm_held": None, "pm_turnover": None, "peak_premarket": None}
    if pm and reg:
        pm_high = max(b[1] for b in pm)
        if pm_high > 0:
            out["pm_held"] = round(reg[0][3] / pm_high, 3)
        out["peak_premarket"] = pm_high >= max(b[1] for b in reg)
        if shares_out:
            out["pm_turnover"] = round(sum(b[4] for b in pm) / shares_out, 3)
    return out


def vwap_at(bars, upto):
    """Volume-weighted average price over regular-hours bars up to `upto`.
    VWAP is the day's average price weighted by how much traded at each level —
    the line big traders judge against. Price above it = buyers in control."""
    num = den = 0.0
    for t, h, lo, c, v in bars:
        if dt.time(9, 30) <= t.time() < dt.time(16, 0) and t <= upto and v:
            typical = (h + lo + c) / 3
            num += typical * v
            den += v
    return (num / den) if den else None


def score_detection(det, tp=TAKE_PROFIT, sl=STOP_LOSS, cost=0.0):
    """The honest referee, same rules as the Pro Ticker tracker: buy at the
    first traded price AFTER our detection, then +tp target / -sl stop /
    else out at the close. Realized returns (the rule, the small target) are
    reported NET of `cost` — the round-trip spread+slippage you actually pay.
    best_case / drawdown / at_close stay raw (they describe the stock, not a
    realized trade), and the exit tuner subtracts cost itself using `cost`."""
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
    for _, h, lo, c, _v in seg:
        hit_tp = h >= entry * (1 + tp)
        hit_sl = lo <= entry * (1 - sl)
        if hit_tp and hit_sl:
            rule, reason = -sl, "stop"      # both in one bar -> assume the stop (honest)
            break
        if hit_tp:
            rule, reason = tp, "target"
            break
        if hit_sl:
            rule, reason = -sl, "stop"
            break
    close = seg[-1][3]
    at_close = (close - entry) / entry
    if rule is None:
        rule = at_close
    best_case = (max(h for _, h, _, _, _ in seg) - entry) / entry
    # small realistic target (William's 0.5-5%/day goal): first touch of
    # +SMALL_TP vs -SMALL_SL after we caught it.
    small_pct, small_reason = None, "close"
    for _, h, lo, c, _v in seg:
        hit_tp = h >= entry * (1 + SMALL_TP)
        hit_sl = lo <= entry * (1 - SMALL_SL)
        if hit_tp and hit_sl:
            small_pct, small_reason = -SMALL_SL, "stop"
            break
        if hit_tp:
            small_pct, small_reason = SMALL_TP, "target"
            break
        if hit_sl:
            small_pct, small_reason = -SMALL_SL, "stop"
            break
    if small_pct is None:
        small_pct = at_close
    rule_net = rule - cost                   # what you actually keep after friction
    small_net = small_pct - cost
    out = {"status": "ok", "entry": round(entry, 4), "cost": cost,
           "at_close": at_close,
           "best_case": best_case,
           "drawdown": (min(lo for _, _, lo, _, _ in seg) - entry) / entry,
           "rule_pct": rule_net, "rule_reason": reason,
           "result": "WIN" if rule_net > 0 else "LOSS",
           "small_pct": small_net, "small_reason": small_reason,
           "early": best_case >= EARLY_RUNWAY,      # was there real upside left?
           # per-bar [favorable%, adverse%] from entry, so the exit-rule tuner
           # can replay ANY take-profit/stop-loss combo without re-fetching
           "path": [[round((h - entry) / entry, 4), round((lo - entry) / entry, 4)]
                    for _, h, lo, _c, _v in seg]}
    # VWAP read at the moment of detection: were buyers or sellers in control?
    vw = vwap_at(bars, seen)
    if vw:
        out["above_vwap"] = entry >= vw
    out.update(premarket_read(bars, det.get("shares_out")))
    return out


# ------------------------------------------------------- news & market weather
def fetch_news(query, count=5):
    """Yahoo's free news search — headlines with source, time, and link."""
    try:
        d = json.loads(fetch(
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={query}&newsCount={count}&quotesCount=0"))
        out = []
        for n in d.get("news", [])[:count]:
            out.append({"title": n.get("title", ""),
                        "publisher": n.get("publisher", ""),
                        "time": n.get("providerPublishTime", 0),
                        "link": n.get("link", "")})
        return out
    except Exception:
        return []


MACRO_SYMBOLS = [
    ("^GSPC", "S&P 500", "the 500 biggest US companies — the market's headline number"),
    ("^IXIC", "Nasdaq", "tech-heavy index"),
    ("^RUT", "Russell 2000 (small caps)", "small companies — the pond our runners swim in"),
    ("^VIX", "VIX (fear gauge)", "how violent traders expect the next 30 days to be — up = fear"),
    ("^TNX", "10-year Treasury yield", "the interest rate that prices everything else"),
    ("BTC-USD", "Bitcoin", "risk-appetite thermometer — speculative money's mood"),
]


def macro_snapshot():
    """One quote per macro dial, with day-change vs the prior close."""
    out = []
    for sym, label, why in MACRO_SYMBOLS:
        try:
            d = json.loads(fetch(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                f"?range=1d&interval=5m"))
            m = d["chart"]["result"][0]["meta"]
            price = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            chg = (price - prev) / prev if (price and prev) else None
            out.append({"symbol": sym, "label": label, "why": why,
                        "price": price, "chg": chg})
        except Exception:
            out.append({"symbol": sym, "label": label, "why": why,
                        "price": None, "chg": None})
        time.sleep(0.2)
    return out


def macro_readout(macro):
    """A one-line plain-English read of the day's weather."""
    by = {m["symbol"]: m.get("chg") for m in macro}
    spx, rut, vix = by.get("^GSPC"), by.get("^RUT"), by.get("^VIX")
    if spx is None:
        return "Market weather unavailable right now."
    if spx > 0.003 and (vix is None or vix < 0):
        mood = "Risk-ON: the big market is up and fear is falling — speculative money flows easier."
    elif spx < -0.003 and (vix is None or vix > 0):
        mood = "Risk-OFF: the big market is down and fear is rising — runners get abandoned faster."
    else:
        mood = "Mixed / quiet: no strong push from the big market either way."
    if rut is not None and spx is not None and rut - spx > 0.005:
        mood += " Small caps are leading — good sign for our pond."
    elif rut is not None and spx is not None and spx - rut > 0.005:
        mood += " Small caps are lagging the big market — our pond is out of favor."
    return mood


# -------------------------------------------------------------------- state
def load_state():
    """Yesterday's memory lives on the published page itself."""
    try:
        return json.loads(fetch(LIVE_DATA_URL).decode())
    except Exception:
        log("No previous data.json on the live site (first run?) — starting fresh.")
        return {"detections": {}, "inplay": {}}


# ---------------------------------------------------------------- detection
def detect_inplay(state, now, quotes):
    """The 'In Play' list: real, liquid companies genuinely moving today —
    not pump lottery tickets. Filters on RVOL (relative volume = today's
    volume vs the stock's own normal volume; the #1 day-trader metric) plus a
    real-money liquidity floor. Regular hours only (RVOL needs the day going)."""
    if now.time() < dt.time(9, 30) or now.time() >= dt.time(16, 0):
        return
    ip = state.setdefault("inplay", {})
    found = 0
    for sym, q in quotes.items():
        price = q.get("regularMarketPrice")
        move = (q.get("regularMarketChangePercent") or 0) / 100
        vol = q.get("regularMarketVolume") or 0
        avg = q.get("averageDailyVolume3Month") or q.get("averageDailyVolume10Day") or 0
        if not price or not (IP_PRICE_MIN <= price <= IP_PRICE_MAX):
            continue
        if not (IP_MOVE_MIN <= abs(move) <= IP_MOVE_MAX):
            continue
        if price * vol < IP_MIN_DOLLAR_VOL or not avg:
            continue
        # RVOL projected to a full day so an early-morning read isn't penalised
        mins_open = max((now - now.replace(hour=9, minute=30, second=0,
                                           microsecond=0)).total_seconds() / 60, 5)
        projected = vol * (390 / min(mins_open, 390))
        rvol = projected / avg
        if rvol < IP_MIN_RVOL:
            continue
        k = f"{now:%Y-%m-%d}|{sym}"
        if k in ip:
            continue
        ip[k] = {"ticker": sym, "date": f"{now:%Y-%m-%d}",
                 "detected_at": now.isoformat(timespec="seconds"),
                 "name": (q.get("longName") or "")[:60],
                 "price_at_detection": price,
                 "move_at_detection": move,
                 "rvol": round(rvol, 1),
                 "dollar_vol": round(price * vol),
                 "direction": "up" if move > 0 else "down",
                 "shares_out": q.get("sharesOutstanding"),
                 "exchange": q.get("fullExchangeName", "")}
        news = fetch_news(sym, count=1)
        if news:
            ip[k]["catalyst"] = news[0]
        found += 1
    if found:
        log(f"In Play: {found} new liquid mover(s).")


def detect(state, now):
    quotes = batch_quotes(stock_universe())
    log(f"Quoted {len(quotes)} stocks.")
    premarket = now.time() < dt.time(9, 30)
    detect_inplay(state, now, quotes)
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
        # freshness: is the stock still near its high of the day, or already
        # collapsing off the top? (late catches proved to be the losers)
        day_high = q.get("regularMarketDayHigh")
        off_high = (price / day_high) if (day_high and session == "regular") else None
        # day-2 pump: did we already catch this same ticker in the last 5 days?
        # A re-pump usually means the first crowd is looking for its exit.
        repeat = any(v["ticker"] == sym and v["date"] != f"{now:%Y-%m-%d}"
                     and (now.date() - dt.date.fromisoformat(v["date"])).days <= 5
                     for v in dets.values())
        dets[k] = {"ticker": sym, "date": f"{now:%Y-%m-%d}",
                   "detected_at": now.isoformat(timespec="seconds"),
                   "session": session,
                   "name": (q.get("longName") or "")[:60],
                   "price_at_detection": price,
                   "move_at_detection": move,
                   "off_high": off_high,
                   "repeat_runner": repeat,
                   "shares_out": shares,
                   "exchange": q.get("fullExchangeName", "")}
        found += 1
        # the catalyst: what news (if any) is this stock moving on right now?
        news = fetch_news(sym, count=1)
        if news:
            dets[k]["catalyst"] = news[0]
        # live pre-market X-ray for catches early in the regular session:
        # did buyers defend the pre-market high at the open, or abandon it?
        if session == "regular" and now.time() <= dt.time(11, 0):
            try:
                pr = premarket_read(day_bars(sym, f"{now:%Y-%m-%d}"), shares)
                dets[k]["pm_read"] = pr
            except Exception:
                pass
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

    def run(store, tp, sl, cost, need_field):
        n = 0
        for det in store.values():
            sc = det.get("score", {})
            if sc.get("status") == "ok" and need_field in sc:
                continue
            day = dt.date.fromisoformat(det["date"])
            if (today - day).days > SCORE_MAX_AGE_DAYS:
                continue
            if day == today and not market_done:
                continue
            res = score_detection(det, tp=tp, sl=sl, cost=cost)
            if res.get("status") == "ok" or sc.get("status") != "ok":
                det["score"] = res
            n += 1
            time.sleep(0.7)
        return n

    n1 = run(state.get("detections", {}), TAKE_PROFIT, STOP_LOSS, COST_RUNNER, "cost")
    n2 = run(state.get("inplay", {}), IP_TP, IP_SL, COST_INPLAY, "cost")
    if n1 or n2:
        log(f"Scored {n1} runner(s), {n2} in-play mover(s).")


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
        if d.get("repeat_runner"):
            flags.append('<span class="neg">🔁 repeat pump</span>')
        held = (sc.get("pm_held") if sc.get("pm_held") is not None
                else (d.get("pm_read") or {}).get("pm_held"))
        if held is not None:
            if held >= 0.9:
                flags.append('<span class="pos">🛡 defended pre-market high</span>')
            elif held < 0.75:
                flags.append('<span class="neg">🏳 abandoned pre-market high</span>')
        turn = (sc.get("pm_turnover") if sc.get("pm_turnover") is not None
                else (d.get("pm_read") or {}).get("pm_turnover"))
        if turn is not None and turn >= 1.0:
            flags.append(f"🌊 float traded {turn:.1f}× pre-market")
        if d.get("off_high") is not None and d["off_high"] < 0.85:
            flags.append('<span class="neg">📉 already fading off its high</span>')
        if d["move_at_detection"] >= 0.5:
            flags.append("🕑 caught late (already +50%)")
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
        cat = d.get("catalyst")
        cat_html = "—"
        if cat and cat.get("title"):
            title = html.escape(cat["title"][:80])
            link = html.escape(cat.get("link") or "")
            cat_html = (f'<a href="{link}" target="_blank" rel="noopener">{title}</a>'
                        if link else title)
        rows.append(f"""<tr><td>{d['date']}</td><td class="tk">{html.escape(d['ticker'])}</td>
<td class="nm">{html.escape(d.get('name', ''))}</td><td>{t} <span class="note">{d['session']}</span></td>
<td class="num">{d['price_at_detection']:g}</td>
<td class="num">+{d['move_at_detection'] * 100:.0f}%</td>
<td class="num">{d['shares_out'] / 1e6:.1f}M</td>
<td class="ctx">{cat_html}</td>
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
    # --- "what's working" splits: same catches, sliced by how we caught them ---
    def split_row(label, group):
        if len(group) < 3:
            return ""
        vals = [g["score"]["rule_pct"] for g in group]
        w = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        cls = "pos" if avg > 0 else "neg"
        return (f'<tr><td>{label}</td><td class="num">{len(group)}</td>'
                f'<td class="num">{100 * w / len(group):.0f}%</td>'
                f'<td class="num {cls}">{pct(avg)}</td></tr>')

    splits_html = ""
    if scored:
        fresh = [d for d in scored if d["move_at_detection"] < 0.5]
        late = [d for d in scored if d["move_at_detection"] >= 0.5]
        pre = [d for d in scored if d["session"] == "pre-market"]
        reg = [d for d in scored if d["session"] == "regular"]
        first = [d for d in scored if d.get("repeat_runner") is False]
        rep = [d for d in scored if d.get("repeat_runner") is True]
        defended = [d for d in scored if (d["score"].get("pm_held") or 0) >= 0.9]
        abandoned = [d for d in scored
                     if d["score"].get("pm_held") is not None
                     and d["score"]["pm_held"] < 0.75]
        churned = [d for d in scored if (d["score"].get("pm_turnover") or 0) >= 1.0]
        srows = (split_row("🛡 Defended its pre-market high at the open", defended)
                 + split_row("🏳 Abandoned its pre-market high at the open", abandoned)
                 + split_row("🌊 Float fully traded before the open", churned)
                 + split_row("Caught early (under +50% when spotted)", fresh)
                 + split_row("Caught late (already +50% or more)", late)
                 + split_row("Caught in pre-market", pre)
                 + split_row("Caught during market hours", reg)
                 + split_row("First-time runner", first)
                 + split_row("🔁 Repeat pump (also ran in last 5 days)", rep))
        if srows:
            splits_html = f"""
<h2>What's working — same catches, sliced up</h2>
<p class="sub">Every group below uses the same +25%/−10% exit rule. This is how the scanner learns:
find which KINDS of catches make money before trusting any of them.</p>
<div class="scroll"><table>
<thead><tr><th>Kind of catch</th><th class="num">Catches</th><th class="num">Win rate</th>
<th class="num">Avg per catch</th></tr></thead>
<tbody>{srows}</tbody></table></div>"""

    verdict = ("Too early to judge — building the track record."
               if n < 30 else "Track record live — see the table.")

    # ---- market weather + news tabs ----------------------------------------
    macro = macro_snapshot()
    mood = macro_readout(macro)
    mcards = []
    for m in macro:
        chg = m.get("chg")
        cls = "pos" if (chg or 0) > 0 else ("neg" if chg is not None else "")
        val = f"{m['price']:,.2f}" if m.get("price") else "—"
        mcards.append(f"""<div class="stat"><b class="{cls}">{pct(chg)}</b>
<span><b style="font-size:13px">{m['label']}</b> · {val}</span>
<span>{m['why']}</span></div>""")
    weather_html = (f'<p class="sub" style="font-size:15px"><b>{mood}</b></p>'
                    f'<div class="stats">{"".join(mcards)}</div>'
                    '<p class="sub">Why this tab exists: micro-cap runners live inside the big market\'s '
                    'mood. On risk-off days (market down, fear up) even good catches get abandoned '
                    'faster. Same data source as everything else — free, checked every run.</p>')

    def news_list(items):
        lis = []
        for nw in items:
            ts = dt.datetime.fromtimestamp(nw["time"], ET).strftime("%b %-d, %-I:%M %p ET") if nw.get("time") else ""
            title = html.escape(nw.get("title", "")[:120])
            link = html.escape(nw.get("link") or "")
            pub = html.escape(nw.get("publisher", ""))
            body_txt = f'<a href="{link}" target="_blank" rel="noopener">{title}</a>' if link else title
            lis.append(f'<li>{body_txt} <span class="note">{pub} · {ts}</span></li>')
        return "<ul class=\"news\">" + "".join(lis) + "</ul>" if lis else '<p class="sub">No headlines fetched this run.</p>'

    market_news = fetch_news("stock market", 8)
    todays = [d for d in dets if d["date"] == f"{now:%Y-%m-%d}"][:8]
    ticker_news = []
    for d in todays:
        for nw in fetch_news(d["ticker"], 2):
            nw["title"] = f"[{d['ticker']}] " + nw.get("title", "")
            ticker_news.append(nw)
        time.sleep(0.2)
    ticker_news.sort(key=lambda x: -(x.get("time") or 0))
    news_html = ("<h2>Today's caught tickers — their headlines</h2>"
                 + news_list(ticker_news[:16])
                 + "<h2>The big market</h2>" + news_list(market_news)
                 + '<p class="sub">Headlines are pulled automatically and are NOT vetted — a press '
                 'release on a micro-cap is often part of the pump itself. A catch with NO news at '
                 'all is its own red flag: something is moving it, and it isn\'t public information.</p>')

    body = "".join(rows) or ('<tr><td colspan="13" class="dimc">Nothing caught yet — '
                             'the scanner is watching.</td></tr>')

    # ---- In Play tab: liquid real-company movers ----------------------------
    ipall = sorted(state.get("inplay", {}).values(),
                   key=lambda d: d["detected_at"], reverse=True)
    ip_scored = [d for d in ipall if d.get("score", {}).get("status") == "ok"]
    ip_rows = []
    for d in ipall[:400]:
        sc = d.get("score") or {}
        cat = d.get("catalyst")
        cat_html = "—"
        if cat and cat.get("title"):
            title = html.escape(cat["title"][:80])
            link = html.escape(cat.get("link") or "")
            cat_html = (f'<a href="{link}" target="_blank" rel="noopener">{title}</a>'
                        if link else title)
        vw = sc.get("above_vwap")
        vwap_html = ("🟢 above" if vw else ("🔴 below" if vw is False else "—"))
        rp = sc.get("rule_pct")
        rcls = "pos" if (rp or 0) > 0 else ("neg" if rp is not None else "")
        badge = ""
        if sc.get("result") == "WIN":
            badge = '<span class="pill win">WIN</span>'
        elif sc.get("result") == "LOSS":
            badge = '<span class="pill loss">LOSS</span>'
        elif d["date"] == f"{now:%Y-%m-%d}":
            badge = '<span class="pill live">live</span>'
        t = dt.datetime.fromisoformat(d["detected_at"]).strftime("%-I:%M %p")
        arrow = "▲" if d.get("direction") == "up" else "▼"
        acls = "pos" if d.get("direction") == "up" else "neg"
        ip_rows.append(f"""<tr><td>{d['date']}</td><td class="tk">{html.escape(d['ticker'])}</td>
<td class="nm">{html.escape(d.get('name', ''))}</td><td>{t}</td>
<td class="num">{d['price_at_detection']:g}</td>
<td class="num {acls}">{arrow}{abs(d['move_at_detection']) * 100:.1f}%</td>
<td class="num">{d.get('rvol', '—')}×</td>
<td class="num">${d.get('dollar_vol', 0) / 1e6:.0f}M</td>
<td>{vwap_html}</td><td class="ctx">{cat_html}</td>
<td class="num {rcls}">{pct(rp)}</td><td class="num best">{pct(sc.get('best_case'))}</td>
<td>{badge}</td></tr>""")
    ip_body = "".join(ip_rows) or ('<tr><td colspan="13" class="dimc">No liquid movers '
                                   'logged yet — the In-Play scan runs during market hours.</td></tr>')
    ip_stats = ""
    if ip_scored:
        rv = [d["score"]["rule_pct"] for d in ip_scored]
        w = sum(1 for x in rv if x > 0)
        av = [d for d in ip_scored if d["score"].get("above_vwap")]
        bv = [d for d in ip_scored if d["score"].get("above_vwap") is False]
        def ipavg(g):
            v = [x["score"]["rule_pct"] for x in g]
            return (100 * sum(1 for x in v if x > 0) / len(v), 100 * sum(v) / len(v)) if v else (0, 0)
        avw, ava = ipavg(av)
        bvw, bva = ipavg(bv)
        ip_stats = f"""<div class="stats">
<div class="stat"><b>{len(ip_scored)}</b><span>movers scored</span></div>
<div class="stat"><b>{100 * w / len(ip_scored):.0f}%</b><span>win rate (+{int(IP_TP*100)}%/−{int(IP_SL*100)}% rule)</span></div>
<div class="stat"><b>{pct(sum(rv) / len(ip_scored))}</b><span>avg per mover</span></div>
</div>"""
        if av and bv:
            ip_stats += (f'<p class="sub">Above VWAP when caught ({len(av)}): '
                         f'<b>{avw:.0f}% win, {bva if False else ava:+.1f}% avg</b> · '
                         f'below VWAP ({len(bv)}): <b>{bvw:.0f}% win, {bva:+.1f}% avg</b>. '
                         'VWAP = the day\'s volume-weighted average price; above it means buyers '
                         'were in control at the moment we caught it.</p>')
    inplay_html = f"""{ip_stats}
<h2>In Play today — liquid real-company movers</h2>
<div class="scroll"><table>
<thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>Caught at</th>
<th class="num">Price</th><th class="num">Move</th><th class="num">RVOL</th>
<th class="num">$ traded</th><th>VWAP</th><th>Catalyst</th>
<th class="num">Rule (+{int(IP_TP*100)}/−{int(IP_SL*100)})</th><th class="num">Ran after</th><th></th></tr></thead>
<tbody>{ip_body}</tbody></table></div>
<p class="sub"><b>What this list is.</b> Real companies ($5–$100), $20M+ traded today, moving 3–15% on
<b>3×+ their normal volume</b> ("RVOL"). Not pump lottery tickets — these are liquid enough to get in
AND out. The idea: a calmer, safer daily "what's in play" list, scored with a gentler +{int(IP_TP*100)}%/−{int(IP_SL*100)}%
rule. Observation only, same honest scoring, {'' if len(ip_scored) >= 30 else 'still building the record — '}not advice.</p>"""

    # ---- exit-rule tuner: replay every take-profit/stop-loss combo ----------
    def replay(path, at_close, tp, sl, cost=0.0):
        """First touch wins: walk the bars; if +tp reached, +tp; if -sl reached,
        -sl; if a single bar hit both, assume the stop (honest). Never triggered
        -> exit at the close. Result is NET of round-trip cost (spread+slippage)."""
        out = at_close
        for fav, adv in path:
            hit_tp = fav >= tp
            hit_sl = adv <= -sl
            if hit_tp and hit_sl:
                out = -sl
                break
            if hit_tp:
                out = tp
                break
            if hit_sl:
                out = -sl
                break
        return out - cost

    tunable = [d for d in (list(state.get("detections", {}).values())
                           + list(state.get("inplay", {}).values()))
               if d.get("score", {}).get("status") == "ok" and d["score"].get("path")]
    TP_GRID = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]
    SL_GRID = [0.03, 0.05, 0.08, 0.10, 0.12, 0.15]
    tuner_html = '<p class="sub">Not enough scored catches with price paths yet.</p>'
    if len(tunable) >= 10:
        grid = {}          # (tp,sl) -> list of per-trade returns
        for d in tunable:
            sc = d["score"]
            for tp in TP_GRID:
                for sl in SL_GRID:
                    grid.setdefault((tp, sl), []).append(
                        replay(sc["path"], sc["at_close"], tp, sl, sc.get("cost", 0)))
        cells = {}
        best = None
        for (tp, sl), rets in grid.items():
            n2 = len(rets)
            avg = sum(rets) / n2
            wr = 100 * sum(1 for r in rets if r > 0) / n2
            # outlier-robust: average with the top 3 winners removed
            trimmed = sorted(rets)[:-3] if n2 > 6 else rets
            avg_trim = sum(trimmed) / len(trimmed)
            cells[(tp, sl)] = (avg, wr, avg_trim)
            # rank by the trimmed average — the edge that ISN'T just a lottery win
            if best is None or avg_trim > cells[best][2]:
                best = (tp, sl)
        # build the heatmap: rows = take-profit, cols = stop-loss
        def cell_color(avg_trim):
            # diverging: red (neg) -> neutral -> green (pos), scaled to +/-3%
            v = max(-1, min(1, avg_trim / 0.03))
            if v >= 0:
                return f"color-mix(in srgb, var(--good) {int(v * 55)}%, transparent)"
            return f"color-mix(in srgb, var(--bad) {int(-v * 55)}%, transparent)"
        head = "".join(f'<th class="num">−{int(sl * 100)}%</th>' for sl in SL_GRID)
        body_rows = []
        for tp in TP_GRID:
            tds = []
            for sl in SL_GRID:
                avg, wr, avg_trim = cells[(tp, sl)]
                star = " ★" if (tp, sl) == best else ""
                bd = "outline:2px solid var(--accent);" if (tp, sl) == best else ""
                tds.append(f'<td class="num" title="win {wr:.0f}% · avg {avg*100:+.1f}%" '
                           f'style="background:{cell_color(avg_trim)};{bd}">'
                           f'{avg_trim * 100:+.1f}%{star}</td>')
            body_rows.append(f'<tr><th class="num">+{int(tp * 100)}%</th>{"".join(tds)}</tr>')
        bt, bs = best
        bavg, bwr, btrim = cells[best]
        # compounding illustration: $1000, this rule, one trade per trading day,
        # risking a fixed 20% of the account each trade (illustration only)
        frac = 0.20
        bal = 1000.0
        for d in sorted(tunable, key=lambda x: x["detected_at"]):
            r = replay(d["score"]["path"], d["score"]["at_close"], bt, bs,
                       d["score"].get("cost", 0))
            bal *= (1 + frac * r)
        tuner_html = f"""
<p class="sub" style="font-size:15px">Every catch we've scored, replayed under <b>every</b>
combination of take-profit and stop-loss. The winning box (★) is chosen by the <b>outlier-robust</b>
average — the top 3 lucky moonshots removed — so we reward a rule that works on ordinary catches, not
one flattered by a couple of lottery tickets.</p>
<div class="stats">
<div class="stat"><b class="{'pos' if btrim > 0 else 'neg'}">+{int(bt*100)}% / −{int(bs*100)}%</b><span>best take-profit / stop-loss found</span></div>
<div class="stat"><b>{bwr:.0f}%</b><span>win rate at that rule</span></div>
<div class="stat"><b class="{'pos' if bavg > 0 else 'neg'}">{pct(bavg)}</b><span>avg per trade (all catches)</span></div>
<div class="stat"><b class="{'pos' if btrim > 0 else 'neg'}">{pct(btrim)}</b><span>avg with top-3 winners removed</span></div>
</div>
<h2>Exit-rule grid <span class="note">outlier-robust avg return per trade · rows = take-profit, cols = stop-loss</span></h2>
<div class="scroll"><table>
<thead><tr><th class="num">TP ↓ / SL →</th>{head}</tr></thead>
<tbody>{''.join(body_rows)}</tbody></table></div>
<p class="sub">Greener = more profit per trade. The ★ box is the current best exit rule on
{len(tunable)} catches. As an illustration, $1,000 run through all {len(tunable)} catches in order using
the ★ rule and risking 20% each time would be <b>${bal:,.0f}</b> — a rough feel, not a promise.</p>
<div class="foot" style="margin-top:14px;border:0;padding-top:0">
<b>These returns are net of costs</b> (~1% round trip on runners, ~0.3% on liquid names — spread +
slippage; commissions ~$0). That's why a symmetric small rule struggles: costs eat thin edges.<br>
<b>Read this with real skepticism.</b> Trying dozens of rules and picking the best is called
<b>overfitting</b> — the winner is partly luck, and it will look worse on catches we haven't seen yet.
That's exactly why the honest test is whether this rule keeps winning on FUTURE catches, which the
scanner keeps logging automatically. The grid is a hypothesis generator, not a green light. Observation
only, not advice.</div>"""

    # ---- "Caught before they rocketed" — the honest proof-of-concept -------
    all_scored = [d for d in scored] + [d for d in state.get("inplay", {}).values()
                                        if d.get("score", {}).get("status") == "ok"]
    early = [d for d in all_scored if d["score"].get("early")]
    early.sort(key=lambda d: -(d["score"].get("best_case") or 0))
    n_all = len(all_scored)
    n_early = len(early)
    small = [d["score"]["small_pct"] for d in all_scored
             if d["score"].get("small_pct") is not None]
    small_wins = sum(1 for v in small if v > 0)
    early_rate = (100 * n_early / n_all) if n_all else 0
    proof_cards = []
    for d in early[:24]:
        sc = d["score"]
        bc = sc.get("best_case") or 0
        t = dt.datetime.fromisoformat(d["detected_at"]).strftime("%b %-d, %-I:%M %p")
        sm = sc.get("small_pct")
        sr = sc.get("small_reason")
        smcls = "g" if (sm or 0) > 0 else "r"
        smtxt = ("+5% target hit" if sr == "target"
                 else ("−5% stop hit" if sr == "stop" else f"{(sm or 0) * 100:+.0f}% by close"))
        proof_cards.append(f"""<div class="mv up">
<div class="mv-top"><span class="mv-tk">{html.escape(d['ticker'])}</span>
<span class="mv-mv up">▲{bc * 100:.0f}%</span></div>
<div class="mv-nm">room left after we caught it · {t}</div>
<div class="mv-meta"><span class="tag {smcls}">{smtxt}</span>
<span class="tag">caught at {d['price_at_detection']:g}</span></div></div>""")
    proof_grid = (f'<div class="movers">{"".join(proof_cards)}</div>' if proof_cards
                  else '<div class="empty">No early catches scored yet — the record is still building.</div>')
    small_avg = (sum(small) / len(small)) if small else None
    rocketed_html = f"""
<p class="sub" style="font-size:15px">The honest test of whether this tool works: how often do we
catch a stock while there's <b>still room left to run</b> — not after it already peaked? Every card below
is a real catch where the stock kept climbing <b>after</b> our detection. No cherry-picking: the
headline number is the share of <i>all</i> catches that were early.</p>
<div class="stats">
<div class="stat"><b class="{'pos' if early_rate >= 50 else ''}">{early_rate:.0f}%</b>
<span>of catches had 5%+ room left AFTER we caught them ({n_early} of {n_all})</span></div>
<div class="stat"><b>{100 * small_wins / len(small):.0f}%</b>
<span>hit a +5% target before a −5% stop ({small_wins} of {len(small)})</span></div>
<div class="stat"><b class="{'pos' if (small_avg or 0) > 0 else 'neg'}">{pct(small_avg)}</b>
<span>avg per catch with a small +5%/−5% day-trade rule</span></div>
</div>
<h2>🚀 Stocks we caught before they rocketed</h2>
{proof_grid}
<div class="foot" style="margin-top:16px;border:0;padding-top:0">
<b>Costs are baked in.</b> The +5% target and rule returns are <b>net</b> of an estimated round-trip
cost (~1% on thin runners, ~0.3% on liquid names — the spread + slippage you actually pay; commissions
are ~$0 on modern brokers). "Room left" is raw — it describes the stock, not a trade you made.<br>
<b>How to read this honestly.</b> "Room left" is the most the stock rose after our catch — a perfect
exit nobody hits every time. The number that matters for your goal is the <b>+5% target hit rate</b>:
a disciplined limit order at +5%, stop at −5%, first one to trigger wins. That's the closest thing to
the 0.5–5%/day you're aiming for. This is still observation only — not advice, and not yet a green light
to trade. We're proving the edge first.</div>"""

    # ---- hero strip: the one-glance focus at the very top -------------------
    today_iso = f"{now:%Y-%m-%d}"
    todays_runners = [d for d in dets if d["date"] == today_iso]
    todays_inplay = [d for d in ipall if d["date"] == today_iso]
    spx = next((m.get("chg") for m in macro if m["symbol"] == "^GSPC"), None)
    hero = f"""<div class="hero">
<p class="hero-mood">{mood}</p>
<div class="hero-row">
<div class="hero-fig"><b>{len(todays_runners)}</b><span>runners caught today</span></div>
<div class="hero-fig"><b>{len(todays_inplay)}</b><span>liquid movers in play</span></div>
<div class="hero-fig"><b class="{'pos' if (spx or 0) > 0 else 'neg'}">{pct(spx)}</b><span>S&amp;P 500 today</span></div>
<div class="hero-fig"><b>{n}</b><span>runners scored all-time</span></div>
</div></div>"""

    def mover_cards(items, kind):
        """Big readable cards for today's catches — the eye lands here first."""
        cards = []
        for d in items[:12]:
            up = d.get("move_at_detection", 0) >= 0 if kind == "inplay" else True
            mv = d.get("move_at_detection", 0)
            dircls = "up" if up else "down"
            arrow = "▲" if up else "▼"
            tags = []
            if kind == "runner":
                tags.append(f'<span class="tag">{d["shares_out"] / 1e6:.0f}M shares</span>')
                if d["shares_out"] <= ULTRA_LOW:
                    tags.append('<span class="tag r">ultra-low float</span>')
                held = (d.get("pm_read") or {}).get("pm_held")
                if held is not None and held >= 0.9:
                    tags.append('<span class="tag g">🛡 defended</span>')
                elif held is not None and held < 0.75:
                    tags.append('<span class="tag r">🏳 abandoned</span>')
            else:
                tags.append(f'<span class="tag a">RVOL {d.get("rvol", "?")}×</span>')
                tags.append(f'<span class="tag">${d.get("dollar_vol", 0) / 1e6:.0f}M</span>')
            cat = d.get("catalyst") or {}
            cat_html = ""
            if cat.get("title"):
                link = html.escape(cat.get("link") or "")
                title = html.escape(cat["title"][:70])
                cat_html = (f'<div class="mv-cat">📰 <a href="{link}" target="_blank" rel="noopener">{title}</a></div>'
                            if link else f'<div class="mv-cat">📰 {title}</div>')
            t = dt.datetime.fromisoformat(d["detected_at"]).strftime("%-I:%M %p")
            cards.append(f"""<div class="mv {dircls}">
<div class="mv-top"><span class="mv-tk">{html.escape(d['ticker'])}</span>
<span class="mv-mv {dircls}">{arrow}{abs(mv) * 100:.0f}%</span></div>
<div class="mv-nm">{html.escape(d.get('name', '') or '&nbsp;')} · {t}</div>
<div class="mv-meta">{''.join(tags)}</div>{cat_html}</div>""")
        return f'<div class="movers">{"".join(cards)}</div>' if cards else \
            '<div class="empty">Nothing caught yet today — the scanner is watching. 👀</div>'

    runner_cards = mover_cards(todays_runners, "runner")
    inplay_cards = mover_cards(todays_inplay, "inplay")

    doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Runner Scanner — Project Lighthouse</title>
<style>
:root {{ color-scheme: light dark;
  /* calm light palette (default) — soft, low-glare, one accent */
  --bg:#f6f7fb; --card:#ffffff; --soft:#eef1f7; --line:#e4e8f0;
  --txt:#1c2433; --dim:#69748a; --accent:#3a6df0; --accent-soft:#e8eefe;
  --good:#1a8f4a; --good-soft:#e3f5ea; --bad:#d23b3b; --bad-soft:#fdeaea;
  --shadow:0 1px 2px rgba(20,30,50,.04), 0 4px 16px rgba(20,30,50,.06); }}
@media (prefers-color-scheme:dark) {{ :root {{
  --bg:#0e1119; --card:#171b26; --soft:#1e2331; --line:#272d3d;
  --txt:#e8edf6; --dim:#95a0b5; --accent:#6c9bff; --accent-soft:#1b2740;
  --good:#48c07a; --good-soft:#152a1f; --bad:#f0685f; --bad-soft:#2c1a1a;
  --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.28); }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--txt);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:26px 20px 80px; }}
h1 {{ font-size:25px; margin:0 0 3px; letter-spacing:-.02em; display:flex; align-items:center; gap:9px; }}
h2 {{ font-size:17px; margin:30px 0 14px; letter-spacing:-.01em; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.sub {{ color:var(--dim); margin:0 0 18px; font-size:14px; max-width:760px; }}

/* sticky, always-there navigation — one less thing to hunt for */
.tabs {{ position:sticky; top:0; z-index:20; display:flex; gap:8px; flex-wrap:wrap;
  margin:18px -20px 22px; padding:12px 20px; background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:saturate(1.4) blur(10px); border-bottom:1px solid var(--line); }}
.tab {{ background:var(--card); color:var(--txt); border:1px solid var(--line); border-radius:999px;
  padding:9px 18px; font-size:14px; font-weight:600; cursor:pointer; text-decoration:none;
  display:inline-block; transition:all .12s; }}
.tab:hover {{ border-color:var(--accent); color:var(--accent); }}
.tab.active {{ background:var(--accent); color:#fff; border-color:var(--accent); box-shadow:var(--shadow); }}

/* the one-glance focus strip at the very top */
.hero {{ background:var(--card); border:1px solid var(--line); border-radius:18px;
  padding:20px 22px; box-shadow:var(--shadow); margin-bottom:12px; }}
.hero-mood {{ font-size:17px; font-weight:650; margin:0 0 12px; line-height:1.45; }}
.hero-row {{ display:flex; gap:26px; flex-wrap:wrap; }}
.hero-fig {{ display:flex; flex-direction:column; }}
.hero-fig b {{ font-size:27px; letter-spacing:-.02em; line-height:1.1; }}
.hero-fig span {{ font-size:12.5px; color:var(--dim); margin-top:2px; }}

/* stat tiles */
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:12px; margin:16px 0 6px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:15px 17px; box-shadow:var(--shadow); }}
.stat b {{ display:block; font-size:23px; letter-spacing:-.02em; }}
.stat span {{ font-size:12.5px; color:var(--dim); display:block; margin-top:2px; }}
.stat b.pos {{ color:var(--good); }} .stat b.neg {{ color:var(--bad); }}

/* big readable mover cards — the Trade-Ideas idea, calmer */
.movers {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(184px,1fr)); gap:12px; margin:4px 0 8px; }}
.mv {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--dim);
  border-radius:14px; padding:14px 15px; box-shadow:var(--shadow); }}
.mv.up {{ border-left-color:var(--good); }} .mv.down {{ border-left-color:var(--bad); }}
.mv-top {{ display:flex; align-items:baseline; justify-content:space-between; gap:8px; }}
.mv-tk {{ font-size:19px; font-weight:800; letter-spacing:-.01em; }}
.mv-mv {{ font-size:16px; font-weight:700; }}
.mv-mv.up {{ color:var(--good); }} .mv-mv.down {{ color:var(--bad); }}
.mv-nm {{ font-size:12px; color:var(--dim); margin:3px 0 9px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.mv-meta {{ display:flex; gap:6px; flex-wrap:wrap; }}
.tag {{ font-size:11px; font-weight:600; padding:3px 9px; border-radius:999px;
  background:var(--soft); color:var(--dim); }}
.tag.g {{ background:var(--good-soft); color:var(--good); }}
.tag.r {{ background:var(--bad-soft); color:var(--bad); }}
.tag.a {{ background:var(--accent-soft); color:var(--accent); }}
.mv-cat {{ font-size:11.5px; color:var(--dim); margin-top:9px; line-height:1.4; }}
.mv-cat a {{ color:var(--dim); text-decoration:none; }} .mv-cat a:hover {{ color:var(--accent); }}

/* details tables — lighter, roomier, zebra */
details.more {{ margin:14px 0 4px; }}
details.more > summary {{ cursor:pointer; font-size:13.5px; color:var(--accent); font-weight:600;
  list-style:none; padding:8px 0; }}
details.more > summary::-webkit-details-marker {{ display:none; }}
details.more > summary:before {{ content:"▸ "; }}
details.more[open] > summary:before {{ content:"▾ "; }}
.scroll {{ overflow-x:auto; border-radius:14px; box-shadow:var(--shadow); }}
table {{ width:100%; border-collapse:collapse; background:var(--card); font-size:13.5px; }}
th,td {{ padding:11px 13px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim);
  position:sticky; top:57px; background:var(--card); }}
tbody tr:nth-child(even) {{ background:color-mix(in srgb,var(--soft) 55%,transparent); }}
tbody tr:hover {{ background:var(--accent-soft); }}
tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.tk {{ font-weight:700; }} .nm {{ color:var(--dim); max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
.pos {{ color:var(--good); font-weight:600; }} .neg {{ color:var(--bad); font-weight:600; }} .best {{ color:var(--good); }}
.pill {{ padding:3px 10px; border-radius:999px; font-size:11px; font-weight:700; }}
.pill.win {{ background:var(--good-soft); color:var(--good); }}
.pill.loss {{ background:var(--bad-soft); color:var(--bad); }}
.pill.live {{ background:var(--accent-soft); color:var(--accent); }}
.note {{ color:var(--dim); font-size:11px; margin-left:5px; }}
.ctx {{ font-size:12px; color:var(--dim); white-space:normal; max-width:210px; }}
.dimc {{ color:var(--dim); text-align:center; padding:22px; }}
.verdict {{ display:inline-block; padding:7px 15px; border-radius:999px; font-weight:600;
  font-size:13.5px; margin:2px 0 4px; background:var(--accent-soft); color:var(--accent); }}
.empty {{ background:var(--card); border:1px dashed var(--line); border-radius:14px;
  padding:26px; text-align:center; color:var(--dim); }}
ul.news {{ list-style:none; padding:0; margin:0 0 20px; display:flex; flex-direction:column; gap:9px; }}
ul.news li {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 15px; box-shadow:var(--shadow); }}
ul.news a {{ color:var(--txt); text-decoration:none; font-weight:600; }}
ul.news a:hover {{ color:var(--accent); }}
.foot {{ margin-top:34px; padding-top:18px; border-top:1px solid var(--line); color:var(--dim); font-size:12.5px; line-height:1.75; }}
.foot b {{ color:var(--txt); }}
</style></head><body><div class="wrap">
<h1>🔦 Runner Scanner</h1>
<p class="sub">Catches low-float stocks the moment they move — no middleman, no late alerts ·
scans every NASDAQ/NYSE stock every 15 minutes, pre-market included ·
updated {now:%A, %B %-d %Y at %-I:%M %p ET}</p>
<nav class="tabs">
<button class="tab active" data-t="rocketed">🚀 Caught early</button>
<button class="tab" data-t="tuner">🎯 Exit tuner</button>
<button class="tab" data-t="catches">Runners</button>
<button class="tab" data-t="inplay">In Play</button>
<button class="tab" data-t="working">What's working</button>
<button class="tab" data-t="weather">Market weather</button>
<button class="tab" data-t="news">News</button>
<a class="tab" href="patterns.html">Chart patterns ↗</a>
</nav>
<section id="tab-rocketed">
{hero}
{rocketed_html}
</section>
<section id="tab-catches" hidden>
<div class="verdict">{verdict}</div>
<h2>🔥 Caught today</h2>
{runner_cards}
{stats_html}
<details class="more"><summary>Show every catch, scored ({len(dets)} rows)</summary>
<div class="scroll"><table>
<thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>Caught at</th>
<th class="num">Price</th><th class="num">Already up (when caught)</th><th class="num">Shares</th>
<th>Catalyst (news at catch)</th>
<th>Red flags</th><th class="num">Rule (+25/−10)</th><th class="num">At close</th>
<th class="num">Ran after catch</th><th></th></tr></thead>
<tbody>{body}</tbody></table></div></details>
</section>
<section id="tab-tuner" hidden>{tuner_html}</section>
<section id="tab-inplay" hidden>
<h2>💧 In Play today <span class="note">real companies · liquid · calmer</span></h2>
{inplay_cards}
{ip_stats}
<details class="more"><summary>Show every liquid mover, scored ({len(ipall)} rows)</summary>
<div class="scroll"><table>
<thead><tr><th>Date</th><th>Ticker</th><th>Company</th><th>Caught at</th>
<th class="num">Price</th><th class="num">Move</th><th class="num">RVOL</th>
<th class="num">$ traded</th><th>VWAP</th><th>Catalyst</th>
<th class="num">Rule (+{int(IP_TP*100)}/−{int(IP_SL*100)})</th><th class="num">Ran after</th><th></th></tr></thead>
<tbody>{ip_body}</tbody></table></div></details>
<p class="sub"><b>What this list is.</b> Real companies ($5–$100), $20M+ traded today, moving 3–15% on
<b>3×+ their normal volume</b> ("RVOL"). Not pump lottery tickets — liquid enough to get in AND out.
A calmer daily "what's in play" list, scored with a gentler +{int(IP_TP*100)}%/−{int(IP_SL*100)}% rule.
Observation only, same honest scoring, {'' if len(ip_scored) >= 30 else 'still building the record — '}not advice.</p></section>
<section id="tab-working" hidden>{splits_html or '<p class="sub">Not enough scored catches yet.</p>'}</section>
<section id="tab-weather" hidden>{weather_html}</section>
<section id="tab-news" hidden>{news_html}</section>
<script>
document.querySelectorAll('button.tab').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('button.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('section[id^=tab-]').forEach(s => s.hidden = true);
  document.getElementById('tab-' + b.dataset.t).hidden = false;
}}));
</script>
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
