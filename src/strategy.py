"""Signal engine — MarketState + StrategyA. Pure logic, no network/orders (see AGENTS.md §Detection..§Sleeves).

Maps 1:1 to the research spec in ../CLAUDE.md (CURRENT STATE) / notes §AB.3, §AC.4, §AC.5, §AE:

  DETECTOR (§AB.3)  consecutive same-side prints with gap <= agg_gap_ms are ONE aggressor ORDER; reach is the
                    order's max penetration past the touch AT ORDER START. Scoring per PRINT (the pre-§AB.3
                    code) threw 339,774 candidates where 776 real orders existed on a fragmented tape (HYPE,
                    $17 median print): the rolling p99.8 could not keep up and one order fired hundreds of
                    times. This is the single biggest fidelity gap between the old live code and the research.
  BURST (the CORE)  reach >= max(reach_floor_bps, rolling p99.8 of ORDER reach), breadth >= min_k other books
                    swept the same direction in the prior 120s, sw_span >= trailing median, dedup nmax per
                    direction per cooldown. The edge IS the common factor — solo sweeps are net-NEGATIVE (§O).
  DEEP (overlay)    reach >= max(k_vol * own trailing vol, deep reach_floor_bps, rolling p99.8), NO breadth or
                    sw_span gate, AND own trailing vol <= vol_ceiling_bps_min. The ceiling must be ABSOLUTE —
                    a per-token percentile version fails in both regimes (§AC.4). It self-disables in a
                    cascade (10 of 270 Oct-10 events survived it).
  BOTH              non-overlapping per token over (reaction + hold): one candidate per token per ~120s, and
                    breadth is counted over that NON-OVERLAPPED stream (matches research select()).
                    A BURST and a DEEP candidate on the same (token, order) -> BURST wins.

Hot path stays cheap: per-trade work is a dict lookup + one comparison; percentile/median recompute is
amortized (detection.reach_refresh, swspan.refresh)."""
import datetime
from collections import deque, defaultdict
import numpy as np

NS = 1_000_000_000


