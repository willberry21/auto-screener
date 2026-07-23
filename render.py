#!/usr/bin/env python3
"""Renders the Trend Trader website from data.json (called by engine.py)."""
import datetime as dt
import html
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent
(_ROOT / "site").mkdir(parents=True, exist_ok=True)
SITE = _ROOT / "site" / "index.html"
SHARE = _ROOT / "site" / "share.html"


def pct(x, dash="—"):
    return dash if x is None else f"{x*100:+.1f}%"


def rate(x, dash="—"):
    """Percentage with no +/- sign, for win rate and similar."""
    return dash if x is None else f"{x*100:.0f}%"


def money(x):
    return "—" if x is None else f"${x:,.0f}"


def spark_path(curve, w, h, pad=2):
    """Return an SVG polyline points string for the equity curve."""
    if len(curve) < 2:
        return "", 0, 0
    vals = [p["equity"] for p in curve]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts), lo, hi


def render_site(store):
    sim = store["sim"]
    st = store["stats"]
    cfg = store["config"]
    consensus = set(store.get("consensus_tickers", []))
    bench = store.get("spy_benchmark")
    gen = dt.datetime.fromisoformat(store["generated"]).strftime(
        "%A, %B %-d %Y at %-I:%M %p ET")

    # ---- verdict banner
    tr = st["total_ret"]
    beat = (bench is not None and tr is not None and tr > bench)
    if st["n"] < 20:
        verdict, vclass = f"Early — {st['n']} closed trades so far", "neutral"
    elif tr is not None and tr > 0 and beat:
        verdict, vclass = "Beating buy-and-hold so far", "good"
    elif tr is not None and tr > 0:
        verdict, vclass = "Positive, but trailing the S&P", "neutral"
    else:
        verdict, vclass = "Underwater so far", "bad"

    def stat(label, value, sub=""):
        return (f'<div class="stat"><div class="stat-val">{value}</div>'
                f'<div class="stat-lbl">{label}</div>'
                f'<div class="stat-sub">{sub}</div></div>')

    stats_html = "".join([
        stat("Paper equity", money(st["total_equity"]),
             f"from {money(cfg['starting_cash'])} start"),
        stat("Total return", pct(tr),
             f"S&amp;P {pct(bench)} same window" if bench is not None else ""),
        stat("Win rate", rate(st["win_rate"]),
             f"{st['n']} closed trades · trend-style"),
        stat("Profit factor", f"{st['profit_factor']:.2f}" if st["profit_factor"] else "—",
             "gross win ÷ gross loss"),
        stat("Avg win / loss", f"{money(st['avg_win'])} / {money(st['avg_loss'])}",
             f"avg hold {st['avg_hold']} days"),
        stat("Max drawdown", pct(st["max_dd"]), "worst equity dip"),
    ])

    # ---- equity curve svg
    curve = sim["equity_curve"]
    W, H = 1040, 220
    pts, lo, hi = spark_path(curve, W, H)
    start_cash = cfg["starting_cash"]
    # baseline (starting cash) line position
    base_y = ""
    if curve and hi != lo:
        vals = [p["equity"] for p in curve]
        lo2, hi2 = min(vals), max(vals)
        by = 2 + (H - 4) * (1 - (start_cash - lo2) / ((hi2 - lo2) or 1))
        base_y = f'<line x1="0" y1="{by:.1f}" x2="{W}" y2="{by:.1f}" class="baseline"/>'
    curve_svg = (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="equity">'
                 f'{base_y}<polyline points="{pts}" class="eqline"/></svg>'
                 if pts else '<div class="empty">Equity curve appears once trades begin.</div>')
    first_d = curve[0]["d"] if curve else ""
    last_d = curve[-1]["d"] if curve else ""

    # ---- open positions
    open_rows = []
    for o in sorted(sim["open"], key=lambda x: x["open_pct"], reverse=True):
        cons = ' <span class="pill cons">ProTicker ✓</span>' if o["ticker"] in consensus else ""
        cls = "pos" if o["open_pct"] >= 0 else "neg"
        open_rows.append(f"""<tr>
          <td class="tk">{html.escape(o['ticker'])}{cons}</td>
          <td>{o['entry_date']}</td>
          <td class="num">{o['entry']:g}</td>
          <td class="num">{o['current']:g}</td>
          <td class="num">{o['stop']:g}</td>
          <td class="num">{o['target']:g}</td>
          <td class="num {cls}">{pct(o['open_pct'])}</td>
          <td class="reason">{html.escape(o['reason'])}</td>
        </tr>""")
    open_tbl = ("".join(open_rows) or
                '<tr><td colspan="8" class="dimc">No open positions '
                '(cash, or market is risk-off).</td></tr>')

    # ---- closed trades (most recent first)
    closed_rows = []
    for t in sorted(sim["closed"], key=lambda x: x["exit_date"], reverse=True):
        cls = "pos" if t["pnl"] >= 0 else "neg"
        badge = (f'<span class="pill win">WIN</span>' if t["result"] == "WIN"
                 else '<span class="pill loss">LOSS</span>')
        closed_rows.append(f"""<tr>
          <td class="tk">{html.escape(t['ticker'])}</td>
          <td>{t['entry_date']} → {t['exit_date']}</td>
          <td class="num">{t['entry']:g} → {t['exit']:g}</td>
          <td class="num {cls}">{pct(t['pct'])}</td>
          <td class="num {cls}">{'+' if t['pnl']>=0 else ''}{t['pnl']:,.0f}</td>
          <td>{html.escape(t['exit_reason'])}</td>
          <td>{badge}</td>
        </tr>""")
    closed_tbl = ("".join(closed_rows) or
                  '<tr><td colspan="7" class="dimc">No closed trades yet.</td></tr>')

    # ---- today's live watchlist (all setups firing on the latest bar)
    STATE_CLS = {"holding": "flat", "candidate": "win",
                 "watch (max positions)": "live", "market risk-off": "loss"}
    watch_rows = []
    for w in sim.get("watchlist", []):
        cons = ' <span class="pill cons">ProTicker ✓</span>' if w["ticker"] in consensus else ""
        pill = STATE_CLS.get(w["state"], "flat")
        watch_rows.append(f"""<tr>
          <td class="tk">{html.escape(w['ticker'])}{cons}</td>
          <td><span class="pill {pill}">{html.escape(w['state'])}</span></td>
          <td class="num">{w['entry']:g}</td>
          <td class="num">{w['stop']:g}</td>
          <td class="num">{w['target']:g}</td>
          <td class="num pos">{pct(w['rs'])}</td>
          <td class="reason">{html.escape(w['reason'])}</td>
        </tr>""")
    watch_tbl = ("".join(watch_rows) or
                 '<tr><td colspan="7" class="dimc">No setups firing today.</td></tr>')

    # ---- performance by setup type
    setup_rows = []
    for s in st.get("by_setup", []):
        cls = "pos" if s["pnl"] >= 0 else "neg"
        setup_rows.append(f"""<tr>
          <td class="tk">{html.escape(s['setup'])}</td>
          <td class="num">{s['n']}</td>
          <td class="num">{rate(s['win_rate'])}</td>
          <td class="num {cls}">{pct(s['avg_ret'])}</td>
          <td class="num {cls}">{'+' if s['pnl']>=0 else ''}{s['pnl']:,.0f}</td>
        </tr>""")
    setup_tbl = ("".join(setup_rows) or
                 '<tr><td colspan="5" class="dimc">No closed trades yet.</td></tr>')

    # ---- current regime
    reg = sim["regime_log"][-1] if sim["regime_log"] else None
    reg_note = reg["note"] if reg else "unknown"
    reg_on = reg["risk_on"] if reg else False
    reg_cls = "good" if reg_on else "bad"
    vix = reg["vix"] if reg else None
    vix_txt = f"VIX {vix:.1f}" if vix else ""

    doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Trend Trader — Paper Track Record</title>
