"""Throttle check — does HL degrade the bbo stream when we subscribe to everything?

The live feed opens 3 channels x N coins on ONE socket (93 subscriptions at 31 coins). If the venue caps
message rate or silently drops subscriptions, BREADTH starves -- and breadth is the BURST gate. That failure
is invisible in the strategy log: it just looks like a quiet market.

Method: measure the SAME quantity (bbo messages per coin) twice on separate sockets --
  A) bbo alone            (the clean baseline)
  B) bbo + l2Book + trades (what the strategy actually runs)
If B's per-coin bbo rate is materially below A's, we are being throttled and should split channels across
sockets. If they match, the full subscription is free.

Usage:  .venv/bin/python tools/throttle_check.py [seconds_per_arm]      (default 90)
"""
import asyncio
import collections
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import websockets
import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
UNI = CFG["universe"]
URL = CFG["endpoint"]["ws"]
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0


async def probe(chans, dur):
    n = collections.Counter(); last = {}; gaps = collections.defaultdict(list)
    errs = []; nsub = 0; other = collections.Counter()
    async with websockets.connect(URL, ping_interval=20, max_queue=None) as ws:
        for c in UNI:
            for ty in chans:
                await ws.send(json.dumps({"method": "subscribe",
                                          "subscription": {"type": ty, "coin": c}}))
                nsub += 1
        t_end = time.time() + dur
        while time.time() < t_end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            ch = m.get("channel")
            if ch == "error":
                errs.append(str(m)[:150]); continue
            if ch != "bbo":
                other[ch] += 1; continue
            d = m["data"]; coin = d["coin"]; t = int(d["time"])
            n[coin] += 1
            if coin in last:
                gaps[coin].append(t - last[coin])
            last[coin] = t
    return n, gaps, errs, nsub, other


async def main():
    print(f"{len(UNI)} coins, {DUR:.0f}s per arm\n")
    res = {}
    for chans in [("bbo",), ("bbo", "l2Book", "trades")]:
        n, g, errs, nsub, other = await probe(chans, DUR)
        rates = [n[c] / DUR for c in UNI]
        allg = [x for v in g.values() for x in v]
        lbl = "+".join(chans)
        res[lbl] = statistics.median(rates)
        print(f"{lbl:24s} subs={nsub:3d}  coins_with_bbo={sum(1 for c in UNI if n[c] > 0):2d}/{len(UNI)}"
              f"  total={sum(n.values()):6d}  median={statistics.median(rates):5.2f}/s"
              f"  min={min(rates):5.2f}/s  p90gap={np.percentile(allg, 90) if allg else float('nan'):6.0f}ms")
        if other:
            print(f"{'':24s} other channels: {dict(other)}")
        if errs:
            print(f"{'':24s} *** ERRORS: {errs[:2]}")
        zero = [c for c in UNI if n[c] == 0]
        if zero:
            print(f"{'':24s} *** NO bbo AT ALL: {', '.join(zero)}")
        await asyncio.sleep(2)

    a, b = res["bbo"], res["bbo+l2Book+trades"]
    ratio = b / a if a else float("nan")
    print(f"\nmedian bbo rate  alone {a:.2f}/s -> full subscription {b:.2f}/s  = {100*ratio:.0f}%")
    if ratio < 0.75:
        print("*** THROTTLED. The full subscription is degrading the touch stream. Split channels across")
        print("    separate sockets (bbo on its own connection) before trusting breadth.")
    else:
        print("No meaningful throttling -- the full subscription costs the touch stream nothing.")
    print("\nNB per-coin bbo rate varies with how ACTIVE the book is; a quiet coin legitimately sends few")
    print("updates. What matters is that no coin is at ZERO and that the two arms agree.")


if __name__ == "__main__":
    asyncio.run(main())