class MarketState:
    """Live order-book + trade tape state, updated by the feed; read by strategy + execution.
    Also maintains the 10s-grid trailing volatility (bps/min) that gates the DEEP sleeve."""
    def __init__(self, trade_buf=4000, vol_grid_s=10, vol_win=180, lag_buf=200):
        self.book = {}       # coin -> dict(bid, ask, mid, t, bids=[(px,sz)], asks=[(px,sz)])
        self.tick = {}       # coin -> estimated price tick
        self.trades = defaultdict(lambda: deque(maxlen=trade_buf))   # coin -> (t_ns, px, dir)
        self.GRID = vol_grid_s * NS; self.VW = vol_win
        self._vt = {}                                                # coin -> last grid bucket
        self._vm = {}                                                # coin -> last grid mid
        self._vr = defaultdict(lambda: deque(maxlen=vol_win))        # coin -> log returns on the grid
        self._lag = deque(maxlen=lag_buf)                            # ms from exchange stamp to our receipt

    def _touch(self, coin, bid, ask, bid_sz, ask_sz, t_ns):
        """Set the touch from whichever source is NEWER. HL's public l2Book arrives ~every 5.4s while bbo
        arrives ~every 78ms, so a late l2Book must never overwrite a fresh bbo touch and re-stale the book."""
        bk = self.book.get(coin)
        if bk is None:
            self.book[coin] = bk = dict(bids=[], asks=[], t_ladder=0)
        if t_ns >= bk.get("t", -1):
            mid = 0.5 * (bid + ask)
            bk.update(bid=bid, ask=ask, mid=mid, t=t_ns, bid_sz=bid_sz, ask_sz=ask_sz)
            b = t_ns // self.GRID                                    # 10s grid sample for trailing vol
            if self._vt.get(coin) != b:
                prev = self._vm.get(coin)
                if prev and prev > 0 and self._vt.get(coin) is not None:
                    self._vr[coin].append(float(np.log(mid / prev)))
                self._vt[coin] = b; self._vm[coin] = mid

    def update_bbo(self, coin, bid, bid_sz, ask, ask_sz, t_ns):
        """THE TOUCH. Everything latency-critical -- reach, the fresh_ms filter, sw_span, the stop, the exit
        level -- reads this. See tools/feed_doctor.py for why l2Book alone is not enough."""
        self._touch(coin, bid, ask, bid_sz, ask_sz, t_ns)

    def update_l2(self, coin, bids, asks, t_ns):
        """DEPTH only: the ladder for depth-aware sizing and entry/exit book-walks. Slow (~5.4s) and that is
        fine -- sizing tolerates a stale ladder in a way that reach does not."""
        if not bids or not asks:
            return
        bk = self.book.get(coin)
        if bk is None:
            self.book[coin] = bk = dict(t=-1)
        bk["bids"] = bids; bk["asks"] = asks; bk["t_ladder"] = t_ns
        da = np.diff([p for p, _ in asks[:6]]); da = da[da > 0]
        if len(da):
            self.tick[coin] = float(da.min())
        self._touch(coin, bids[0][0], asks[0][0], bids[0][1], asks[0][1], t_ns)   # no-op if bbo is fresher

    def vol_bps_min(self, coin):
        """Trailing realised vol in bps/MINUTE: std of 10s log returns * sqrt(6) * 1e4 (research trailing_vol).
        nan until half the window is filled (~15 min of book) — DEEP simply stays off until then."""
        r = self._vr.get(coin)
        if r is None or len(r) < self.VW // 2:
            return float("nan")
        return float(np.std(np.asarray(r), ddof=1) * np.sqrt(6.0) * 1e4)

    def note_lag(self, recv_ns, msg_ts_ns):
        """Record feed lag for one message. This is the honest measure of how stale our view of the book
        is at decision time — it costs nothing (no extra requests) and it is what the 0.4s reaction budget
        is actually spent on. Assumes an NTP-synced clock; see feed_lag_ms for the skew guard."""
        self._lag.append((recv_ns - msg_ts_ns) / 1e6)

    def feed_lag_ms(self, min_samples=50):
        """Rolling MEDIAN feed lag in ms (median, not mean: one GC pause must not trip a halt).
        None until warm. A persistently NEGATIVE median means the local clock is ahead of the exchange
        (NTP problem), not a fast feed — the caller flags that rather than treating it as healthy."""
        if len(self._lag) < min_samples:
            return None
        return float(np.median(np.asarray(self._lag)))

    def add_trade(self, coin, px, de, t_ns):
        self.trades[coin].append((t_ns, px, de))


class Signal:
    __slots__ = ("coin", "dir", "breadth", "sw_span", "ts", "sleeve", "reach", "vol")
    def __init__(self, coin, d, breadth, sw_span, ts, sleeve, reach, vol):
        self.coin, self.dir, self.breadth, self.sw_span, self.ts = coin, d, breadth, sw_span, ts
        self.sleeve, self.reach, self.vol = sleeve, reach, vol


