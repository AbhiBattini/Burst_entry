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
    """⚠ REAL ORDERS. Reuses the paper DECISION logic (sizing, entry/exit levels, stop timing) but SENDS the
    orders via the Hyperliquid SDK. Gated by config.live_safety. DEFAULT dry_run=True -> it LOGS the exact
    order it would place at every lifecycle transition (entry, maker-improve exit, cancel, stop/taker close)
    and does NOT send. To go live: (1) `pip install hyperliquid-python-sdk eth-account`, (2) create an HL
    *API/agent wallet* in the HL UI (NOT your main MetaMask key) and put its key in .env as HL_PRIVATE_KEY
    (+ HL_ACCOUNT_ADDRESS = your main account), (3) run mode:live with dry_run:true and watch a FULL session
    of the logged orders, (4) flip live_safety.dry_run=false yourself.

    ⚠ SDK-VERSION SURFACE — the exact calls below (`market_open`, `order` with an Alo post-only limit,
    `cancel`, `market_close`) and the `Info.user_state` / order-response shapes vary across
    hyperliquid-python-sdk releases. They are decisions-frozen but transport-unverified: confirm each against
    YOUR installed version in dry_run before trusting them. Placing real orders is YOUR responsibility.

    Design: entry is a taker market order (on_signal). poll() then drives, per position:
      - path stop (always active): cancel any resting exit, then market-close (reduce-only).
      - hold elapsed: post a reduce-only post-only (Alo) limit at the maker-improve level.
      - resting-exit fill: confirmed live via user_state (position flattened); in dry_run reused from the
        paper queue-sim so a dry session still produces a comparable PnL trace.
      - exit window elapsed without full fill: cancel the resting limit, market-close the remainder (taker).
    Reduce-only guarantees exits never flip the position. Recorded exit prices are ESTIMATES (intended level
    / touch) — real realized PnL must be reconciled from HL fills; the estimate only drives the daily-loss
    brake. NOTE: market_close / user_state net per COIN, so a single position per coin at a time is assumed
    (the burst dedup — 1 entry/direction/cooldown — makes overlap on one coin effectively impossible)."""
    FLAT_EPS = 1e-9

    def __init__(self, cfg, market, log):
        super().__init__(cfg, market, log)
        ls = cfg["live_safety"]
        self.dry = ls["dry_run"]; self.max_notional = ls["max_notional_usd"]
        self.daily_loss_stop = ls["daily_loss_stop_usd"]; self.kill = cfg["_root"] / ls["killswitch_file"]
        self.poll_s = ls.get("fill_poll_s", 2.0)                # how often to reconcile fills via user_state (live only)
        self.base_url = cfg["endpoint"]["rest"]; self._day = None; self._day_pnl = 0.0
        self.ex = self.info = None; self.addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
        self._live = {}; self._last_poll = 0                    # coin -> signed live position size; last user_state poll ns
        if not self.dry:
            from hyperliquid.exchange import Exchange           # imported only when actually going live
            from hyperliquid.info import Info
            from eth_account import Account
            key = os.environ["HL_PRIVATE_KEY"]                  # from .env — NEVER hard-coded, NEVER committed
            self.ex = Exchange(Account.from_key(key), self.base_url, account_address=self.addr or None)
            self.info = Info(self.base_url, skip_ws=True)       # REST-only client for fill polling
        log.warning(f"LiveExecution dry_run={self.dry} max_notional=${self.max_notional} "
                    f"daily_loss_stop=${self.daily_loss_stop} poll={self.poll_s}s killswitch={self.kill.name}")

    def _blocked(self):
        today = datetime.datetime.utcnow().date()
        if today != self._day:
            self._day, self._day_pnl = today, 0.0
        if self.kill.exists():
            self.log.warning("killswitch present -> no new entry"); return True
        if self._day_pnl <= -self.daily_loss_stop:
            self.log.warning(f"daily loss stop hit (${self._day_pnl:.0f}) -> no new entry"); return True
        return False

    # --- entry -----------------------------------------------------------
    def on_signal(self, sig):
        if self._blocked():
            return
        bk = self.m.book.get(sig.coin)
        if bk is None or len([p for p in self.positions if p["status"] != "closed"]) >= self.MAXOPEN:
            return
        buy = sig.dir > 0
        levels = bk["asks"] if buy else bk["bids"]; touch = bk["ask"] if buy else bk["bid"]
        size = min(max(size_within_budget(levels, touch, buy, self.SLIP, self.SIZE), 1.0), self.max_notional)
        pin = walk(levels, size)
        if not np.isfinite(pin) or pin <= 0:
            return
        coin_sz = round(size / touch, 6); side = "BUY" if buy else "SELL"
        if self.dry:
            self.log.info(f"[DRY entry ] TAKER {side} {sig.coin} {coin_sz} (${size:,.0f}) @ ~{touch:.6g}")
        else:
            try:                                                # ⚠ verify signature: market_open(coin, is_buy, sz, px, slippage)
                self.ex.market_open(sig.coin, buy, coin_sz, None, 0.01)
                self.log.info(f"[LIVE entry ] TAKER {side} {sig.coin} {coin_sz} (${size:,.0f})")
            except Exception as e:
                self.log.error(f"[LIVE entry ] market_open failed {sig.coin}: {e}"); return
        # Track with the ACTUALLY-ORDERED size (capped) so exit sizing + PnL match what we sent.
        self.positions.append(dict(coin=sig.coin, dir=sig.dir, entry_ts=self._now(), entry_px=pin, size=size,
                                   status="open", breadth=sig.breadth, sw_span=sig.sw_span, exit_level=None,
                                   q_ahead=0.0, q_you=0.0, cum=0.0, exit_start=0, exit_oid=None, seen_live=False))

    def on_trade(self, coin, px, sz, de, t_ns):
        # LIVE fills are confirmed by polling user_state (not the tape); the paper queue-sim only backs dry_run.
        if self.dry:
            super().on_trade(coin, px, sz, de, t_ns)

    # --- lifecycle (send-paths + fill confirmation) ----------------------
    def poll(self, now_ns):
        if not self.dry and self.info is not None and (now_ns - self._last_poll) / NS >= self.poll_s:
            self._refresh_live(); self._last_poll = now_ns
        for pos in [p for p in self.positions if p["status"] != "closed"]:
            bk = self.m.book.get(pos["coin"])
            if bk is None:
                continue
            de, mid, age = pos["dir"], bk["mid"], (now_ns - pos["entry_ts"]) / NS
            # 1) PATH STOP — always active. Pull any resting exit, then market-close.
            if de * (mid - pos["entry_px"]) / pos["entry_px"] * 1e4 <= -self.STOP:
                if pos["status"] == "exiting":
                    self._cancel_exit(pos)
                self._market_close(pos, "stop")
                self._settle(pos, bk["bid"] if de > 0 else bk["ask"], "stop"); continue
            # 2) HOLD elapsed — post the reduce-only maker-improve exit.
            if pos["status"] == "open" and age >= self.HOLD:
                lvl = self._improve_level(pos, bk); buy = de > 0
                pos.update(exit_level=lvl, exit_start=now_ns, status="exiting", cum=0.0,
                           q_ahead=(bk["asks"][0][1] if buy else bk["bids"][0][1]), q_you=pos["size"] / lvl)
                pos["exit_oid"] = self._post_exit(pos, lvl); continue
            # 3) resting exit — filled? else window elapsed -> cancel + taker close.
            if pos["status"] == "exiting":
                if self._exit_filled(pos):
                    self._settle(pos, pos["exit_level"], "maker"); continue
                if (now_ns - pos["exit_start"]) / NS >= self.EXW:
                    self._cancel_exit(pos)
                    buy = de > 0; p_tk = walk(bk["bids"] if buy else bk["asks"], pos["size"])
                    self._market_close(pos, "exit-taker")
                    if self.dry:                                # blend the partial maker fill exactly like the paper sim
                        frac = float(np.clip((pos["cum"] - pos["q_ahead"]) / pos["q_you"], 0.0, 1.0))
                        self._settle(pos, frac * pos["exit_level"] + (1 - frac) * p_tk, "maker" if frac > 0.5 else "taker")
                    else:
                        self._settle(pos, p_tk, "taker")

    def _improve_level(self, pos, bk):
        buy = pos["dir"] > 0; tk = self.m.tick.get(pos["coin"], 0.0) * self.TICKS
        if buy and tk and bk["ask"] - tk > bk["bid"]:
            return bk["ask"] - tk
        if (not buy) and tk and bk["bid"] + tk < bk["ask"]:
            return bk["bid"] + tk
        return bk["ask"] if buy else bk["bid"]

    def _exit_filled(self, pos):
        if self.dry:                                            # paper queue-sim: your resting size cleared the queue
            return pos["cum"] >= pos["q_ahead"] + pos["q_you"]
        sz = abs(self._live_size(pos["coin"]))                  # LIVE: reduce-only order flattened the position
        if sz > self.FLAT_EPS:
            pos["seen_live"] = True                             # only trust "flat" after we've observed the position live
        return pos["seen_live"] and sz <= self.FLAT_EPS

    # --- SDK send-paths (all dry-aware; ⚠ verify against your SDK version) ----
    def _post_exit(self, pos, level):
        is_buy = pos["dir"] < 0                                 # exit a long by SELLING, a short by BUYING
        sz = round(pos["size"] / level, 6); side = "BUY" if is_buy else "SELL"
        if self.dry:
            self.log.info(f"[DRY exit  ] POST-ONLY {side} {pos['coin']} {sz} @ {level:.6g} reduce-only"); return None
        try:                                                    # ⚠ Alo = post-only (add-liquidity-only); reduce_only never flips
            r = self.ex.order(pos["coin"], is_buy, sz, level, {"limit": {"tif": "Alo"}}, reduce_only=True)
            oid = self._oid(r)
            self.log.info(f"[LIVE exit  ] POST-ONLY {side} {pos['coin']} {sz} @ {level:.6g} oid={oid}"); return oid
        except Exception as e:
            self.log.error(f"[LIVE exit  ] order failed {pos['coin']}: {e}"); return None

    def _cancel_exit(self, pos):
        oid = pos.get("exit_oid")
        if self.dry:
            self.log.info(f"[DRY cancel] resting exit {pos['coin']} oid={oid}"); return
        if not oid:
            return
        try:
            self.ex.cancel(pos["coin"], oid)
        except Exception as e:
            self.log.warning(f"[LIVE cancel] failed {pos['coin']} oid={oid}: {e}")
        pos["exit_oid"] = None

    def _market_close(self, pos, reason):
        side = "SELL" if pos["dir"] > 0 else "BUY"              # reduce-only close of the position
        if self.dry:
            self.log.info(f"[DRY {reason}] MARKET-CLOSE {pos['coin']} ({side})"); return
        try:
            self.ex.market_close(pos["coin"])
            self.log.info(f"[LIVE {reason}] MARKET-CLOSE {pos['coin']} ({side})")
        except Exception as e:
            self.log.error(f"[LIVE {reason}] market_close failed {pos['coin']}: {e}")

    # --- fill polling + settlement ---------------------------------------
    def _refresh_live(self):
        try:                                                    # ⚠ user_state -> assetPositions[].position.szi (signed)
            st = self.info.user_state(self.addr)
        except Exception as e:
            self.log.warning(f"[LIVE poll ] user_state failed: {e}"); return
        live = {}
        for ap in (st.get("assetPositions") or []):
            p = ap.get("position") or {}
            c = p.get("coin")
            try:
                szi = float(p.get("szi", 0) or 0)
            except (TypeError, ValueError):
                szi = 0.0
            if c:
                live[c] = szi
        self._live = live

    def _live_size(self, coin):
        return self._live.get(coin, 0.0)

    def _settle(self, pos, exit_px, kind):
        de = pos["dir"]; fee = self.TAKER + (self.MAKER if kind == "maker" else self.TAKER)
        net = de * (exit_px - pos["entry_px"]) / pos["entry_px"] * 1e4 - fee
        usd = net * pos["size"] / 1e4
        pos["status"] = "closed"; self._day_pnl += usd                       # est PnL drives the daily-loss brake
        self.closed.append(dict(coin=pos["coin"], dir=de, entry_ts=pos["entry_ts"], entry_px=pos["entry_px"],
                                size=pos["size"], exit_px=exit_px, kind=kind, net_bps=net, usd=usd,
                                breadth=pos["breadth"], sw_span=pos["sw_span"]))
        tag = "DRY" if self.dry else "LIVE"
        self.log.info(f"[{tag} close] {pos['coin']} {kind} net {net:+.1f}bps ${usd:+.2f} (est; reconcile vs HL fills)")

    @staticmethod
    def _oid(resp):                                             # ⚠ order-response shape is SDK-version-dependent
        try:
            st = resp["response"]["data"]["statuses"][0]
            return (st.get("resting") or st.get("filled") or {}).get("oid")
        except Exception:
            return None
