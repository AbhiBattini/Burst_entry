"""Local validation — run the REAL detector against the live feed and report what it actually sees.

Why this exists: the production trigger fires ~1.4 gated signals/hour, so a short run showing "0 trades"
is indistinguishable from a broken pipeline. `--test` is no substitute -- it lowers the reach floor from
30 bps to 4 and therefore measures a different thing entirely. This tool runs the REAL thresholds and
reports the intermediate quantities, so a few minutes is enough to tell working from broken.

It answers, in order:
  1. Is the ORDER AGGREGATOR working?           orders/min, prints per order
  2. Is the REPLAY filter cutting live flow?    rejected count, and the age distribution behind it
  3. What does genuine REACH look like?         the full distribution vs the 30 bps floor and the seed p99.8
  4. Would candidates fire at the RESEARCH rate? extrapolated candidates/hour vs ~11.7 expected

Read-only: no orders, no execution, no state written.

Usage:  .venv/bin/python tools/local_validate.py [seconds]     (default 600 = 10 min)
"""
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np

from src.config import load
from src.feed import HLFeed
from src.strategy import MarketState, StrategyA
from src.run import parse_l2

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
# research: 3,938 canonical (post non-overlap) events over 14 days x 31 tokens
EXPECTED_CAND_PER_HR = 3938 / 14 / 24


async def main():
    cfg, seed = load()
    market = MarketState()
    strat = StrategyA(cfg, seed, market)
    reach_all = defaultdict(list)          # coin -> every FRESH order reach seen
    sigs = []
    feed = HLFeed(cfg["endpoint"]["ws"], cfg["universe"])
    t_end = time.time() + DUR

    # wrap the detector so we can see EVERY fresh order's reach, not just the ones that clear the bar
    orig_close = strat._close_order

    def spy(coin, a):
        bk_ok = a["stale_ms"] < strat.FRESH_MS and a["stale_ms"] >= -strat.REPLAY_MS
        if bk_ok and a["mid"]:
            reach_all[coin].append(max(a["ext"], 0.0) / a["mid"] * 1e4)
        return orig_close(coin, a)

    strat._close_order = spy

    print(f"running the REAL config for {DUR:.0f}s ({DUR/60:.1f} min) on {len(cfg['universe'])} coins")
    print(f"floor {strat.FLOOR}bps | pctile {strat.PCT} | breadth>={strat.MINK} | "
          f"sw_span seed {strat.swmed} | DEEP floor {strat.DEEP_FLOOR}\n")
    async for kind, payload in feed.stream():
        now = time.time_ns()
        if time.time() > t_end:
            feed.stop(); break
        if kind != "msg":
            continue
        ch = payload.get("channel")
        if ch == "bbo":
            d = payload["data"]; b, a = d["bbo"]
            if b and a:
                t = int(d["time"]) * 1_000_000
                market.note_lag(now, t)
                market.update_bbo(d["coin"], float(b["px"]), float(b["sz"]),
                                  float(a["px"]), float(a["sz"]), t)
        elif ch == "l2Book":
            coin, bids, asks, t = parse_l2(payload["data"])
            market.update_l2(coin, bids, asks, t)
        elif ch == "trades":
            for tr in payload["data"]:
                strat.on_trade(tr["coin"], float(tr["px"]), float(tr["sz"]),
                               1.0 if tr["side"] == "B" else -1.0, int(tr["time"]) * 1_000_000)
        for s in strat.poll(now):
            sigs.append(s)
            print(f"  SIGNAL {s.sleeve} {s.coin} reach {s.reach:.0f}bps breadth {s.breadth} "
                  f"sw_span {s.sw_span:.0f}")

    mins = DUR / 60
    allr = np.concatenate([np.array(v) for v in reach_all.values()]) if reach_all else np.array([])

    print(f"\n=== 1. AGGREGATOR ===")
    print(f"orders {strat.n_orders:,} ({strat.n_orders/mins:.0f}/min) | fresh & scored {len(allr):,}")
    if strat.n_orders == 0:
        print("*** NO ORDERS AT ALL -- the tape is not reaching the detector. Feed problem, not a threshold.")

    print(f"\n=== 2. REPLAY FILTER ===")
    print(f"rejected as replayed: {strat.n_replay:,} of {strat.n_orders:,} "
          f"({100*strat.n_replay/max(strat.n_orders,1):.0f}%)")
    print("  ~30% on a fresh connect is EXPECTED (HL replays recent trades once, with their original")
    print("  stamps). A high rate that persists past the first seconds would mean the tolerance is wrong.")

    print(f"\n=== 3. GENUINE REACH (the thing the floor is set against) ===")
    if len(allr):
        for p in (50, 90, 99, 99.8):
            print(f"  p{p:<5} {np.percentile(allr, p):7.1f} bps")
        print(f"  max     {allr.max():7.1f} bps")
        print(f"  >= {strat.FLOOR}bps floor: {int((allr >= strat.FLOOR).sum()):,} "
              f"({100*np.mean(allr >= strat.FLOOR):.2f}% of orders)")
        print("  NB reach is measured against a FRESH touch now. A stale touch inflates it -- if these")
        print("  numbers look far larger than the floor, suspect the touch source before celebrating.")
    else:
        print("  no fresh orders scored")

    print(f"\n=== 4. CANDIDATE RATE vs RESEARCH ===")
    cph = strat.n_sweeps / mins * 60
    print(f"BURST candidates {strat.n_sweeps} in {mins:.1f} min -> {cph:.1f}/hour "
          f"(research ~{EXPECTED_CAND_PER_HR:.1f}/hour)")
    print(f"DEEP candidates  {strat.n_deep}")
    print(f"GATED signals    {len(sigs)}  (research ~1.1/hour, so 0 in a short window is normal)")
    if strat.n_orders and strat.n_sweeps == 0:
        print("  0 candidates from a healthy order flow is NOT automatically broken at this window length,")
        print(f"  but at ~{EXPECTED_CAND_PER_HR:.0f}/hour you would expect ~{EXPECTED_CAND_PER_HR*mins/60:.1f} "
              f"in {mins:.0f} min. Well under that across repeated runs = investigate the reach bar.")
    fl = market.feed_lag_ms(1)
    if fl is not None:
        print(f"\nfeed lag median {fl:.0f}ms (negative => local clock behind the exchange; fix NTP)")


if __name__ == "__main__":
    asyncio.run(main())
