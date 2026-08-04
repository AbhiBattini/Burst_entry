"""Execution adapters.
  PaperExecution — the PROVEN simulator (§Z.9/Z.4/R.1): taker entry (depth-aware) / 120s hold / 100bps path
    stop / maker-improve exit with live queue-aware fills / per-burst gross cap. No orders, no risk.
  LiveExecution  — REAL orders via the Hyperliquid SDK, gated by live_safety (dry_run default). Runs the SAME
    open-position lifecycle as the paper sim — entry, 120s hold, maker-improve exit, taker-fallback, 100bps
    stop — but with real order sends + real fill queries. The wire-touching code is isolated to the `_live_*`
    helpers; dry_run logs the intended order and models the fill off the paper tape (reconciles to Paper). YOU
    own this: read the ⚠ block, verify every `_live_*` SDK signature for your version in dry_run, and accept
    that placing real orders is your responsibility.
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

    def reconcile_on_start(self):
        """No-op for the paper sim (no account state). LiveExecution overrides to adopt-or-flatten."""
        return


# =====================================================================================================
class LiveExecution(PaperExecution):
    """⚠ REAL ORDERS. Runs the SAME state machine as the paper sim (sizing, 120s hold, maker-improve exit,
    100bps path stop, per-burst cap) but drives it with REAL orders + REAL fill queries via the Hyperliquid
    SDK. Gated by config.live_safety. DEFAULT dry_run=True -> every send/query LOGS the intended action and
    falls back to the paper tape model (so a dry_run session reconciles 1:1 against PaperExecution). Flip
    dry_run=false ONLY after: (1) `pip install hyperliquid-python-sdk eth-account`, (2) an HL *API/agent
    wallet* (NOT your MetaMask key) in .env as HL_PRIVATE_KEY (+ HL_ACCOUNT_ADDRESS = main account), (3) a
    full dry_run session watched + reconciled, (4) you verifying the SDK method signatures for YOUR version
    (they drift release-to-release — the `_live_*` helpers below are the ONLY place orders touch the wire, so
    audit those four methods). Placing real orders is YOUR responsibility.

    Design: poll() is a full reimplementation of PaperExecution.poll's timing (NOT inherited) so the exact
    live code path is exercised in dry_run. The live/dry difference lives entirely in the small `_live_*`
    helpers. Position dict gains: exit_oid (resting maker-exit order id) + last_poll (fill-poll throttle)."""
    def __init__(self, cfg, market, log):
        super().__init__(cfg, market, log)
        ls = cfg["live_safety"]
        self.dry = ls["dry_run"]; self.max_notional = ls["max_notional_usd"]
        self.daily_loss_stop = ls["daily_loss_stop_usd"]; self.kill = cfg["_root"] / ls["killswitch_file"]
        self.fill_poll_ms = ls.get("fill_poll_ms", 500)        # throttle for live user_state/order queries
        self.reconcile_start = ls.get("reconcile_on_start", True)   # flatten stale positions/orders on boot
        self.reconcile_mode = ls.get("reconcile_mode", "flatten")  # flatten | report
        self.universe = list(cfg.get("universe", []))          # restrict reconciliation to OUR coins
        self.base_url = cfg["endpoint"]["rest"]; self._day = None; self._day_pnl = 0.0
        self.ex = self.info = None; self.addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
        if not self.dry:
            from hyperliquid.exchange import Exchange           # imported only when actually going live
            from hyperliquid.info import Info
            from eth_account import Account
            key = os.environ["HL_PRIVATE_KEY"]                  # from .env — NEVER hard-coded, NEVER committed
            acct = Account.from_key(key)
            self.ex = Exchange(acct, self.base_url, account_address=self.addr or None)
            self.info = Info(self.base_url, skip_ws=True)
            if not self.addr:
                self.addr = acct.address                        # trade FOR the signer if no master given
        log.warning(f"LiveExecution dry_run={self.dry} max_notional=${self.max_notional} "
                    f"daily_loss_stop=${self.daily_loss_stop} poll={self.poll_s}s killswitch={self.kill.name}")

    # --- risk gate -------------------------------------------------------
    def _blocked(self):
        today = datetime.datetime.utcnow().date()
        if today != self._day:
            self._day, self._day_pnl = today, 0.0
        if self.kill.exists():
            self.log.warning("killswitch present -> no new entry"); return True
        if self._day_pnl <= -self.daily_loss_stop:
            self.log.warning(f"daily loss stop hit (${self._day_pnl:.0f}) -> no new entry"); return True
        return False

    # --- startup reconciliation ------------------------------------------
    def reconcile_on_start(self):
        """Adopt-or-flatten pre-existing account state on boot. The process can die mid-trade, orphaning a
        position (and its resting reduce-only exit) that this object knows nothing about — poll() would then
        never manage it. Since the strategy horizon is ~120s, anything that outlived a restart is STALE, so
        the safe default is FLATTEN: cancel stray resting orders on our universe, then market-close any open
        position on our universe. Restricted to cfg.universe so a shared account's other orders are untouched.
        `reconcile_mode: report` logs what it finds without sending (use it for the first live boot). Adopting
        a position back into the state machine is intentionally NOT done — entry_ts/px/breadth are unrecoverable
        and a stale hold is worse than a clean flat. dry_run / paper: no account -> no-op."""
        if not self.reconcile_start:
            self.log.warning("[reconcile] disabled (live_safety.reconcile_on_start=false) -> orphaned state NOT handled")
            return
        if self.dry:
            self.log.info("[reconcile] dry_run: no live account queries -> skipping")
            return
        uni = set(self.universe); flatten = self.reconcile_mode == "flatten"
        self.log.warning(f"[reconcile] scanning account {self.addr[:10]}… mode={self.reconcile_mode} universe={len(uni)}")
        try:
            oo = self.info.open_orders(self.addr) or []              # ⚠ verify: open_orders(addr) -> [{coin,oid,...}]
        except Exception as e:
            self.log.error(f"[reconcile] open_orders query FAILED ({e}) -> aborting reconcile, do NOT trade"); raise
        for o in oo:
            c = o.get("coin")
            if c in uni:
                if flatten:
                    try:
                        self.ex.cancel(c, o["oid"]); self.log.warning(f"[reconcile] cancelled stray order {c} oid={o['oid']}")
                    except Exception as e:
                        self.log.error(f"[reconcile] cancel {c} oid={o.get('oid')} FAILED: {e}")
                else:
                    self.log.warning(f"[reconcile] REPORT stray order {c} oid={o.get('oid')} sz={o.get('sz')} (not cancelled)")
        try:
            st = self.info.user_state(self.addr)                     # ⚠ verify: user_state(addr).assetPositions[*].position
            positions = st.get("assetPositions", []) if isinstance(st, dict) else []
        except Exception as e:
            self.log.error(f"[reconcile] user_state query FAILED ({e}) -> aborting reconcile, do NOT trade"); raise
        n_flat = 0
        for ap in positions:
            p = ap.get("position", ap) if isinstance(ap, dict) else {}
            c = p.get("coin"); szi = float(p.get("szi", 0) or 0)
            if c in uni and abs(szi) > 0:
                if flatten:
                    try:
                        r = self.ex.market_close(c); n_flat += 1
                        self.log.warning(f"[reconcile] FLATTEN {c} szi={szi} entryPx={p.get('entryPx')} -> {r}")
                    except Exception as e:
                        self.log.error(f"[reconcile] flatten {c} szi={szi} FAILED: {e} -> FLATTEN MANUALLY")
                else:
                    self.log.warning(f"[reconcile] REPORT open position {c} szi={szi} entryPx={p.get('entryPx')} (not flattened)")
        self.log.warning(f"[reconcile] complete: {len(oo)} orders scanned, {n_flat} positions flattened")

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
        coin_sz = round(size / touch, 6)
        fill_px = self._live_taker(sig.coin, buy, coin_sz, size, touch)     # entry send (dry logs, returns modeled)
        if not np.isfinite(fill_px) or fill_px <= 0:
            self.log.error(f"[entry ABORT] {sig.coin} no valid fill px -> not tracking a phantom position"); return
        self.positions.append(dict(coin=sig.coin, dir=sig.dir, entry_ts=self._now(), entry_px=fill_px, size=size,
                                   status="open", breadth=sig.breadth, sw_span=sig.sw_span,
                                   exit_level=None, q_ahead=0.0, q_you=0.0, cum=0.0, exit_start=0,
                                   exit_oid=None, last_poll=0))
        self.log.info(f"[OPEN ] {sig.coin} {'LONG' if buy else 'SHORT'} @ {fill_px:.6g} ${size:,.0f} breadth {sig.breadth}")

    # --- lifecycle (full reimpl so live code is what dry_run exercises) --
    def poll(self, now_ns):
        for pos in [p for p in self.positions if p["status"] != "closed"]:
            bk = self.m.book.get(pos["coin"])
            if bk is None:
                continue
            de, mid, age = pos["dir"], bk["mid"], (now_ns - pos["entry_ts"]) / NS
            # (1) 100 bps path stop — always active. Cancel any resting exit, then TAKER close.
            if de * (mid - pos["entry_px"]) / pos["entry_px"] * 1e4 <= -self.STOP:
                self._live_cancel(pos)
                self._close(pos, self._live_taker_close(pos, bk), "stop"); continue
            # (2) hold elapsed -> post a maker-improve reduce-only limit exit
            if pos["status"] == "open" and age >= self.HOLD:
                tk = self.m.tick.get(pos["coin"], 0.0) * self.TICKS; buy = de > 0
                lvl = (bk["ask"] - tk) if (buy and tk and bk["ask"] - tk > bk["bid"]) else \
                      (bk["bid"] + tk) if ((not buy) and tk and bk["bid"] + tk < bk["ask"]) else \
                      (bk["ask"] if buy else bk["bid"])
                pos.update(exit_level=lvl, q_ahead=(bk["asks"][0][1] if buy else bk["bids"][0][1]),
                           q_you=pos["size"] / lvl, status="exiting", exit_start=now_ns, cum=0.0, last_poll=0)
                pos["exit_oid"] = self._live_post_exit(pos, lvl)
            # (3) exiting -> query real fill; TAKER-fallback for the remainder at window end
            elif pos["status"] == "exiting":
                filled_frac = self._live_exit_fill_frac(pos, now_ns)       # 0..1 of the position filled maker
                if filled_frac >= 1.0 - 1e-9:
                    self._live_cancel(pos)
                    self._close(pos, pos["exit_level"], "maker"); continue
                if (now_ns - pos["exit_start"]) / NS >= self.EXW:          # window end: cancel + taker remainder
                    self._live_cancel(pos)
                    p_tk = self._live_taker_close(pos, bk)
                    blended = filled_frac * pos["exit_level"] + (1 - filled_frac) * p_tk
                    self._close(pos, blended, "maker" if filled_frac > 0.5 else "taker")

    def _close(self, pos, exit_px, kind):
        super()._close(pos, exit_px, kind)
        self._day_pnl += self.closed[-1]["usd"]                            # feed the daily-loss stop

    # --- the ONLY wire-touching code. dry_run: log + paper model. live: SDK. Verify signatures for YOUR SDK.
    def _live_taker(self, coin, buy, coin_sz, size, touch):
        """Entry: market order same side. Returns the fill px (modeled in dry, parsed from response live)."""
        if self.dry:
            self.log.info(f"[DRY entry] TAKER {'BUY' if buy else 'SELL'} {coin} ~{coin_sz} (${size:,.0f}) @ ~{touch:.6g}")
            bk = self.m.book.get(coin)                                     # model the fill = book-walk VWAP into the sweep
            return walk(bk["asks"] if buy else bk["bids"], size) if bk else touch
        r = self.ex.market_open(coin, buy, coin_sz, None, 0.01)            # ⚠ market_open(coin,is_buy,sz,px,slippage)
        self.log.info(f"[LIVE entry] TAKER {'BUY' if buy else 'SELL'} {coin} {coin_sz} (${size:,.0f}) -> {r}")
        return self._avg_px(r, touch)

    def _live_post_exit(self, pos, lvl):
        """Post-only (Alo) reduce-only limit in the profit direction. Returns the resting order id (or None)."""
        coin, exit_buy = pos["coin"], pos["dir"] < 0                       # exit closes the position -> opposite side
        sz = round(pos["size"] / lvl, 6)
        if self.dry:
            self.log.info(f"[DRY exit ] POST-ONLY {'BUY' if exit_buy else 'SELL'} {coin} {sz} @ {lvl:.6g} (reduce-only)")
            return -1
        r = self.ex.order(coin, exit_buy, sz, float(lvl), {"limit": {"tif": "Alo"}}, reduce_only=True)  # ⚠ verify sig
        oid = self._oid(r)
        self.log.info(f"[LIVE exit ] POST-ONLY {'BUY' if exit_buy else 'SELL'} {coin} {sz} @ {lvl:.6g} oid={oid} -> {r}")
        return oid

    def _live_exit_fill_frac(self, pos, now_ns):
        """Fraction of the position the resting maker exit has filled. dry: tape model (parent on_trade fills
        pos['cum']). live: throttled query_order_by_oid, filled/total. Returns 0..1."""
        if self.dry:
            denom = pos["q_ahead"] + pos["q_you"]                          # paper model: queue-ahead + own size
            return float(np.clip((pos["cum"] - pos["q_ahead"]) / pos["q_you"], 0.0, 1.0)) if pos["q_you"] > 0 else 0.0
        if (now_ns - pos["last_poll"]) / 1e6 < self.fill_poll_ms or pos["exit_oid"] in (None, -1):
            return 0.0
        pos["last_poll"] = now_ns
        try:
            st = self.info.query_order_by_oid(self.addr, pos["exit_oid"])  # ⚠ verify shape for YOUR SDK version
            o = st.get("order", st).get("order", st)
            total = float(o.get("origSz", o.get("sz", 0)) or 0)
            rem = float(o.get("sz", 0) or 0)
            return float(np.clip((total - rem) / total, 0.0, 1.0)) if total > 0 else 0.0
        except Exception as e:                                             # a failed query must NOT fake a fill
            self.log.error(f"[exit poll ERR] {pos['coin']} oid={pos['exit_oid']}: {e}"); return 0.0

    def _live_cancel(self, pos):
        if pos.get("exit_oid") in (None, -1):
            return
        if self.dry:
            self.log.info(f"[DRY cancel] {pos['coin']} oid={pos['exit_oid']}")
        else:
            try:
                self.ex.cancel(pos["coin"], pos["exit_oid"])              # ⚠ cancel(coin, oid)
                self.log.info(f"[LIVE cancel] {pos['coin']} oid={pos['exit_oid']}")
            except Exception as e:
                self.log.error(f"[cancel ERR] {pos['coin']} oid={pos['exit_oid']}: {e}")
        pos["exit_oid"] = None

    def _live_taker_close(self, pos, bk):
        """TAKER close of the (remaining) position — the stop exit and the window-end fallback. Returns fill px."""
        de = pos["dir"]; touch = bk["bid"] if de > 0 else bk["ask"]        # crossing the spread to flatten
        if self.dry:
            self.log.info(f"[DRY close] TAKER {'SELL' if de > 0 else 'BUY'} {pos['coin']} ~${pos['size']:,.0f}")
            return walk(bk["bids"] if de > 0 else bk["asks"], pos["size"])  # book-walk out
        r = self.ex.market_close(pos["coin"])                             # ⚠ market_close(coin) flattens the position
        self.log.info(f"[LIVE close] TAKER {'SELL' if de > 0 else 'BUY'} {pos['coin']} -> {r}")
        return self._avg_px(r, touch)

    # --- SDK response parsers (defensive — response shape varies by version; fall back to a reference px) ----
    @staticmethod
    def _statuses(r):
        try:
            return r["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return []

    def _avg_px(self, r, ref):
        for s in self._statuses(r):
            f = s.get("filled") if isinstance(s, dict) else None
            if f and "avgPx" in f:
                return float(f["avgPx"])
        self.log.warning(f"[parse] no avgPx in response; using reference px {ref:.6g} -> {r}")
        return float(ref)

    def _oid(self, r):
        for s in self._statuses(r):
            if isinstance(s, dict):
                if "resting" in s:
                    return s["resting"].get("oid")
                if "filled" in s:                                          # crossed immediately (shouldn't w/ Alo)
                    return s["filled"].get("oid")
        return None
