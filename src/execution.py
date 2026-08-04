"""Execution adapters.
  PaperExecution — the PROVEN simulator (§Z.9/Z.4/R.1): taker entry (depth-aware) / 120s hold / 100bps path
    stop / maker-improve exit with live queue-aware fills / per-burst gross cap. No orders, no risk.
  LiveExecution  — REAL orders via the Hyperliquid SDK, gated by live_safety (dry_run default). YOU own this:
    read the ⚠ block, verify in dry_run, and accept that placing real orders is your responsibility.
Both share the same interface: on_signal(sig), on_trade(coin,px,sz,de,t_ns), poll(now_ns)."""
import os
import datetime
import numpy as np

NS = 1_000_000_000


def walk(levels, notional):
    rem, cost, base = notional, 0.0, 0.0
    for p, s in levels:
        if rem <= 0:
            break
        take = min(rem, p * s); base += take / p; cost += take; rem -= take
    if rem > 1e-9 and levels:
        p = levels[-1][0]; base += rem / p; cost += rem
    return cost / base if base > 0 else np.nan


def size_within_budget(levels, touch, buy, bud_bps, qmax):
    lim = touch * (1 + bud_bps / 1e4) if buy else touch * (1 - bud_bps / 1e4)
    notion = 0.0
    for p, s in levels:
        if (buy and p > lim) or ((not buy) and p < lim):
            break
        notion += p * s
    return min(qmax, notion)


class PaperExecution:
    def __init__(self, cfg, market, log):
        e, f = cfg["execution"], cfg["fees"]
        self.m, self.log = market, log
        self.HOLD, self.EXW, self.SIZE = e["hold_s"], e["exitwin_s"], e["size_usd"]
        self.SLIP, self.STOP, self.TICKS, self.MAXOPEN = e["slip_budget_bps"], e["stop_bps"], e["maker_improve_ticks"], e["max_open"]
        self.TAKER, self.MAKER = f["taker_bps"], f["maker_bps"]
        self.positions, self.closed = [], []

    # --- lifecycle -------------------------------------------------------
    def on_signal(self, sig):
        bk = self.m.book.get(sig.coin)
        if bk is None or len([p for p in self.positions if p["status"] != "closed"]) >= self.MAXOPEN:
            return
        buy = sig.dir > 0
        levels = bk["asks"] if buy else bk["bids"]; touch = bk["ask"] if buy else bk["bid"]
        size = max(size_within_budget(levels, touch, buy, self.SLIP, self.SIZE), 1.0)
        pin = walk(levels, size)
        if not np.isfinite(pin) or pin <= 0:
            return
        self.positions.append(dict(coin=sig.coin, dir=sig.dir, entry_ts=self._now(), entry_px=pin, size=size,
                                   status="open", breadth=sig.breadth, sw_span=sig.sw_span,
                                   exit_level=None, q_ahead=0.0, q_you=0.0, cum=0.0, exit_start=0))
        self.log.info(f"[OPEN ] {sig.coin} {'LONG' if buy else 'SHORT'} @ {pin:.6g} ${size:,.0f} breadth {sig.breadth}")

    def on_trade(self, coin, px, sz, de, t_ns):
        for pos in self.positions:                              # accumulate maker-exit fill volume
            if pos["status"] == "exiting" and pos["coin"] == coin:
                buy = pos["dir"] > 0
                if buy and de > 0 and px >= pos["exit_level"]:
                    pos["cum"] += sz
                elif (not buy) and de < 0 and px <= pos["exit_level"]:
                    pos["cum"] += sz

    def poll(self, now_ns):
        for pos in [p for p in self.positions if p["status"] != "closed"]:
            bk = self.m.book.get(pos["coin"])
            if bk is None:
                continue
            de, mid, age = pos["dir"], bk["mid"], (now_ns - pos["entry_ts"]) / NS
            if de * (mid - pos["entry_px"]) / pos["entry_px"] * 1e4 <= -self.STOP:      # path stop (always active)
                self._close(pos, bk["bid"] if de > 0 else bk["ask"], "stop"); continue
            if pos["status"] == "open" and age >= self.HOLD:                            # post maker-improve exit
                tk = self.m.tick.get(pos["coin"], 0.0) * self.TICKS; buy = de > 0
                lvl = (bk["ask"] - tk) if (buy and tk and bk["ask"] - tk > bk["bid"]) else \
                      (bk["bid"] + tk) if ((not buy) and tk and bk["bid"] + tk < bk["ask"]) else \
                      (bk["ask"] if buy else bk["bid"])
                pos.update(exit_level=lvl, q_ahead=(bk["asks"][0][1] if buy else bk["bids"][0][1]),
                           q_you=pos["size"] / lvl, status="exiting", exit_start=now_ns)
            elif pos["status"] == "exiting":
                if pos["cum"] >= pos["q_ahead"] + pos["q_you"]:                         # fully filled maker
                    self._close(pos, pos["exit_level"], "maker"); continue
                if (now_ns - pos["exit_start"]) / NS >= self.EXW:                       # window end: partial blend
                    frac = float(np.clip((pos["cum"] - pos["q_ahead"]) / pos["q_you"], 0.0, 1.0))
                    buy = de > 0; p_tk = walk(bk["bids"] if buy else bk["asks"], pos["size"])
                    self._close(pos, frac * pos["exit_level"] + (1 - frac) * p_tk, "maker" if frac > 0.5 else "taker")

    def _close(self, pos, exit_px, kind):
        de = pos["dir"]; fee = self.TAKER + (self.MAKER if kind == "maker" else self.TAKER)
        net = de * (exit_px - pos["entry_px"]) / pos["entry_px"] * 1e4 - fee
        pos["status"] = "closed"
        self.closed.append(dict(coin=pos["coin"], dir=de, entry_ts=pos["entry_ts"], entry_px=pos["entry_px"],
                                size=pos["size"], exit_px=exit_px, kind=kind, net_bps=net, usd=net * pos["size"] / 1e4,
                                breadth=pos["breadth"], sw_span=pos["sw_span"]))
        self.log.info(f"[CLOSE] {pos['coin']} {kind} net {net:+.1f}bps ${net * pos['size'] / 1e4:+.2f}")

    def _now(self):
        import time
        return time.time_ns()

    def summary(self):
        import pandas as pd
        return pd.DataFrame(self.closed)


