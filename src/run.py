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
from .capital import CapitalManager
from .execution import PaperExecution, LiveExecution
from .recorder import Recorder
from .feed import HLFeed

NS = 1_000_000_000


def parse_l2(d):
    coin = d["coin"]; bids, asks = d["levels"]
    return coin, [(float(l["px"]), float(l["sz"])) for l in bids], \
        [(float(l["px"]), float(l["sz"])) for l in asks], int(d["time"]) * 1_000_000


def setup_log(root):
    # Force UTF-8 on both sinks. A non-ASCII char in ANY log line kills the process on a cp1252 console
    # (Windows) — the strategy must never die because of a log format. errors="replace" is the belt.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log = logging.getLogger("stratA"); log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); log.addHandler(sh)
    fh = logging.FileHandler(root / "stratA.log", encoding="utf-8", errors="replace")
    fh.setFormatter(fmt); log.addHandler(fh)
    return log


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg, seed = load()
    log = setup_log(cfg["_root"])
    vol_grid_s, vol_win = 10, 180                    # 30-min trailing vol window (research trailing_vol)
    if args.test:                                    # lowered thresholds to see the path fire quickly
        cfg["detection"].update(reach_floor_bps=4, reach_pctile=90, reach_min_count=40, reach_refresh=20)
        cfg["swspan"].update(min_count=5); cfg["execution"].update(hold_s=20, exitwin_s=15)
        cfg["deep"].update(reach_floor_bps=6, vol_ceiling_bps_min=1e9)
        cfg["capital"].update(mode="fixed")           # never touch a real wallet in a smoke test
        vol_grid_s, vol_win = 1, 8                   # so DEEP's vol warms in ~4s instead of ~15min
        seed = {"reach_p998": {}, "swspan_median": 8.0}

    market = MarketState(vol_grid_s=vol_grid_s, vol_win=vol_win)
    strat = StrategyA(cfg, seed, market)
    Exec = LiveExecution if cfg["mode"] == "live" else PaperExecution
    execu = Exec(cfg, market, log)
    cap = CapitalManager(cfg, log)                   # sizing derives from wallet equity, not from config $
    params = cap.initial()
    strat.apply_capital(params); execu.apply_capital(params)
    execu.reconcile_on_start()                       # flatten any orphaned position/order before trading (live only)
    rec = Recorder(cfg, execu.closed)
    feed = HLFeed(cfg["endpoint"]["ws"], cfg["universe"])
    deadline = time.time() + args.duration if args.duration else None
    e = cfg["execution"]
    log.info(f"START mode={cfg['mode']} universe={len(cfg['universe'])} seed={len(seed.get('reach_p998', {}))}tok "
             f"sw_med={seed.get('swspan_median')} | equity ${params['equity']:,.0f} "
             f"cap ${params['gross_cap_usd']:,.0f} size ${params['size_usd']:,.0f} "
             f"burst_reserve {100 * e['burst_reserve_frac']:.0f}% nmax {params['nmax']} "
             f"| DEEP {'on' if cfg['deep']['enabled'] else 'OFF'} "
             f"(floor {cfg['deep']['reach_floor_bps']}bps, vol<={cfg['deep']['vol_ceiling_bps_min']}bps/min) "
             f"{'[TEST]' if args.test else ''}")
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
            log.info(f"SIGNAL {sig.sleeve} {sig.coin} {'BUY' if sig.dir > 0 else 'SELL'} reach {sig.reach:.0f}bps "
                     f"breadth {sig.breadth} sw_span {sig.sw_span:.0f} vol {sig.vol:.1f}bps/min")
            execu.on_signal(sig)
        execu.poll(now)
        # Resize to the wallet — only while FLAT, so a cap never moves under an open position.
        newp = cap.maybe_refresh(time.time(), execu.is_flat())
        if newp:
            strat.apply_capital(newp); execu.apply_capital(newp)
        rec.maybe_flush(log)
        if deadline and time.time() > deadline:
            feed.stop(); break

    rec.maybe_flush(log)
    c = execu.summary()
    log.info(f"DONE reconnects {n_recon} orders {strat.n_orders} burst-cands {strat.n_sweeps} "
             f"deep-cands {strat.n_deep} closed {len(c)} skipped-no-room {execu.n_skipped}")
    if len(c):
        log.info(f"PnL total ${c.usd.sum():+.2f} on {len(c)} trades, mean {c.net_bps.mean():+.2f}bps, "
                 f"win {100 * (c.net_bps > 0).mean():.0f}%")
        for sl, g in c.groupby("sleeve"):            # BURST carries the book; watch the split, not the total
            log.info(f"  {sl:>5}: ${g.usd.sum():+.2f} on {len(g)} trades, mean {g.net_bps.mean():+.2f}bps, "
                     f"win {100 * (g.net_bps > 0).mean():.0f}%")


if __name__ == "__main__":
    asyncio.run(main())