class StrategyA:
    def __init__(self, cfg, seed, market):
        d, b, s, bu, dp, e = (cfg["detection"], cfg["breadth"], cfg["swspan"], cfg["burst"],
                              cfg["deep"], cfg["execution"])
        self.m = market
        self.FLOOR, self.PCT, self.RBUF = d["reach_floor_bps"], d["reach_pctile"], d["reach_buf"]
        self.RMINC, self.REFRESH, self.FRESH_MS = d["reach_min_count"], d["reach_refresh"], d["fresh_ms"]
        self.AGG_NS = int(d["agg_gap_ms"] * 1e6)
        self.BW, self.MINK = b["window_s"] * NS, b["min_k"]
        self.CLUS, self.SWMINC, self.SWREF = s["cluster_s"], s["min_count"], s["refresh"]
        self.COOL = bu["cooldown_s"] * NS
        self.NMAX = 1                    # set by apply_capital() — derived from equity (src/capital.py)
        self.DEEP_ON = dp["enabled"]; self.DEEP_FLOOR = dp["reach_floor_bps"]
        self.KVOL = dp["k_vol"]; self.VCEIL = dp["vol_ceiling_bps_min"]
        # per-token non-overlap window = the position lifetime the research selector used
        self.NONOVL = int((e["reaction_s"] + e["hold_s"]) * NS)

        self.reach = defaultdict(lambda: deque(maxlen=self.RBUF)); self.since = defaultdict(int)
        rp = seed.get("reach_p998", {}) or {}
        self.thr = {c: max(self.FLOOR, rp.get(c, self.FLOOR * 3)) for c in cfg["universe"]}   # warm from t0
        self.agg = {}                                    # coin -> in-flight aggressor order
        self.last_cand = defaultdict(lambda: -(1 << 62))  # coin -> ts of last BURST candidate (non-overlap)
        self.last_deep = defaultdict(lambda: -(1 << 62))  # coin -> ts of last DEEP  candidate (non-overlap)
        self.sweeps = deque()                            # non-overlapped BURST candidates, for breadth
        self.swhist = deque(maxlen=s["hist_len"]); self.swsince = 0
        self.swmed = seed.get("swspan_median") or float("inf")
        self.pending = []; self.fired = {1.0: deque(), -1.0: deque()}
        self.clock_hours = set(cfg.get("clock", {}).get("restrict_utc_hours") or [])
        self.n_orders = self.n_sweeps = self.n_deep = 0

    def apply_capital(self, p):
        """Adopt the burst dedup width from CapitalManager. §AD: nmax is a CAPITAL-RATIONING dial — it is 1
        while the cap is one trade wide and rises as capital allows, up to capital.nmax_max."""
        self.NMAX = p["nmax"]

    # --- §AB.3 order aggregation -----------------------------------------
    def on_trade(self, coin, px, sz, de, t_ns):
        bk = self.m.book.get(coin)
        if bk is None:
            return
        self.m.add_trade(coin, px, de, t_ns)
        a = self.agg.get(coin)
        if a is not None and a["dir"] == de and (t_ns - a["last"]) <= self.AGG_NS \
                and (t_ns - a["t0"]) < self.CLUS * NS:
            a["ext"] = max(a["ext"], (px - a["touch"]) if de > 0 else (a["touch"] - px))
            a["last"] = t_ns
            return
        if a is not None:
            self._close_order(coin, a)
        self.agg[coin] = dict(dir=de, t0=t_ns, last=t_ns, touch=(bk["ask"] if de > 0 else bk["bid"]),
                              mid=bk["mid"], stale_ms=(t_ns - bk["t"]) / 1e6,
                              ext=max((px - bk["ask"]) if de > 0 else (bk["bid"] - px), 0.0))

    def _flush_stale(self, now_ns):
        """An order also ends on silence, or once it has run past the cluster window — so a long-running
        order is still entered inside the reaction budget instead of never closing."""
        for coin in [c for c, a in self.agg.items()
                     if (now_ns - a["last"]) > self.AGG_NS or (now_ns - a["t0"]) >= self.CLUS * NS]:
            self._close_order(coin, self.agg.pop(coin))

    def _close_order(self, coin, a):
        """One completed aggressor order -> update the rolling bar, then test both sleeves' reach bars."""
        self.n_orders += 1
        if a["stale_ms"] >= self.FRESH_MS:                     # fresh-book filter, applied at ORDER START
            return
        reach = max(a["ext"], 0.0) / a["mid"] * 1e4
        buf = self.reach[coin]; buf.append(reach); self.since[coin] += 1
        if self.since[coin] >= self.REFRESH and len(buf) >= self.RMINC:
            self.thr[coin] = max(self.FLOOR, float(np.percentile(buf, self.PCT))); self.since[coin] = 0
        te = a["t0"]; adaptive = self.thr[coin]
        vol = self.m.vol_bps_min(coin)

        # The two sleeves are INDEPENDENT selections (research calls select() twice, each with its own reach
        # bar and its own per-token non-overlap). They are de-duplicated only where a BURST actually TRADES —
        # resolved at emission in poll(), NOT here. Doing it here would swallow the solo deep sweeps that are
        # most of DEEP: a candidate that clears the reach bar but fails the breadth gate is still DEEP's.
        if reach >= adaptive and te - self.last_cand[coin] >= self.NONOVL:
            self.last_cand[coin] = te; self.n_sweeps += 1      # non-overlapped BURST candidate
            while self.sweeps and self.sweeps[0][0] < te - self.BW:
                self.sweeps.popleft()
            breadth = sum(1 for (st, sc, sd) in self.sweeps if sc != coin and sd == a["dir"])
            self.sweeps.append((te, coin, a["dir"]))
            self.pending.append(dict(sleeve="BURST", ts=te, coin=coin, dir=a["dir"],
                                     reach=reach, breadth=breadth, vol=vol))
        if self.DEEP_ON and np.isfinite(vol) and vol <= self.VCEIL \
                and reach >= max(self.KVOL * vol, self.DEEP_FLOOR, adaptive) \
                and te - self.last_deep[coin] >= self.NONOVL:
            self.last_deep[coin] = te; self.n_deep += 1        # non-overlapped DEEP candidate
            self.pending.append(dict(sleeve="DEEP", ts=te, coin=coin, dir=a["dir"],
                                     reach=reach, breadth=0, vol=vol))

    # --- gates + emission -------------------------------------------------
    def poll(self, now_ns):
        """Return Signals for candidates whose cluster window has completed and whose gates pass."""
        self._flush_stale(now_ns)
        out = []
        burst_fired = set()          # (coin, ts) that BURST actually TRADED -> DEEP drops the duplicate
        # BURST is appended before DEEP for the same order, so list order already decides BURST first.
        for p in [p for p in self.pending if now_ns >= p["ts"] + int(self.CLUS * NS)]:
            self.pending.remove(p)
            te, coin, de = p["ts"], p["coin"], p["dir"]
            bk = self.m.book.get(coin)
            cl = [px for (tt, px, dd) in self.m.trades[coin]
                  if te <= tt <= te + int(self.CLUS * NS) and dd == de]
            span = max((((max(cl) - bk["ask"]) if de > 0 else (bk["bid"] - min(cl))) / bk["mid"] * 1e4),
                       p["reach"]) if (bk and cl) else p["reach"]
            if self.clock_hours and datetime.datetime.utcfromtimestamp(te / NS).hour not in self.clock_hours:
                continue
            if p["sleeve"] == "DEEP":                           # no breadth / sw_span gate (§AC.2)
                if (coin, te) not in burst_fired:               # BURST wins an exact duplicate
                    out.append(Signal(coin, de, 0, span, te, "DEEP", p["reach"], p["vol"]))
                continue
            self.swhist.append(span); self.swsince += 1         # the median tracks BURST candidates
            if self.swsince >= self.SWREF and len(self.swhist) >= self.SWMINC:
                self.swmed = float(np.median(self.swhist)); self.swsince = 0
            if p["breadth"] < self.MINK or span < self.swmed:
                continue
            q = self.fired[de]                                  # dedup: nmax entries / direction / cooldown
            while q and te - q[0] > self.COOL:
                q.popleft()
            if len(q) >= self.NMAX:
                continue
            q.append(te); burst_fired.add((coin, te))
            out.append(Signal(coin, de, p["breadth"], span, te, "BURST", p["reach"], p["vol"]))
        return out