# =====================================================================================================
class LiveExecution(PaperExecution):
    """⚠ REAL ORDERS. Reuses the paper DECISION logic (sizing, entry/exit levels, stop) but SENDS orders
    via the Hyperliquid SDK. Gated by config.live_safety. DEFAULT dry_run=True -> it LOGS the exact order
    it would place and does NOT send. To go live you must: (1) `pip install hyperliquid-python-sdk eth-account`,
    (2) create an HL *API/agent wallet* in the HL UI (NOT your main MetaMask key) and put its key in .env as
    HL_PRIVATE_KEY (+ HL_ACCOUNT_ADDRESS = your main account), (3) verify behavior in dry_run over a full
    session, (4) flip live_safety.dry_run=false yourself. Order-lifecycle details (fill polling, cancel/replace,
    partial fills) depend on your SDK version — TEST THEM. Placing real orders is YOUR responsibility.
    This class intentionally keeps the send-path small and explicit so you can audit every order."""
    def __init__(self, cfg, market, log):
        super().__init__(cfg, market, log)
        ls = cfg["live_safety"]
        self.dry = ls["dry_run"]; self.max_notional = ls["max_notional_usd"]
        self.daily_loss_stop = ls["daily_loss_stop_usd"]; self.kill = cfg["_root"] / ls["killswitch_file"]
        self.base_url = cfg["endpoint"]["rest"]; self._day = None; self._day_pnl = 0.0
        self.ex = None; self.addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
        if not self.dry:
            from hyperliquid.exchange import Exchange           # imported only when actually going live
            from eth_account import Account
            key = os.environ["HL_PRIVATE_KEY"]                  # from .env — NEVER hard-coded, NEVER committed
            self.ex = Exchange(Account.from_key(key), self.base_url, account_address=self.addr or None)
        log.warning(f"LiveExecution dry_run={self.dry} max_notional=${self.max_notional} "
                    f"daily_loss_stop=${self.daily_loss_stop} killswitch={self.kill.name}")

    def _blocked(self):
        today = datetime.datetime.utcnow().date()
        if today != self._day:
            self._day, self._day_pnl = today, 0.0
        if self.kill.exists():
            self.log.warning("killswitch present -> no new entry"); return True
        if self._day_pnl <= -self.daily_loss_stop:
            self.log.warning(f"daily loss stop hit (${self._day_pnl:.0f}) -> no new entry"); return True
        return False

    def on_signal(self, sig):
        if self._blocked():
            return
        bk = self.m.book.get(sig.coin)
        if bk is None:
            return
        buy = sig.dir > 0
        levels = bk["asks"] if buy else bk["bids"]; touch = bk["ask"] if buy else bk["bid"]
        size = min(max(size_within_budget(levels, touch, buy, self.SLIP, self.SIZE), 1.0), self.max_notional)
        coin_sz = round(size / touch, 6)
        if self.dry:
            self.log.info(f"[DRY entry] would TAKER {'BUY' if buy else 'SELL'} {sig.coin} ~{coin_sz} (${size:,.0f}) @ ~{touch:.6g}")
        else:
            # ⚠ SEND — verify the SDK signature for your version. market_open(coin, is_buy, sz, px=None, slippage).
            self.ex.market_open(sig.coin, buy, coin_sz, None, 0.01)
            self.log.info(f"[LIVE entry] TAKER {'BUY' if buy else 'SELL'} {sig.coin} {coin_sz} (${size:,.0f})")
        # Track the position with the paper lifecycle so poll() manages hold/stop/exit timing. The EXIT/STOP
        # send-paths mirror entry (post-only limit at the improve level; market close on stop) — implement +
        # test them in dry_run before flipping live. See AGENTS.md §Live execution.
        super().on_signal(sig)