<style>
:root {{ color-scheme: light dark;
  --bg:#0b0e14; --card:#151a24; --line:#232b3a; --txt:#e6edf3; --dim:#8b98a9;
  --accent:#4da3ff; --good:#3fb950; --bad:#f85149; --flat:#8b98a9; }}
@media (prefers-color-scheme:light) {{ :root {{
  --bg:#f4f6fa; --card:#fff; --line:#e2e8f0; --txt:#1a2230; --dim:#5b6675;
  --accent:#1f6feb; --good:#1a7f37; --bad:#cf222e; --flat:#8b98a9; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--txt);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:28px 20px 60px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.02em; }}
.sub {{ color:var(--dim); margin:0 0 20px; font-size:13.5px; }}
.badges {{ margin:0 0 22px; display:flex; gap:8px; flex-wrap:wrap; }}
.verdict, .regime {{ display:inline-block; padding:6px 14px; border-radius:999px;
  font-weight:600; font-size:13px; }}
.verdict.good, .regime.good {{ background:color-mix(in srgb,var(--good) 18%,transparent); color:var(--good); }}
.verdict.bad, .regime.bad {{ background:color-mix(in srgb,var(--bad) 18%,transparent); color:var(--bad); }}
.verdict.neutral {{ background:color-mix(in srgb,var(--accent) 16%,transparent); color:var(--accent); }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:26px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }}
.stat-val {{ font-size:24px; font-weight:700; letter-spacing:-.02em; }}
.stat-lbl {{ font-size:12.5px; color:var(--dim); margin-top:2px; }}
.stat-sub {{ font-size:11.5px; color:var(--dim); margin-top:4px; opacity:.85; }}
h2 {{ font-size:16px; margin:30px 0 12px; }}
.eqwrap {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
.equity {{ width:100%; height:220px; display:block; }}
.eqline {{ fill:none; stroke:var(--accent); stroke-width:2; vector-effect:non-scaling-stroke; }}
.baseline {{ stroke:var(--dim); stroke-width:1; stroke-dasharray:4 4; opacity:.5; vector-effect:non-scaling-stroke; }}
.eqmeta {{ display:flex; justify-content:space-between; color:var(--dim); font-size:12px; margin-top:8px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:13.5px; }}
th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ font-size:11.5px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim); font-weight:600; }}
tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.tk {{ font-weight:700; }}
.reason {{ color:var(--dim); font-size:12.5px; white-space:normal; }}
.pos {{ color:var(--good); }} .neg {{ color:var(--bad); }}
.dimc {{ color:var(--dim); text-align:center; padding:18px; }}
.pill {{ padding:2px 9px; border-radius:999px; font-size:11px; font-weight:700; }}
.pill.win {{ background:color-mix(in srgb,var(--good) 20%,transparent); color:var(--good); }}
.pill.loss {{ background:color-mix(in srgb,var(--bad) 20%,transparent); color:var(--bad); }}
.pill.cons {{ background:color-mix(in srgb,var(--accent) 20%,transparent); color:var(--accent); font-size:10px; }}
.scroll {{ overflow-x:auto; }}
.foot {{ margin-top:34px; color:var(--dim); font-size:12px; line-height:1.7; }}
.foot b {{ color:var(--txt); }}
</style></head><body><div class="wrap">
<h1>Trend Trader</h1>
<p class="sub">Rules-based paper trading on real market data · no money at risk · updated {gen}</p>
<div class="badges">
  <span class="verdict {vclass}">{verdict}</span>
  <span class="regime {reg_cls}">Market: {html.escape(reg_note)} {vix_txt}</span>
</div>
<div class="stats">{stats_html}</div>

<h2>Paper equity — {money(start_cash)} start</h2>
<div class="eqwrap">
  {curve_svg}
  <div class="eqmeta"><span>{first_d}</span><span>dashed line = starting cash</span><span>{last_d}</span></div>
</div>

<h2>Today's watchlist — {len(sim.get('watchlist', []))} setups firing</h2>
<div class="scroll"><table>
<thead><tr><th>Ticker</th><th>Status</th><th class="num">Would enter</th><th class="num">Stop</th>
<th class="num">Target</th><th class="num">vs S&amp;P (20d)</th><th>Setup</th></tr></thead>
<tbody>{watch_tbl}</tbody></table></div>

<h2>Open positions ({st['open_count']})</h2>
<div class="scroll"><table>
<thead><tr><th>Ticker</th><th>Entered</th><th class="num">Entry</th><th class="num">Now</th>
<th class="num">Stop</th><th class="num">Target</th><th class="num">Open P/L</th><th>Setup</th></tr></thead>
<tbody>{open_tbl}</tbody></table></div>

<h2>Which edge is working — by setup type</h2>
<div class="scroll"><table>
<thead><tr><th>Setup</th><th class="num">Trades</th><th class="num">Win rate</th>
<th class="num">Avg return</th><th class="num">Total P/L $</th></tr></thead>
<tbody>{setup_tbl}</tbody></table></div>

<h2>Closed trades ({st['n']})</h2>
<div class="scroll"><table>
<thead><tr><th>Ticker</th><th>Held</th><th class="num">Entry → Exit</th><th class="num">%</th>
<th class="num">P/L $</th><th>Why closed</th><th>Result</th></tr></thead>
<tbody>{closed_tbl}</tbody></table></div>

<div class="foot">
<b>What this is.</b> A disciplined trend-following strategy, coded as fixed rules and run on real daily
price data from Yahoo Finance across {cfg['universe_size']} large-cap stocks. It back-tests from
{store['backtest_start']} to today, then keeps trading forward each day. <b>No real money is involved</b>
— it is a track record to see whether the rules actually work before anyone would ever risk a cent.<br>
<b>The rules.</b> Go long only when the S&amp;P is above its 200-day average and the VIX is calm
(macro filter). Buy stocks in a confirmed uptrend (price &gt; 50-day &gt; 200-day) that are stronger
than the market, on either a 20-day breakout with volume or a pullback bounce to a rising 20-day average.
Risk {cfg['risk_per_trade']*100:.0f}% of equity per trade, stop {cfg['atr_stop_mult']:g}×ATR below entry,
target {cfg['reward_mult']:g}× the risk, max {cfg['max_positions']} positions, exit on stop, target,
trend break, or {40}-day time stop. When a same-day bar could have hit both stop and target, the stop is
assumed (honest, not optimistic). <b>ProTicker ✓</b> marks names your ProTicker tracker also flagged in
the last 30 days.<br>
<b>Not investment advice.</b> Past simulated performance says nothing about the future.
</div>
</div></body></html>"""
    SITE.write_text(doc)
    # also emit a body-only fragment (style + content, no <head>/<body> wrapper)
    # so it can be published as a hosted page to share with others
    style = doc[doc.index("<style>"):doc.index("</style>") + len("</style>")]
    body = doc[doc.index('<div class="wrap">'):doc.index("</body>")]
    SHARE.write_text(style + body)
