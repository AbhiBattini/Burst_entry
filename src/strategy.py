"""Signal engine — MarketState + StrategyA. Pure logic, no network/orders (see AGENTS.md §Detection..§Burst).
Maps 1:1 to the validated research: causal detection (reach>=max(30,trailing p99.8)/fresh<100ms), breadth
(other-book same-dir sweeps prior 120s), sw_span (0.4s forward cluster >= trailing median), burst-entry dedup.
Hot path is minimal: per-trade = dict lookup + reach + 1 compare vs a CACHED threshold (percentile amortized)."""
import datetime
from collections import deque, defaultdict
import numpy as np

NS = 1_000_000_000


class MarketState:
    """Live order-book + trade tape state, updated by the feed; read by strategy + execution."""
    def __init__(self, trade_buf=4000):
        self.book = {}       # coin -> dict(bid, ask, mid, t, bids=[(px,sz)], asks=[(px,sz)])
        self.tick = {}       # coin -> estimated price tick
        self.trades = defaultdict(lambda: deque(maxlen=trade_buf))   # coin -> (t_ns, px, dir)

    def update_l2(self, coin, bids, asks, t_ns):
        if not bids or not asks:
            return
        self.book[coin] = dict(bid=bids[0][0], ask=asks[0][0], mid=0.5 * (bids[0][0] + asks[0][0]),
                               t=t_ns, bids=bids, asks=asks)
        da = np.diff([p for p, _ in asks[:6]]); da = da[da > 0]
        if len(da):
            self.tick[coin] = float(da.min())

    def add_trade(self, coin, px, de, t_ns):
        self.trades[coin].append((t_ns, px, de))


class Signal:
    __slots__ = ("coin", "dir", "breadth", "sw_span", "ts")
    def __init__(self, coin, d, breadth, sw_span, ts):
        self.coin, self.dir, self.breadth, self.sw_span, self.ts = coin, d, breadth, sw_span, ts


class StrategyA:
    def __init__(self, cfg, seed, market):
        d, b, s, bu = cfg["detection"], cfg["breadth"], cfg["swspan"], cfg["burst"]
        self.m = market
        self.FLOOR, self.PCT, self.RBUF = d["reach_floor_bps"], d["reach_pctile"], d["reach_buf"]
        self.RMINC, self.REFRESH, self.FRESH_MS = d["reach_min_count"], d["reach_refresh"], d["fresh_ms"]
        self.BW, self.MINK = b["window_s"] * NS, b["min_k"]
        self.CLUS, self.SWMINC, self.SWREF = s["cluster_s"], s["min_count"], s["refresh"]
        self.COOL = bu["cooldown_s"] * NS
        self.reach = defaultdict(lambda: deque(maxlen=self.RBUF)); self.since = defaultdict(int)
        rp = seed.get("reach_p998", {}) or {}
        self.thr = {c: max(self.FLOOR, rp.get(c, self.FLOOR * 3)) for c in cfg["universe"]}   # seeded, warm from t0
        self.sweeps = deque(); self.swhist = deque(maxlen=s["hist_len"]); self.swsince = 0
        self.swmed = seed.get("swspan_median") or float("inf")
        self.pending = []; self.last_fire = {1.0: -1e18, -1.0: -1e18}
        self.clock_hours = set(cfg.get("clock", {}).get("restrict_utc_hours") or [])
        self.n_sweeps = 0

    def on_trade(self, coin, px, sz, de, t_ns):
        bk = self.m.book.get(coin)
        if bk is None:
            return
        self.m.add_trade(coin, px, de, t_ns)
        reach = (px - bk["ask"]) / bk["mid"] * 1e4 if de > 0 else (bk["bid"] - px) / bk["mid"] * 1e4
        if reach < 0:
            reach = 0.0
        if (t_ns - bk["t"]) / 1e6 < self.FRESH_MS:                       # fresh-book filter
            buf = self.reach[coin]; buf.append(reach); self.since[coin] += 1
            if self.since[coin] >= self.REFRESH and len(buf) >= self.RMINC:
                self.thr[coin] = max(self.FLOOR, float(np.percentile(buf, self.PCT))); self.since[coin] = 0
            if reach >= self.thr[coin]:                                  # SWEEP
                while self.sweeps and self.sweeps[0][0] < t_ns - self.BW:
                    self.sweeps.popleft()
                breadth = sum(1 for (st, sc, sd) in self.sweeps if sc != coin and sd == de)
                self.sweeps.append((t_ns, coin, de)); self.pending.append((t_ns, coin, de, reach, breadth))
                self.n_sweeps += 1

    def poll(self, now_ns):
        """Return burst-entry Signals for triggers whose 0.4s cluster has completed and gates pass."""
        out = []
        for p in [p for p in self.pending if now_ns >= p[0] + int(self.CLUS * NS)]:
            self.pending.remove(p); te, coin, de, reach, breadth = p
            bk = self.m.book.get(coin)
            cl = [px for (tt, px, dd) in self.m.trades[coin] if te <= tt <= te + int(self.CLUS * NS) and dd == de]
            span = max((((max(cl) - bk["ask"]) if de > 0 else (bk["bid"] - min(cl))) / bk["mid"] * 1e4), reach) if (bk and cl) else reach
            self.swhist.append(span); self.swsince += 1
            if self.swsince >= self.SWREF and len(self.swhist) >= self.SWMINC:
                self.swmed = float(np.median(self.swhist)); self.swsince = 0
            if self.clock_hours and datetime.datetime.utcfromtimestamp(te / NS).hour not in self.clock_hours:
                continue
            if breadth >= self.MINK and span >= self.swmed and te - self.last_fire[de] > self.COOL:
                self.last_fire[de] = te; out.append(Signal(coin, de, breadth, span, te))
        return out
