"""Feed doctor — measure whether the LIVE feed can actually produce the backtest's trade rate.

Answers three questions the strategy log cannot:

  1. ARE ALL BOOKS ARRIVING?  You subscribe 2 streams per coin on ONE socket. If HL caps or drops some,
     you run on fewer books -- which starves BREADTH, and breadth is the BURST gate. A missing book is
     invisible in the strategy log: it just looks like a quiet market.

  2. IS `fresh_ms` EATING THE CANDIDATES?  The live detector drops any order whose book was older than
     detection.fresh_ms (100ms) at order start. **The research candidate selection (size_ladder.py) has NO
     freshness term** -- it filters on reach alone. So live is strictly more selective than the pool the
     $/day figures came from. If l2Book arrives slower than ~100ms per coin, most orders are rejected
     before they are ever scored, and the trade rate collapses for a reason no threshold explains.

  3. WHAT IS THE BOOK CADENCE?  Per-coin l2Book updates/sec and the trade-vs-book staleness distribution.

Read-only: subscribes to the same streams, scores nothing, sends nothing.

Usage:  .venv/bin/python tools/feed_doctor.py [seconds]      (default 120)
"""
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
import warnings
import numpy as np
warnings.filterwarnings('ignore', 'All-NaN')   # quiet books have no gaps yet

from src.config import load
from src.feed import HLFeed

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0


async def main():
    cfg, _ = load()
    uni = cfg["universe"]
    FRESH = cfg["detection"]["fresh_ms"]
    AGG_NS = int(cfg["detection"]["agg_gap_ms"] * 1e6)

    l2n = defaultdict(int); bbn = defaultdict(int); trn = defaultdict(int)
    last_l2_ts = {}                       # coin -> exchange ts of last TOUCH (bbo, else l2Book)
    last_only_l2 = {}                     # coin -> exchange ts of last l2Book, for the counterfactual
    gaps = defaultdict(list)              # coin -> l2 inter-arrival ms
    stale = defaultdict(list)             # coin -> trade-vs-book age ms (per ORDER, not per print)
    agg = {}                              # coin -> in-flight order (mirrors strategy.py aggregation)
    stale_l2 = []                         # counterfactual: age if we only had l2Book
    n_orders = 0; n_fresh = 0

    feed = HLFeed(cfg["endpoint"]["ws"], uni)
    t_end = time.time() + DUR
    print(f"listening {DUR:.0f}s on {len(uni)} coins (fresh_ms={FRESH}, agg_gap_ms={AGG_NS/1e6:.0f})...")
    async for kind, payload in feed.stream():
        if time.time() > t_end:
            feed.stop(); break
        if kind != "msg":
            continue
        ch = payload.get("channel")
        if ch == "bbo":
            d = payload["data"]; coin = d["coin"]; t = int(d["time"]) * 1_000_000
            bbn[coin] += 1
            if coin in last_l2_ts:
                gaps[coin].append((t - last_l2_ts[coin]) / 1e6)
            last_l2_ts[coin] = max(t, last_l2_ts.get(coin, -1))
        elif ch == "l2Book":
            d = payload["data"]; coin = d["coin"]; t = int(d["time"]) * 1_000_000
            l2n[coin] += 1
            last_only_l2[coin] = t
            last_l2_ts[coin] = max(t, last_l2_ts.get(coin, -1))
        elif ch == "trades":
            for tr in payload["data"]:
                coin = tr["coin"]; t = int(tr["time"]) * 1_000_000
                de = 1.0 if tr["side"] == "B" else -1.0
                trn[coin] += 1
                bk = last_l2_ts.get(coin)
                if bk is None:
                    continue
                a = agg.get(coin)                       # same order-aggregation rule as the detector
                if a is not None and a["dir"] == de and (t - a["last"]) <= AGG_NS:
                    a["last"] = t
                    continue
                if a is not None:
                    n_orders += 1
                    stale[coin].append(a["age_ms"]); stale_l2.append(a["age_l2_ms"])
                    if a["age_ms"] < FRESH:
                        n_fresh += 1
                bk2 = last_only_l2.get(coin)
                agg[coin] = dict(dir=de, last=t, age_ms=(t - bk) / 1e6,
                                 age_l2_ms=((t - bk2) / 1e6 if bk2 else float("nan")))

    seen = sorted(set(bbn) | set(l2n) | set(trn))
    missing = [c for c in uni if c not in bbn and c not in l2n]
    print(f"\n=== 1. COVERAGE ===")
    print(f"configured {len(uni)} | bbo seen {len(bbn)} | l2Book seen {len(l2n)} | trades seen {len(trn)}")
    if missing:
        print(f"*** NO DATA AT ALL FOR {len(missing)}: {', '.join(missing)}")
        print("    Those books contribute NOTHING to breadth. Subscriptions likely dropped/capped.")
    else:
        print("every configured book is delivering -- coverage OK")
    quiet = [c for c in uni if c not in bbn and c in l2n]
    if quiet:
        print(f"note: {len(quiet)} book(s) sent no bbo in this window ({', '.join(quiet)}) -- normally just a")
        print("      quiet touch, not a fault; l2Book still covers them. Re-run longer if unsure.")

    print(f"\n=== 2. FRESHNESS (the live-only filter) ===")
    allst = np.concatenate([np.array(v) for v in stale.values()]) if stale else np.array([])
    if len(allst):
        pas = 100 * np.mean(allst < FRESH)
        print(f"orders {n_orders:,} | book age at order start: median {np.median(allst):.0f}ms  "
              f"p90 {np.percentile(allst,90):.0f}ms")
        print(f"PASSING fresh_ms<{FRESH}: {pas:.1f}%  ({n_fresh:,} of {n_orders:,})")
        sl2 = np.array([x for x in stale_l2 if np.isfinite(x)])
        if len(sl2):
            print(f"  counterfactual, l2Book-only touch: median {np.median(sl2):.0f}ms, "
                  f"would pass {100*np.mean(sl2 < FRESH):.1f}%  <- what this was before bbo")
        if pas < 60:
            print(f"*** {100-pas:.0f}% of orders are DISCARDED before scoring. The research pool applies NO")
            print("    freshness filter, so live is far more selective than the backtest it is compared to.")
            print("    This alone can explain a low trade rate. Consider raising detection.fresh_ms toward")
            print("    the observed book cadence -- but see the caveat in AGENTS.md invariant #2 first.")
    else:
        print("no orders observed (window too short, or no trades)")

    print(f"\n=== 3. BOOK CADENCE (per coin) ===")
    print(f"{'coin':>9}{'bbo/s':>8}{'l2/s':>8}{'gap p50':>9}{'gap p90':>9}{'trades':>8}{'stale p50':>11}{'fresh%':>8}")
    for c in sorted(seen, key=lambda c: -bbn[c]):
        g = np.array(gaps[c]) if gaps[c] else np.array([np.nan])
        st = np.array(stale[c]) if stale[c] else np.array([np.nan])
        fr = 100 * np.mean(st < FRESH) if stale[c] else float("nan")
        print(f"{c:>9}{bbn[c]/DUR:>8.2f}{l2n[c]/DUR:>8.2f}{np.nanmedian(g):>9.0f}{np.nanpercentile(g,90):>9.0f}"
              f"{trn[c]:>8}{np.nanmedian(st):>11.0f}{fr:>8.0f}")

    print("\nNOTE: a low fresh% with a healthy l2/s means the FILTER is the constraint, not the market.")
    print("A low l2/s (or a missing coin) means the FEED is the constraint. They need different fixes.")


if __name__ == "__main__":
    asyncio.run(main())
