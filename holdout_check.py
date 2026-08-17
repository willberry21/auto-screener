#!/usr/bin/env python3
"""Out-of-sample test: does the +5%/-15% exit rule STILL work on catches the
rule never saw? The rule was chosen on 2026-08-17 from catches up to that date.
The only honest test is how it does on catches logged AFTER that cutoff.

Run any time: `python holdout_check.py`  (no args, no keys — reads the live site)
"""
import json
import urllib.request

CUTOFF = "2026-08-17"          # the rules/setups were found on/before this date
TP, SL = 0.05, 0.15           # exit rule we're validating
GG_TP, GG_SL = 0.10, 0.08     # rule used to test the gap-and-go SETUP
LIVE = "https://willberry21.github.io/auto-screener/data.json"
PDB = "https://willberry21.github.io/auto-screener/pattern_db.json"


def replay(path, at_close, tp, sl, cost=0.0):
    out = at_close
    for fav, adv in path:
        if fav >= tp and adv <= -sl:
            out = -sl                  # both in one bar -> assume the stop (honest)
            break
        if fav >= tp:
            out = tp
            break
        if adv <= -sl:
            out = -sl
            break
    return out - cost                  # net of round-trip spread + slippage


def stats(rows):
    if not rows:
        return None
    n = len(rows)
    wins = sum(1 for r in rows if r > 0)
    avg = sum(rows) / n
    trimmed = sorted(rows)[:-3] if n > 6 else rows
    return {"n": n, "win_rate": 100 * wins / n, "avg": 100 * avg,
            "avg_trimmed": 100 * sum(trimmed) / len(trimmed)}


def main():
    req = urllib.request.Request(LIVE, headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    catches = list(d.get("detections", {}).values()) + list(d.get("inplay", {}).values())
    scored = [c for c in catches
              if c.get("score", {}).get("status") == "ok" and c["score"].get("path")]

    in_sample, out_sample = [], []
    for c in scored:
        r = replay(c["score"]["path"], c["score"]["at_close"], TP, SL,
                   c["score"].get("cost", 0))
        (out_sample if c["date"] > CUTOFF else in_sample).append(r)

    print(f"=== +{int(TP*100)}%/-{int(SL*100)}% exit rule — holdout check ===")
    print(f"cutoff (found on/before): {CUTOFF}")
    ins, outs = stats(in_sample), stats(out_sample)
    if ins:
        print(f"IN-SAMPLE  (fit)   n={ins['n']:3d}  win {ins['win_rate']:.0f}%  "
              f"avg {ins['avg']:+.1f}%  trimmed {ins['avg_trimmed']:+.1f}%")
    if outs:
        print(f"OUT-SAMPLE (NEW!)  n={outs['n']:3d}  win {outs['win_rate']:.0f}%  "
              f"avg {outs['avg']:+.1f}%  trimmed {outs['avg_trimmed']:+.1f}%")
        verdict = ("HOLDS — still profitable on unseen catches"
                   if outs["avg_trimmed"] > 0 and outs["n"] >= 15
                   else ("FADED — the edge did not survive out of sample"
                         if outs["n"] >= 15 else
                         f"TOO EARLY — only {outs['n']} new catches, need ~15+"))
        print(f"VERDICT: {verdict}")
    else:
        print("OUT-SAMPLE: no catches logged after the cutoff yet.")

    # --- the gap-and-go SETUP: does it beat costs out of sample? ------------
    print(f"\n=== gap-and-go setup (rule +{int(GG_TP*100)}%/-{int(GG_SL*100)}%, net of costs) ===")
    try:
        labels = {f"{x['date']}|{x['ticker']}": x.get("label")
                  for x in json.load(urllib.request.urlopen(
                      urllib.request.Request(PDB, headers={"User-Agent": "Mozilla/5.0"}),
                      timeout=30))["days"]}
    except Exception:
        labels = {}
    gg_in, gg_out = [], []
    for c in scored:
        key = f"{c['date']}|{c['ticker']}"
        if labels.get(key) == "Gap and go":
            r = replay(c["score"]["path"], c["score"]["at_close"],
                       GG_TP, GG_SL, c["score"].get("cost", 0))
            (gg_out if c["date"] > CUTOFF else gg_in).append(r)
    gi, go = stats(gg_in), stats(gg_out)
    if not labels:
        print("  (pattern_db not reachable — can't label gap-and-go right now)")
    if gi:
        print(f"IN-SAMPLE  (fit)   n={gi['n']:3d}  win {gi['win_rate']:.0f}%  avg {gi['avg']:+.1f}%")
    if go:
        print(f"OUT-SAMPLE (NEW!)  n={go['n']:3d}  win {go['win_rate']:.0f}%  avg {go['avg']:+.1f}%")
        gv = ("HOLDS — gap-and-go still beats costs on unseen catches"
              if go["avg"] > 0 and go["n"] >= 10
              else ("FADED — gap-and-go did not beat costs out of sample"
                    if go["n"] >= 10 else
                    f"TOO EARLY — only {go['n']} new gap-and-go catches, need ~10+"))
        print(f"VERDICT: {gv}")
    elif labels:
        print("OUT-SAMPLE: no new gap-and-go catches logged after the cutoff yet.")


if __name__ == "__main__":
    main()
