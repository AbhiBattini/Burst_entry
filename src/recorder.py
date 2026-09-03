"""Recorder — tees live trades + periodic L2 snapshots to parquet (free forward-OOS data, archive schema).
Flushes every flush_s to timestamped files so an interrupted run still keeps its data."""
import time
from collections import defaultdict
from pathlib import Path
import pandas as pd


class Recorder:
    def __init__(self, cfg, closed_ref, shadow_ref=None):
        r = cfg["recorder"]
        self.enabled = r["enabled"]; self.flush_s = r["flush_s"]
        self.dir = (cfg["_root"] / r["dir"]); self.dir.mkdir(parents=True, exist_ok=True)
        self.root = cfg["_root"]; self.closed_ref = closed_ref; self.shadow_ref = shadow_ref or []
        self.trades, self.l2 = [], []; self.last_l2 = defaultdict(float); self.last_flush = time.time()

    def trade(self, coin, px, sz, side, t_ns):
        if self.enabled:
            self.trades.append(dict(ts_ns=t_ns, coin=coin, price=px, size=sz, side=side))

    def l2_snap(self, coin, bids, asks, t_ns):
        if self.enabled and t_ns / 1e9 - self.last_l2[coin] > 0.5:      # ~2 snaps/s/coin
            self.last_l2[coin] = t_ns / 1e9
            self.l2.append(dict(ts_ns=t_ns, coin=coin, bid_px=[p for p, _ in bids], bid_sz=[s for _, s in bids],
                                ask_px=[p for p, _ in asks], ask_sz=[s for _, s in asks]))

    def maybe_flush(self, log):
        if time.time() - self.last_flush < self.flush_s:
            return
        tag = int(time.time())
        if self.trades:
            pd.DataFrame(self.trades).to_parquet(self.dir / f"trades_{tag}.parquet"); self.trades.clear()
        if self.l2:
            pd.DataFrame(self.l2).to_parquet(self.dir / f"l2_{tag}.parquet"); self.l2.clear()
        if self.closed_ref:
            pd.DataFrame(self.closed_ref).to_parquet(self.root / "paper_trades.parquet")
        if self.shadow_ref:
            pd.DataFrame(self.shadow_ref).to_parquet(self.root / "shadow_trades.parquet")
        tot = sum(c["usd"] for c in self.closed_ref)
        sh = sum(c["usd"] for c in self.shadow_ref)
        log.info(f"flush {tag}: {len(self.closed_ref)} closed trades, cum ${tot:+.2f}"
                 + (f" | SHADOW (skipped, not traded) {len(self.shadow_ref)} trades, "
                    f"cum ${sh:+.2f}" if self.shadow_ref else ""))
        self.last_flush = time.time()
