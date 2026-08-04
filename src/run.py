"""Orchestrator — wires feed -> market/strategy/execution/recorder. Runs forever (systemd) or --duration N.
  python -m src.run                # run until stopped (paper or live per config.yaml)
  python -m src.run --duration 90  # bounded test
  python -m src.run --duration 90 --test   # lowered thresholds to exercise the path quickly"""
import argparse
import asyncio
import logging
import sys
import time

from .config import load
from .strategy import MarketState, StrategyA
from .execution import PaperExecution, LiveExecution
from .recorder import Recorder
from .feed import HLFeed

NS = 1_000_000_000


def parse_l2(d):
    coin = d["coin"]; bids, asks = d["levels"]
    return coin, [(float(l["px"]), float(l["sz"])) for l in bids], \
        [(float(l["px"]), float(l["sz"])) for l in asks], int(d["time"]) * 1_000_000


def setup_log(root):
    log = logging.getLogger("stratA"); log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); log.addHandler(sh)
    fh = logging.FileHandler(root / "stratA.log"); fh.setFormatter(fmt); log.addHandler(fh)
    return log


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg, seed = load()
    log = setup_log(cfg["_root"])
    if args.test:                                    # lowered thresholds to see the path fire quickly
        cfg["detection"].update(reach_floor_bps=4, reach_pctile=90, reach_min_count=40, reach_refresh=20)
        cfg["swspan"].update(min_count=5); cfg["execution"].update(hold_s=20, exitwin_s=15)
        seed = {"reach_p998": {}, "swspan_median": 8.0}

    market = MarketState()
    strat = StrategyA(cfg, seed, market)
    Exec = LiveExecution if cfg["mode"] == "live" else PaperExecution
    execu = Exec(cfg, market, log)
    rec = Recorder(cfg, execu.closed)
    feed = HLFeed(cfg["endpoint"]["ws"], cfg["universe"])
    deadline = time.time() + args.duration if args.duration else None
    log.info(f"START mode={cfg['mode']} universe={len(cfg['universe'])} seed={len(seed.get('reach_p998', {}))}tok "
             f"sw_med={seed.get('swspan_median')} {'[TEST]' if args.test else ''}")
    n_recon = 0
    async for kind, payload in feed.stream():
        now = time.time_ns()
        if kind == "connected":
            n_recon += 1; log.info(f"connected #{n_recon}")
        elif kind == "drop":
            log.warning(f"WS drop -> reconnecting: {payload}")
        elif kind == "msg":
            ch = payload.get("channel")
            if ch == "l2Book":
                coin, bids, asks, t = parse_l2(payload["data"])
                market.update_l2(coin, bids, asks, t); rec.l2_snap(coin, bids, asks, t)
            elif ch == "trades":
                for tr in payload["data"]:
                    coin = tr["coin"]; px = float(tr["px"]); sz = float(tr["sz"])
                    de = 1.0 if tr["side"] == "B" else -1.0; t = int(tr["time"]) * 1_000_000
                    strat.on_trade(coin, px, sz, de, t); execu.on_trade(coin, px, sz, de, t)
                    rec.trade(coin, px, sz, "buy" if de > 0 else "sell", t)
        for sig in strat.poll(now):
            log.info(f"SIGNAL {sig.coin} {'BUY' if sig.dir > 0 else 'SELL'} breadth {sig.breadth} sw_span {sig.sw_span:.0f}")
            execu.on_signal(sig)
        execu.poll(now)
        rec.maybe_flush(log)
        if deadline and time.time() > deadline:
            feed.stop(); break

    rec.maybe_flush(log)
    c = execu.summary()
    log.info(f"DONE reconnects {n_recon} sweeps {strat.n_sweeps} closed {len(c)}")
    if len(c):
        log.info(f"PnL total ${c.usd.sum():+.2f} on {len(c)} trades, mean {c.net_bps.mean():+.2f}bps, "
                 f"win {100 * (c.net_bps > 0).mean():.0f}%")


if __name__ == "__main__":
    asyncio.run(main())
