#!/usr/bin/env python3
"""Trend Trader - an autonomous, rules-based paper-trading track record.

The idea (William's): take the day-trader knowledge encoded in Pattern Trader
(chart patterns) and ProTicker (signals), condense it into a fixed rule set,
and just TRACK TRENDS with real market data - no real money. Trade the way a
disciplined trend-following trader would: only go long strong stocks in strong
markets, size by risk, use stops and targets, and stand aside when the macro
backdrop turns hostile.

Pipeline (mirrors ~/proticker-tracker):
  Yahoo daily bars  ->  indicators + signals  ->  paper-trade simulation
  ->  data.json  ->  self-contained website at ~/trend-trader/site/index.html

It back-simulates from BACKTEST_START on real historical data so there is an
immediate multi-month track record, then continues forward automatically each
day. No connection to any broker; no orders; no money. A measurement tool.

Run manually:   python3 ~/trend-trader/engine.py
Scheduled:      launchd job com.williamberry.trend-trader (once/day after close)
"""
import datetime as dt
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROJECT = Path(__file__).resolve().parent      # repo root — works locally AND in the cloud
DATA = PROJECT / "data.json"
BARS_CACHE = PROJECT / "bars_cache.json"
SITE = PROJECT / "site" / "index.html"
LOGF = PROJECT / "trend-trader.log"
PROTICKER_DATA = PROJECT / "proticker-data.json"   # optional; simply absent in the cloud

# ---- knobs ------------------------------------------------------------------
BACKTEST_START = dt.date(2026, 1, 2)   # where the tracked record begins
STARTING_CASH = 10_000.0
RISK_PER_TRADE = 0.01                  # risk 1% of equity per position
ATR_STOP_MULT = 2.0                    # initial stop = entry - 2*ATR
REWARD_MULT = 2.0                      # target = entry + (2 * stop distance)  -> 2R
MAX_POSITIONS = 8
MAX_HOLD_DAYS = 40                     # time stop
VIX_RISK_OFF = 28.0                    # macro: stand down above this
FETCH_START = dt.date(2024, 6, 1)      # enough history for 200-day MA warm-up

# Liquid, widely-followed names across sectors + market-context symbols.
UNIVERSE = [
    # mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "AMD", "ADBE", "CRM",
    "ORCL", "CSCO", "INTC", "QCOM", "TXN", "MU", "AMAT", "NOW", "INTU", "PANW",
    # consumer / retail
    "TSLA", "HD", "NKE", "MCD", "SBUX", "COST", "WMT", "TGT", "LOW", "DIS",
    "NFLX", "CMG", "LULU",
    # financials
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BLK", "SCHW",
    # health care
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ISRG", "AMGN",
    # industrials / energy / materials
    "CAT", "DE", "BA", "GE", "HON", "UPS", "XOM", "CVX", "COP", "FCX", "LIN",
    # comms / misc growth
    "UBER", "SHOP", "PYPL", "XYZ", "COIN", "PLTR", "SNOW", "ABNB", "MARA",
    # more software / semis
    "CRWD", "DDOG", "NET", "ZS", "MDB", "SMCI", "ARM", "DELL", "ANET", "WDAY", "HPQ",
    # more consumer
    "BKNG", "MAR", "RCL", "F", "GM", "KO", "PEP", "PG", "MDLZ",
    # more financials
    "C", "USB", "PNC", "SPGI", "ICE", "CME", "PGR",
    # more health care
    "GILD", "VRTX", "REGN", "CVS", "MDT", "BMY",
    # more industrials / energy / utilities
    "LMT", "RTX", "NOC", "MMM", "SLB", "EOG", "MPC", "PSX", "NEE", "DUK", "SO",
    # more materials / comms
    "NEM", "APD", "SHW", "NUE", "T", "VZ", "CMCSA", "TMUS",
]
CONTEXT = ["SPY", "QQQ", "^VIX"]


