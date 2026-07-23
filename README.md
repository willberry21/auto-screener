# Auto Screener

A self-contained, **fully autonomous** stock dashboard. It runs on GitHub's
servers on a schedule — no Mac, no app open, nothing to keep awake — fetches
real market data, runs a rules-based strategy, and publishes the result to a
web page. Built to eventually replace depending on the Pro Ticker app.

## How it runs (the whole point)
A GitHub Actions job (`.github/workflows/screener.yml`) fires **every weekday at
21:00 UTC** (~2pm PT / 5pm ET, after the close) — and can be run by hand anytime
from the **Actions** tab. Each run:

1. `python engine.py` — fetches Yahoo daily data, simulates the strategy, builds `site/index.html`.
2. Deploys `site/` straight to **GitHub Pages**.

No state is stored between runs: the engine re-simulates the entire record from
scratch each time from real historical data, so it's identical whether it runs on
your laptop or a cloud server. That's what makes it hands-off.

## The strategy (the "brain" — a placeholder for now)
Right now it's the **Trend Trader** engine: go long strong stocks in a strong
market (S&P above its 200-day, calm VIX), enter on breakouts/pullbacks, size by
risk, use ATR stops and 2R targets. **No real money — a measurement tool.** This
is a stand-in so the autonomous pipeline is live; the signal logic is meant to be
evolved later.

## One-time setup on GitHub
1. Push this folder to a new GitHub repo.
2. **Settings → Pages → Source → "GitHub Actions".**
3. **Actions** tab → **Auto Screener** → **Run workflow** to test it now.
4. The dashboard appears at your Pages URL.

## Run locally (optional)
    python3 engine.py      # writes site/index.html

Pure Python standard library + Yahoo Finance. No API keys, no dependencies.
