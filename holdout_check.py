#!/usr/bin/env python3
"""Out-of-sample test: does the +5%/-15% exit rule STILL work on catches the
rule never saw? The rule was chosen on 2026-08-17 from catches up to that date.
The only honest test is how it does on catches logged AFTER that cutoff.

Run any time: `python holdout_check.py`  (no args, no keys — reads the live site)
"""
import json
import urllib.request

CUTOFF = "2026-08-17"          # the rule was fit on/before this date
TP, SL = 0.05, 0.15           # the rule we're validating
LIVE = "https://willberry21.github.io/auto-screener/data.json"


def replay(path, at_close, tp, sl):
    for fav, adv in path:
        if fav >= tp and adv <= -sl:
            return -sl                 # both in one bar -> assume the stop (honest)
        if fav >= tp:
            return tp
        if adv <= -sl:
            return -sl
    return at_close


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
        r = replay(c["score"]["path"], c["score"]["at_close"], TP, SL)
        (out_sample if c["date"] > CUTOFF else in_sample).append(r)

    print(f"=== +{int(TP*100)}%/-{int(SL*100)}% exit rule — holdout check ===")
    print(f"cutoff (rule fit on/before): {CUTOFF}")
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


if __name__ == "__main__":
    main()