def log(msg):
    stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {msg}"
    print(line)
    with LOGF.open("a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------- price data
def yahoo(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(4)


def daily_bars(symbol):
    """Return list of {d(iso), o,h,l,c,v} daily bars from FETCH_START to today."""
    p1 = int(dt.datetime.combine(FETCH_START, dt.time.min, tzinfo=ET).timestamp())
    p2 = int(dt.datetime.now(ET).timestamp()) + 86400
    sym = urllib.parse.quote(symbol)
    data = yahoo(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                 f"?period1={p1}&period2={p2}&interval=1d")
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    o, h, l, c, v = (q.get(k, []) for k in ("open", "high", "low", "close", "volume"))
    bars = []
    for i, t in enumerate(ts):
        if i >= len(c) or c[i] is None or h[i] is None or l[i] is None:
            continue
        day = dt.datetime.fromtimestamp(t, ET).date()
        bars.append({
            "d": day.isoformat(),
            "o": float(o[i]) if i < len(o) and o[i] is not None else float(c[i]),
            "h": float(h[i]), "l": float(l[i]), "c": float(c[i]),
            "v": float(v[i]) if i < len(v) and v[i] is not None else 0.0,
        })
    return bars


FULL_SWEEP_MINUTES = 20   # how often to refresh the WHOLE universe intraday


def _prior_holdings():
    """Tickers we currently hold, from the last run's data.json (so the fast
    per-minute path knows which names' live prices actually matter)."""
    if not DATA.exists():
        return set()
    try:
        store = json.loads(DATA.read_text())
        return {o["ticker"] for o in store.get("sim", {}).get("open", [])}
    except Exception:
        return set()


def load_all_bars():
    """Fetch (or reuse cached) daily bars for the universe + context.

    Tiered so a 1-minute schedule is possible without hammering Yahoo:
      * Intraday, EVERY run refetches only the fast set - the market context
        (SPY/QQQ/VIX) plus the names we currently hold - since those are what
        move live equity and can hit a stop/target.
      * The FULL universe is refreshed at most every FULL_SWEEP_MINUTES (for the
        watchlist / new-entry scan) and always in the post-close finalize window.
      * Overnight, everything is served from cache until the next day's bar is due.
    """
    cache = {}
    if BARS_CACHE.exists():
        try:
            cache = json.loads(BARS_CACHE.read_text())
        except Exception:
            cache = {}
    meta = cache.get("_meta", {})
    now = dt.datetime.now(ET)
    today = now.date().isoformat()
    symbols = UNIVERSE + CONTEXT

    market = now.weekday() < 5 and dt.time(9, 30) <= now.time() <= dt.time(16, 0)
    finalize = now.weekday() < 5 and dt.time(16, 0) < now.time() <= dt.time(16, 45)
    live = market or finalize

    # is a full-universe sweep due?
    mins_since_full = 1e9
    if meta.get("last_full"):
        try:
            mins_since_full = (now - dt.datetime.fromisoformat(meta["last_full"])).total_seconds() / 60
        except Exception:
            pass
    do_full = finalize or mins_since_full >= FULL_SWEEP_MINUTES
    fast_set = set(CONTEXT) | _prior_holdings()

    out = {}
    fetched = 0
    for sym in symbols:
        have = cache.get(sym)
        have_bars = have.get("bars") if have else None
        need_latest = not (have_bars and have_bars[-1]["d"] >= _last_expected_trading_day())
        if live:
            refetch = do_full or sym in fast_set or need_latest
        else:
            refetch = need_latest  # overnight: only backfill a missing day
        if not refetch and have_bars:
            out[sym] = have_bars
            continue
        try:
            bars = daily_bars(sym)
            out[sym] = bars
            cache[sym] = {"fetched": today, "bars": bars}
            fetched += 1
            time.sleep(0.3)
        except Exception as e:
            log(f"  fetch failed {sym}: {e}")
            if have_bars:
                out[sym] = have_bars

    if live and do_full:
        meta["last_full"] = now.isoformat()
    cache["_meta"] = meta
    BARS_CACHE.write_text(json.dumps(cache))
    tier = "full sweep" if (live and do_full) else ("fast set" if live else "cache/backfill")
    log(f"Loaded bars for {len(out)} symbols ({fetched} fetched, {tier}).")
    return out


def market_open_now():
    now = dt.datetime.now(ET)
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() <= dt.time(16, 0)


def _last_expected_trading_day():
    """Most recent weekday (rough 'is my cache current?' check)."""
    now = dt.datetime.now(ET)
    d = now.date()
    # before the close, yesterday's bar is the latest finalized one
    if now.time() < dt.time(16, 5):
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


# ----------------------------------------------------------------- indicators
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - n, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def macd_hist(closes):
    if len(closes) < 35:
        return None
    macd_line = []
    for i in range(26, len(closes) + 1):
        seg = closes[:i]
        macd_line.append(ema(seg, 12) - ema(seg, 26))
    if len(macd_line) < 9:
        return None
    signal = ema(macd_line, 9)
    return macd_line[-1] - signal


# ----------------------------------------------------------------- simulation
class Position:
    def __init__(self, ticker, entry_date, entry, shares, stop, target, reason):
        self.ticker = ticker
        self.entry_date = entry_date
        self.entry = entry
        self.shares = shares
        self.stop = stop
        self.target = target
        self.reason = reason
        self.days_held = 0

    def value(self, price):
        return self.shares * price


def bars_upto(bars, date_iso):
    return [b for b in bars if b["d"] <= date_iso]


def regime(spy_bars, vix_bars, date_iso):
    """Macro filter. Returns (risk_on: bool, note, spy_above_200, vix_level)."""
    spy = bars_upto(spy_bars, date_iso)
    vix = bars_upto(vix_bars, date_iso)
    if len(spy) < 200:
        return False, "warming up", None, None
    closes = [b["c"] for b in spy]
    spy_200 = sma(closes, 200)
    spy_50 = sma(closes, 50)
    price = closes[-1]
    vix_level = vix[-1]["c"] if vix else None
    above = price > spy_200
    trend_ok = spy_50 is not None and spy_50 > spy_200
    vix_ok = vix_level is None or vix_level < VIX_RISK_OFF
    risk_on = above and vix_ok
    if not above:
        note = "S&P below its 200-day average - defense"
    elif not vix_ok:
        note = f"VIX elevated ({vix_level:.0f}) - standing down"
    elif not trend_ok:
        note = "choppy (50-day below 200-day) - selective"
    else:
        note = "uptrend intact - risk on"
    return risk_on, note, above, vix_level


def scan_entry(ticker, bars, spy_bars, date_iso):
    """Trader-style long setup. Returns dict(entry, stop, target, reason, rs) or None.

    Rules (all must hold), the Pattern-Trader knowledge as code:
      * Uptrend structure: close > 50-day MA > 200-day MA
      * Relative strength: 20-day return beats SPY's 20-day return
      * Momentum sane: RSI 50-78 (rising, not blown-off)
      * A trigger, either:
          BREAKOUT - close = highest close of the last 20 days on >1.3x avg volume
          PULLBACK - dipped to the rising 20-day MA and closed back above it
    """
    b = bars_upto(bars, date_iso)
    if len(b) < 205:
        return None
    closes = [x["c"] for x in b]
    vols = [x["v"] for x in b]
    price = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    if None in (s20, s50, s200):
        return None
    if not (price > s50 > s200):
        return None
    r = rsi(closes)
    if r is None or not (50 <= r <= 78):
        return None
    a = atr(b)
    if not a or a <= 0:
        return None
    # relative strength vs SPY over 20 sessions
    sp = bars_upto(spy_bars, date_iso)
    if len(sp) < 21:
        return None
    stock_ret = closes[-1] / closes[-21] - 1
    spy_ret = sp[-1]["c"] / sp[-21]["c"] - 1
    if stock_ret <= spy_ret:
        return None

    prior_high = max(closes[-21:-1])
    avg_vol = sma(vols, 20)
    vol_ok = avg_vol and vols[-1] >= 1.3 * avg_vol
    s20_prev = sma(closes[:-1], 20)
    s20_rising = s20_prev is not None and s20 > s20_prev

    reason = None
    if price >= prior_high and vol_ok and s20_rising:
        reason = "20-day breakout on volume"
    else:
        low = b[-1]["l"]
        touched = low <= s20 * 1.015
        if touched and price > s20 and s20_rising and r >= 52:
            reason = "pullback bounce off rising 20-day MA"
    if not reason:
        return None

    stop = round(price - ATR_STOP_MULT * a, 2)
    if stop >= price:
        return None
    target = round(price + REWARD_MULT * (price - stop), 2)
    return {"entry": round(price, 2), "stop": stop, "target": target,
            "reason": reason, "rs": stock_ret - spy_ret, "atr": round(a, 2)}


def simulate(all_bars):
    """Walk day by day from BACKTEST_START to today, trading the rules."""
    spy_bars = all_bars.get("SPY", [])
    vix_bars = all_bars.get("^VIX", [])
    # master calendar = SPY trading days within the tracked window
    calendar = [b["d"] for b in spy_bars if b["d"] >= BACKTEST_START.isoformat()]

    cash = STARTING_CASH
    positions = []          # open Position objects
    closed = []             # dicts
    equity_curve = []       # [{d, equity}]
    regime_log = []         # [{d, risk_on, note, vix}]

    def price_on(ticker, date_iso):
        b = bars_upto(all_bars.get(ticker, []), date_iso)
        return b[-1] if b else None

    for date_iso in calendar:
        # ---- 1) manage open positions against today's bar
        still_open = []
        for p in positions:
            bar = price_on(p.ticker, date_iso)
            if not bar or bar["d"] != date_iso:
                still_open.append(p)
                continue
            p.days_held += 1
            exit_price = exit_reason = None
            hit_stop = bar["l"] <= p.stop
            hit_target = bar["h"] >= p.target
            if hit_stop and hit_target:      # ambiguous day -> assume the stop (honest)
                exit_price, exit_reason = p.stop, "stop (same-day both)"
            elif hit_stop:
                exit_price, exit_reason = p.stop, "stop"
            elif hit_target:
                exit_price, exit_reason = p.target, "target"
            elif p.days_held >= MAX_HOLD_DAYS:
                exit_price, exit_reason = bar["c"], "time stop"
            else:
                # trend-break exit: close below 20-day MA
                b = bars_upto(all_bars[p.ticker], date_iso)
                s20 = sma([x["c"] for x in b], 20)
                if s20 and bar["c"] < s20:
                    exit_price, exit_reason = bar["c"], "trend break (below 20-day MA)"
            if exit_price is not None:
                pnl = (exit_price - p.entry) * p.shares
                cash += p.shares * exit_price
                closed.append({
                    "ticker": p.ticker, "entry_date": p.entry_date, "exit_date": date_iso,
                    "entry": p.entry, "exit": round(exit_price, 2), "shares": p.shares,
                    "stop": p.stop, "target": p.target, "reason": p.reason,
                    "exit_reason": exit_reason, "days": p.days_held,
                    "pnl": round(pnl, 2),
                    "pct": (exit_price - p.entry) / p.entry,
                    "result": "WIN" if pnl > 0 else "LOSS",
                })
            else:
                still_open.append(p)
        positions = still_open

        # ---- 2) macro regime
        risk_on, note, above200, vix = regime(spy_bars, vix_bars, date_iso)
        regime_log.append({"d": date_iso, "risk_on": risk_on, "note": note, "vix": vix})

        # ---- 3) look for new entries (only when risk-on and slots free)
        if risk_on and len(positions) < MAX_POSITIONS:
            held = {p.ticker for p in positions}
            candidates = []
            for tk in UNIVERSE:
                if tk in held:
                    continue
                setup = scan_entry(tk, all_bars.get(tk, []), spy_bars, date_iso)
                if setup:
                    candidates.append((tk, setup))
            candidates.sort(key=lambda x: x[1]["rs"], reverse=True)  # strongest first
            for tk, s in candidates:
                if len(positions) >= MAX_POSITIONS:
                    break
                equity = cash + sum(p.value(price_on(p.ticker, date_iso)["c"])
                                    for p in positions if price_on(p.ticker, date_iso))
                risk_dollars = equity * RISK_PER_TRADE
                per_share_risk = s["entry"] - s["stop"]
                if per_share_risk <= 0:
                    continue
                shares = math.floor(risk_dollars / per_share_risk)
                cost = shares * s["entry"]
                if shares < 1 or cost > cash:
                    # scale down to available cash if needed
                    shares = math.floor(cash / s["entry"])
                    cost = shares * s["entry"]
                    if shares < 1:
                        continue
                cash -= cost
                positions.append(Position(tk, date_iso, s["entry"], shares,
                                          s["stop"], s["target"], s["reason"]))

        # ---- 4) mark-to-market equity
        mkt = sum(p.value(price_on(p.ticker, date_iso)["c"])
                  for p in positions if price_on(p.ticker, date_iso))
        equity_curve.append({"d": date_iso, "equity": round(cash + mkt, 2)})

    # snapshot open positions at the latest date
    last = calendar[-1] if calendar else None
    open_out = []
    for p in positions:
        bar = price_on(p.ticker, last)
        cur = bar["c"] if bar else p.entry
        open_out.append({
            "ticker": p.ticker, "entry_date": p.entry_date, "entry": p.entry,
            "shares": p.shares, "stop": p.stop, "target": p.target,
            "reason": p.reason, "days": p.days_held, "current": round(cur, 2),
            "open_pnl": round((cur - p.entry) * p.shares, 2),
            "open_pct": (cur - p.entry) / p.entry,
        })
    # today's live watchlist: every setup firing on the latest bar, whether or
    # not the strategy could take it (position cap / risk-off). This is the raw
    # signal flow - the most data we can surface each day.
    watchlist = []
    if last:
        held = {p.ticker for p in positions}
        risk_on, _, _, _ = regime(spy_bars, vix_bars, last)
        for tk in UNIVERSE:
            s = scan_entry(tk, all_bars.get(tk, []), spy_bars, last)
            if not s:
                continue
            if tk in held:
                state = "holding"
            elif not risk_on:
                state = "market risk-off"
            elif len(open_out) >= MAX_POSITIONS:
                state = "watch (max positions)"
            else:
                state = "candidate"
            watchlist.append({"ticker": tk, "state": state, **s})
        watchlist.sort(key=lambda x: x["rs"], reverse=True)

    return {
        "cash": round(cash, 2),
        "closed": closed,
        "open": open_out,
        "watchlist": watchlist,
        "equity_curve": equity_curve,
        "regime_log": regime_log,
        "last_date": last,
    }


# ----------------------------------------------------------------- proticker
def load_proticker_consensus():
    """Tickers ProTicker also flagged recently (for a 'both agree' badge)."""
    if not PROTICKER_DATA.exists():
        return set()
    try:
        store = json.loads(PROTICKER_DATA.read_text())
    except Exception:
        return set()
    recent = set()
    cutoff = (dt.datetime.now(ET).date() - dt.timedelta(days=30)).isoformat()
    for s in store.get("signals", {}).values():
        if s.get("date", "") >= cutoff:
            recent.add(s.get("ticker"))
    return recent


# ----------------------------------------------------------------- stats
def compute_stats(sim):
    closed = sim["closed"]
    n = len(closed)
    wins = [t for t in closed if t["result"] == "WIN"]
    win_rate = len(wins) / n if n else None
    total_pnl = sum(t["pnl"] for t in closed)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in closed if t["result"] == "LOSS")
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    avg_win = (gross_win / len(wins)) if wins else 0
    losers = [t for t in closed if t["result"] == "LOSS"]
    avg_loss = (-gross_loss / len(losers)) if losers else 0
    avg_hold = (sum(t["days"] for t in closed) / n) if n else 0

    eq = sim["equity_curve"]
    start = STARTING_CASH
    end = eq[-1]["equity"] if eq else start
    open_val = sum(o["current"] * o["shares"] for o in sim["open"])
    total_equity = sim["cash"] + open_val
    total_ret = total_equity / start - 1

    # max drawdown on the equity curve
    peak = start
    max_dd = 0.0
    for pt in eq:
        peak = max(peak, pt["equity"])
        dd = (pt["equity"] - peak) / peak
        max_dd = min(max_dd, dd)

    # performance split by setup type (breakout vs pullback) - which edge works
    def bucket(reason):
        return "Breakout" if "breakout" in reason else "Pullback"
    by_setup = {}
    for t in closed:
        b = bucket(t["reason"])
        g = by_setup.setdefault(b, {"n": 0, "wins": 0, "pnl": 0.0, "ret_sum": 0.0})
        g["n"] += 1
        g["wins"] += 1 if t["result"] == "WIN" else 0
        g["pnl"] += t["pnl"]
        g["ret_sum"] += t["pct"]
    setup_stats = [{
        "setup": name, "n": g["n"],
        "win_rate": (g["wins"] / g["n"]) if g["n"] else None,
        "pnl": round(g["pnl"], 2),
        "avg_ret": (g["ret_sum"] / g["n"]) if g["n"] else None,
    } for name, g in sorted(by_setup.items())]

    # SPY buy-and-hold benchmark over the same window
    return {
        "n": n, "win_rate": win_rate, "total_pnl": round(total_pnl, 2),
        "profit_factor": profit_factor, "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2), "avg_hold": round(avg_hold, 1),
        "total_equity": round(total_equity, 2), "total_ret": total_ret,
        "max_dd": max_dd, "open_count": len(sim["open"]),
        "by_setup": setup_stats,
    }


def spy_benchmark(all_bars):
    spy = [b for b in all_bars.get("SPY", []) if b["d"] >= BACKTEST_START.isoformat()]
    if len(spy) < 2:
        return None
    return spy[-1]["c"] / spy[0]["c"] - 1


def main():
    all_bars = load_all_bars()
    if "SPY" not in all_bars:
        log("ERROR: no SPY data - cannot establish market regime.")
        return 1
    sim = simulate(all_bars)
    stats = compute_stats(sim)
    consensus = load_proticker_consensus()
    bench = spy_benchmark(all_bars)

    store = {
        "generated": dt.datetime.now(ET).isoformat(),
        "backtest_start": BACKTEST_START.isoformat(),
        "sim": sim, "stats": stats,
        "spy_benchmark": bench,
        "consensus_tickers": sorted(consensus),
        "config": {
            "starting_cash": STARTING_CASH, "risk_per_trade": RISK_PER_TRADE,
            "atr_stop_mult": ATR_STOP_MULT, "reward_mult": REWARD_MULT,
            "max_positions": MAX_POSITIONS, "universe_size": len(UNIVERSE),
        },
    }
    DATA.write_text(json.dumps(store, indent=2))
    log(f"Simulated {stats['n']} closed trades, {stats['open_count']} open; "
        f"equity ${stats['total_equity']:,.0f} "
        f"({stats['total_ret']*100:+.1f}%), SPY {bench*100:+.1f}%."
        if bench is not None else "Simulated (no benchmark).")

    from render import render_site
    render_site(store)
    log(f"Website rebuilt at {SITE}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
