# Runner Scanner

A self-contained, **fully autonomous** stock scanner. It runs on GitHub's
servers on a schedule — no Mac, no app open, nothing to keep awake — scans every
NASDAQ/NYSE stock for low-float runners, scores every catch honestly after the
close, and publishes the result to a web page.

**Observation only. It does not trade, and none of it is investment advice.**

Live at <https://willberry21.github.io/auto-screener/>

## How it runs (the whole point)
A GitHub Actions job (`.github/workflows/scanner.yml`) fires **every 15 minutes,
8:00–21:00 UTC on weekdays** (~4:00am–5:45pm ET, pre-market included), plus one
evening pass at 22:15 UTC that scores the day's catches. It can also be run by
hand from the **Actions** tab. Each run:

1. `python scanner.py` — scans the market, scores yesterday's catches, writes
   `site/index.html` and `site/data.json`.
2. `python pattern_lab.py` — rebuilds the chart-pattern catalog into
   `site/patterns.html` and refreshes `pattern_db.json`.
3. Deploys `site/` straight to **GitHub Pages**.

The script checks Eastern Time itself and skips detection outside 4:00am–4:10pm
ET, so the UTC cron can stay dumb across daylight-saving changes.

## What it looks for
Cheap, **low-float** stocks (60 million shares or fewer) the moment they move
20%+ — the setups that run before anyone posts about them. Each catch is
cross-checked against SEC EDGAR for dilution filings, the red flag that says the
company is about to sell new shares into your spike.

Scoring is automatic and unskippable. After the close, every catch is replayed as
if bought at the first price after detection and sold at +25% or cut at −10%,
alongside the held-to-close result and the perfect-exit best case. Round-trip
costs (spread + slippage) are subtracted. **Wins and losses both stay on the
board forever** — there is no mechanism to quietly drop a bad catch.

## Files
| File | What it does |
|---|---|
| `scanner.py` | The live scanner. Detection, scoring, and the main page. |
| `pattern_lab.py` | Builds the chart-pattern catalog from the pattern database. |
| `pattern_db.json` | Permanent record of every charted day. Bootstraps the cloud run. |
| `holdout_check.py` | Manual tool: out-of-sample test of the exit rule and setup. |
| `engine.py`, `render.py` | **Retired.** The Phase 1 Trend Trader brain, kept for reference only. Nothing runs them. |

`site/` and `data.json` are generated artifacts — gitignored, rebuilt from
scratch on every cloud run.

## Run locally (optional)
    python3 scanner.py        # scans and writes site/index.html
    python3 pattern_lab.py    # rebuilds site/patterns.html
    python3 holdout_check.py  # out-of-sample check, prints to stdout

Pure Python standard library + Yahoo Finance and SEC EDGAR. No API keys, no
dependencies.

## History
Phase 1 was the **Trend Trader** engine (`engine.py` + `render.py`, driven by the
retired `screener.yml` workflow) — a rules-based daily-bar simulator that served
as a placeholder brain while the autonomous pipeline was built. It was retired
2026-08-12 when the Runner Scanner took over the site, along with the ProTicker
signal tracker that preceded both. Those files are left in place as a record of
how Phase 1 worked; delete them freely if they ever get in the way.
