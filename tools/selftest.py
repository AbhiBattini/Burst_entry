"""Offline self-test for the signal + rationing logic. No network, no orders, synthetic tape.

Checks the four things the 2026-08-28 spec rewrite changed, each of which is silent-failure-prone:
  1. §AB.3 order aggregation      — N fragmented prints inside agg_gap_ms become ONE order, and reach is
                                    measured against the touch AT ORDER START (not per print).
  2. BURST gates                  — breadth >= min_k and sw_span >= median; a SOLO sweep must NOT fire
                                    (solo is the net-negative subset — if solo fires, the edge is inverted).
  3. DEEP sleeve                  — fires on a deep sweep with no breadth, and is BLOCKED by the absolute
                                    vol ceiling (the load-bearing dial, §AC.4).
  4. Gross cap + reserve          — DEEP is limited to (cap - reserve) so it cannot crowd BURST out.

Run:  python tools/selftest.py        (exit code 0 = all pass)
"""
import sys, json, logging
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from src.strategy import MarketState, StrategyA
from src.execution import PaperExecution
from src.capital import CapitalManager

NS = 1_000_000_000
ROOT = Path(__file__).resolve().parents[1]
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def mk(coins, cap=500.0, size=500.0, deep_ceiling=10.0, nmax=1, vol_win=8, quiet=True, deep_on=True,
       max_open=99):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["universe"] = coins
    cfg["_root"] = ROOT
    cfg["detection"].update(reach_min_count=10 ** 9)          # never override the seed -> deterministic bar
    cfg["swspan"].update(min_count=10 ** 9)                   # ditto for the sw_span median
    cfg["deep"]["vol_ceiling_bps_min"] = deep_ceiling
    cfg["deep"]["enabled"] = deep_on
    cfg["burst"]["nmax"] = nmax
    # default max_open high so the cap/reserve tests exercise the DOLLAR cap, not the count cap
    cfg["execution"]["max_open"] = max_open
    seed = {"reach_p998": {c: 30.0 for c in coins}, "swspan_median": 30.0}
    m = MarketState(vol_grid_s=1, vol_win=vol_win)
    log = logging.getLogger("selftest"); log.addHandler(logging.NullHandler())
    if not quiet:
        log.setLevel(logging.INFO); log.addHandler(logging.StreamHandler(sys.stdout))
    st, ex = StrategyA(cfg, seed, m), PaperExecution(cfg, m, log)
    # sizing is DERIVED now: fund the test book with `cap` dollars of equity and let the rule size it
    cfg["capital"]["max_equity_usd"] = max(cfg["capital"]["max_equity_usd"], cap)
    params = CapitalManager(cfg, log).derive(cap)
    if nmax is not None:
        params = dict(params, nmax=nmax)
    st.apply_capital(params); ex.apply_capital(params)
    return cfg, st, m, ex


def book(m, coin, t, mid=100.0, half=0.005, depth=1e6):
    """Flat 5-level book around `mid`; `depth` = $ per level (big enough that sizing never binds)."""
    bid, ask = mid - half, mid + half
    m.update_l2(coin, [(bid - i * half, depth / mid) for i in range(5)],
                [(ask + i * half, depth / mid) for i in range(5)], t)


def warm_vol(m, coins, t0, n=20, mid=100.0):
    """Feed a quiet 1s-grid book so trailing vol is finite and LOW (well under the ceiling)."""
    for i in range(n):
        for c in coins:
            book(m, c, t0 + i * NS, mid=mid * (1 + 1e-9 * i))
    return t0 + n * NS


def sweep(s, m, coin, t, de, bps, nprints=8, spread_ns=5_000_000, mid=100.0):
    """One aggressor ORDER split into `nprints` fragments `spread_ns` apart (all inside agg_gap_ms)."""
    book(m, coin, t - 1_000_000, mid=mid)
    touch = m.book[coin]["ask"] if de > 0 else m.book[coin]["bid"]
    for i in range(nprints):
        frac = (i + 1) / nprints
        px = touch * (1 + de * frac * bps / 1e4)
        s.on_trade(coin, px, 1.0, de, t + i * spread_ns)
    return t + nprints * spread_ns


# ── 1. order aggregation ────────────────────────────────────────────────────────────────────────────
print("\n1. §AB.3 order aggregation")
COINS = ["AAA", "BBB", "CCC"]
cfg, s, m, ex = mk(COINS)
t = warm_vol(m, COINS, NS * 10_000)
t = sweep(s, m, "AAA", t + NS, +1, 60.0, nprints=12)
s.poll(t + NS)                                                # flush the aggregation
check("12 fragmented prints -> 1 order", s.n_orders == 1, f"n_orders={s.n_orders}")
check("reach measured from order-start touch", len(s.reach["AAA"]) == 1 and 55 < s.reach["AAA"][0] < 65,
      f"reach={list(s.reach['AAA'])}")

# ── 2. BURST gates: solo must NOT fire, breadth>=1 must ─────────────────────────────────────────────
print("\n2. BURST gates (the common factor is the edge; solo is the losing subset)")
# DEEP off throughout this section so these assert BURST behaviour ONLY — a solo deep sweep in a quiet book
# SHOULD fire DEEP, which is what §3 checks. Leaving DEEP on here tests two sleeves and isolates neither.
cfg, s, m, ex = mk(COINS, deep_on=False)
t = warm_vol(m, COINS, NS * 10_000)
t = sweep(s, m, "AAA", t + NS, +1, 60.0)                      # solo sweep — no other book swept
sigs = s.poll(t + NS)
check("solo sweep does NOT fire BURST", len(sigs) == 0, f"{len(sigs)} signals")

cfg, s, m, ex = mk(COINS, deep_on=False)
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 60.0); s.poll(t1 + NS)    # book 1 sweeps up (builds breadth)
t2 = sweep(s, m, "BBB", t1 + NS, +1, 60.0)
sigs = s.poll(t2 + NS)
check("2nd same-dir book fires BURST", len(sigs) == 1 and sigs[0].sleeve == "BURST" and sigs[0].breadth >= 1,
      f"{[(x.sleeve, x.coin, x.breadth) for x in sigs]}")

cfg, s, m, ex = mk(COINS, deep_on=False)
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 60.0); s.poll(t1 + NS)
t2 = sweep(s, m, "BBB", t1 + NS, -1, 60.0)                    # OPPOSITE direction -> no breadth
sigs = s.poll(t2 + NS)
check("opposite-direction sweep gives no breadth", len(sigs) == 0, f"{len(sigs)} signals")

# ── 3. DEEP sleeve + the absolute vol ceiling ───────────────────────────────────────────────────────
print("\n3. DEEP sleeve (overlay) and the §AC.4 absolute vol ceiling")
cfg, s, m, ex = mk(COINS, deep_ceiling=1e9)                   # ceiling effectively off
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 60.0)                     # solo, but reach 60 >= DEEP floor 40
sigs = s.poll(t1 + NS)
check("solo deep sweep fires DEEP", len(sigs) == 1 and sigs[0].sleeve == "DEEP",
      f"{[(x.sleeve, x.reach) for x in sigs]}")

cfg, s, m, ex = mk(COINS, deep_ceiling=1e9)
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 35.0)                     # 35 bps: over BURST floor, under DEEP floor 40
sigs = s.poll(t1 + NS)
check("shallow sweep (35bps) does NOT fire DEEP", len(sigs) == 0, f"{len(sigs)} signals")

cfg, s, m, ex = mk(COINS, deep_ceiling=0.0)                   # ceiling at 0 -> nothing can pass
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 60.0)
sigs = s.poll(t1 + NS)
check("vol ceiling blocks DEEP", len(sigs) == 0, f"{len(sigs)} signals")

# a deep sweep WITH breadth qualifies for both sleeves — BURST must win, exactly one entry
cfg, s, m, ex = mk(COINS, deep_ceiling=1e9)
t = warm_vol(m, COINS, NS * 10_000)
t1 = sweep(s, m, "AAA", t + NS, +1, 60.0); s.poll(t1 + NS)
t2 = sweep(s, m, "BBB", t1 + NS, +1, 60.0)
sigs = s.poll(t2 + NS)
check("BURST wins an exact duplicate", len(sigs) == 1 and sigs[0].sleeve == "BURST",
      f"{[(x.sleeve, x.coin) for x in sigs]}")

# ── 4. gross cap + BURST reserve ────────────────────────────────────────────────────────────────────
print("\n4. shared gross cap + 25% BURST reserve (§AC.2)")
cfg, s, m, ex = mk(COINS, cap=1000.0, size=1000.0)
check("BURST may use the whole cap", ex.room_for("BURST") == 1000.0, f"${ex.room_for('BURST'):,.0f}")
check("DEEP capped at cap - reserve", ex.room_for("DEEP") == 750.0, f"${ex.room_for('DEEP'):,.0f}")


class Sig:                                                    # minimal Signal stand-in for the exec test
    def __init__(self, coin, d, sleeve):
        self.coin, self.dir, self.sleeve = coin, d, sleeve
        self.breadth, self.sw_span, self.ts, self.reach, self.vol = 1, 40.0, 0, 60.0, 1.0


t = NS * 10_000
book(m, "AAA", t); book(m, "BBB", t)
ex._now = lambda: t
ex.on_signal(Sig("AAA", 1.0, "DEEP"))
open_sz = sum(p["size"] for p in ex.positions if p["status"] != "closed")
check("DEEP sized to the reserve limit", abs(open_sz - 750.0) < 1e-6, f"${open_sz:,.0f}")
ex.on_signal(Sig("BBB", 1.0, "BURST"))
sizes = [p["size"] for p in ex.positions if p["status"] != "closed"]
check("BURST still gets the reserved $250", len(sizes) == 2 and abs(sizes[1] - 250.0) < 1e-6, f"{sizes}")
ex.on_signal(Sig("AAA", 1.0, "DEEP"))
n_funded = len([p for p in ex.positions if p["status"] != "closed" and not p.get("shadow")])
check("gross cap is hard (3rd entry refused on $)", n_funded == 2, f"{n_funded} funded positions")

# max_open is a SECOND, independent rail — check it binds on its own when it is the tighter one
cfg2, s2, m2, ex2 = mk(COINS, cap=100_000.0, size=100_000.0, max_open=1)
t2 = NS * 10_000
book(m2, "AAA", t2); book(m2, "BBB", t2)
ex2._now = lambda: t2
ex2.on_signal(Sig("AAA", 1.0, "BURST")); ex2.on_signal(Sig("BBB", 1.0, "BURST"))
n_open2 = len([p for p in ex2.positions if p["status"] != "closed"])
check("max_open binds independently of the $ cap", n_open2 == 1, f"{n_open2} positions")

# ── 5. capital derivation ───────────────────────────────────────────────────────────────────────────
print("\n5. capital derivation from wallet equity (must reproduce the published configs)")
from src.capital import CapitalManager

cfg_c = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
cfg_c["capital"]["max_equity_usd"] = 1e9                      # lift the ceiling so the rule itself is tested
log_c = logging.getLogger("cap"); log_c.addHandler(logging.NullHandler())
cm = CapitalManager(cfg_c, log_c)

# (equity, expected cap, size, nmax, min_trade) — the three configs the research actually published
for eq, cap_, size_, nmax_, mt_ in [(500, 500, 500, 1, 50),
                                    (10_000, 10_000, 10_000, 1, 500),
                                    (100_000, 100_000, 10_000, 8, 500)]:
    p = cm.derive(eq)
    ok = (abs(p["gross_cap_usd"] - cap_) < 1e-6 and abs(p["size_usd"] - size_) < 1e-6
          and p["nmax"] == nmax_ and abs(p["min_trade_usd"] - mt_) < 1e-6)
    check(f"${eq:,} -> cap ${cap_:,} size ${size_:,} nmax {nmax_} minq ${mt_}", ok,
          f"got cap ${p['gross_cap_usd']:,.0f} size ${p['size_usd']:,.0f} nmax {p['nmax']} "
          f"minq ${p['min_trade_usd']:,.0f}")

# per-trade size must STOP growing at the §AD cap; the extra capital goes into nmax instead
p50 = cm.derive(50_000)
check("per-trade size capped at $10k (§AD)", abs(p50["size_usd"] - 10_000) < 1e-6, f"${p50['size_usd']:,.0f}")
check("extra capital raises nmax, not size", p50["nmax"] == 5, f"nmax={p50['nmax']}")
check("nmax never exceeds nmax_max", cm.derive(10_000_000)["nmax"] == cfg_c["capital"]["nmax_max"])

# the safety rails
cm2 = CapitalManager(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")), log_c)
big = cm2.derive(10_000_000)
check("max_equity_usd is a HARD ceiling", abs(big["equity"] - cm2.max_eq) < 1e-6, f"${big['equity']:,.0f}")
check("min_equity_usd blocks trading", cm2.derive(50)["tradable"] is False)
check("daily stop is 5% of equity (§AF)", abs(cm2.derive(500)["daily_loss_stop_usd"] - 25.0) < 1e-6)
check("min trade never exceeds one trade's size", cm2.derive(120)["min_trade_usd"] <= cm2.derive(120)["size_usd"])

# a resize must be adopted by BOTH the strategy (nmax) and the execution (cap/size/minq/reserve)
cfg_s, s_s, m_s, ex_s = mk(COINS)
ex_s.apply_capital(cm.derive(500)); s_s.apply_capital(cm.derive(500))
before = (ex_s.CAP, ex_s.SIZE, ex_s.MINQ, ex_s.RESERVE, s_s.NMAX)
ex_s.apply_capital(cm.derive(10_000)); s_s.apply_capital(cm.derive(10_000))
after = (ex_s.CAP, ex_s.SIZE, ex_s.MINQ, ex_s.RESERVE, s_s.NMAX)
check("resize propagates to execution + strategy", before == (500, 500, 50, 125, 1) and after == (10_000, 10_000, 500, 2_500, 1),
      f"{before} -> {after}")
check("book reports flat/not-flat for the resize gate", ex_s.is_flat() is True)

# ── 6. feed-latency guard ───────────────────────────────────────────────────────────────────────────
print("\n6. feed-latency halt (guards.max_feed_lag_ms)")
cfg6, s6, m6, ex6 = mk(COINS, cap=10_000.0, size=10_000.0)
MAXLAG = cfg6["guards"]["max_feed_lag_ms"]
T0 = 1_000_000_000_000
check("lag is None until warm (guard permissive)", m6.feed_lag_ms(50) is None)
for _ in range(60):
    m6.note_lag(T0, T0 - 300_000_000)   # ~300ms = the MEASURED in-region baseline (HL publish floor)
check("healthy feed (~300ms baseline) is under the halt", m6.feed_lag_ms(50) <= MAXLAG,
      f"{m6.feed_lag_ms(50):.0f}ms vs halt {MAXLAG}ms")

m7 = MarketState(vol_grid_s=1, vol_win=8)
for _ in range(60):
    m7.note_lag(T0, T0 - 2_000_000_000)                                # 2s lag = a real fault
check("stale feed exceeds the threshold", m7.feed_lag_ms(50) > MAXLAG, f"{m7.feed_lag_ms(50):.0f}ms")

# one huge outlier must NOT trip it (median, not mean)
m8 = MarketState(vol_grid_s=1, vol_win=8)
for _ in range(59):
    m8.note_lag(T0, T0 - 300_000_000)
m8.note_lag(T0, T0 - 60_000_000_000)                                   # a 60s GC-pause outlier
check("single outlier does not trip the halt", m8.feed_lag_ms(50) <= MAXLAG, f"{m8.feed_lag_ms(50):.0f}ms")

t6 = NS * 10_000
book(m6, "AAA", t6); ex6._now = lambda: t6
ex6.feed_ok = False
ex6.on_signal(Sig("AAA", 1.0, "BURST"))
check("halted feed refuses new entries", len(ex6.positions) == 0, f"{len(ex6.positions)} positions")
ex6.feed_ok = True
ex6.on_signal(Sig("AAA", 1.0, "BURST"))
check("recovered feed resumes entries", len(ex6.positions) == 1, f"{len(ex6.positions)} positions")

# ── 7. shadow book (opportunity cost of capital rationing) ──────────────────────────────────────────
print("\n7. shadow book for capital-skipped signals")
cfg7, s7, m7b, ex7 = mk(COINS, cap=1000.0, size=1000.0)
t7 = NS * 10_000
book(m7b, "AAA", t7); book(m7b, "BBB", t7); ex7._now = lambda: t7
ex7.on_signal(Sig("AAA", 1.0, "BURST"))                     # funded: consumes the whole cap
real = [p for p in ex7.positions if not p.get("shadow")]
check("first signal is funded", len(real) == 1, f"{len(real)} real")
ex7.on_signal(Sig("BBB", 1.0, "BURST"))                     # no room -> shadow
shadows = [p for p in ex7.positions if p.get("shadow")]
check("skipped signal becomes a shadow", len(shadows) == 1, f"{len(shadows)} shadow")
check("shadow consumes NO capital", abs(ex7.room_for("BURST")) < 1e-6,
      f"room ${ex7.room_for('BURST'):,.0f} (negative would mean shadows are counted)")
check("a real open position still blocks a resize", ex7.is_flat() is False)
for p_ in real:
    p_["status"] = "closed"
check("book reads flat once only shadows remain", ex7.is_flat() is True)

n_real_before = len(ex7.closed)
ex7._close(shadows[0], shadows[0]["entry_px"] * 1.001, "maker")
check("shadow close does NOT enter traded P&L", len(ex7.closed) == n_real_before, f"{len(ex7.closed)}")
check("shadow close lands in the shadow book", len(ex7.shadow_closed) == 1, f"{len(ex7.shadow_closed)}")

# must be impossible in live mode, where poll() sends real orders
import src.execution as _ex
cfg_live = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")); cfg_live["_root"] = ROOT
cfg_live["live_safety"]["dry_run"] = True
lg = logging.getLogger("selftest2"); lg.addHandler(logging.NullHandler())
check("LiveExecution never shadows", _ex.LiveExecution(cfg_live, m7b, lg).SHADOW is False)

# ── 8. entry drift: measurement + anti-chase guard ──────────────────────────────────────────────────
print("\n8. entry drift (implementation shortfall / front-running measure)")
cfg8, s8, m8, ex8 = mk(COINS, cap=10_000.0, size=10_000.0)
t8 = NS * 10_000
book(m8, "AAA", t8, mid=100.0); ex8._now = lambda: t8


class DriftSig(Sig):
    def __init__(self, coin, d, sleeve, ref):
        super().__init__(coin, d, sleeve); self.ref_px = ref


check("no move since trigger -> ~0 drift",
      abs(ex8.entry_drift_bps(DriftSig("AAA", 1.0, "BURST", 100.0), m8.book["AAA"])) < 1e-6)
d_up = ex8.entry_drift_bps(DriftSig("AAA", 1.0, "BURST", 99.5), m8.book["AAA"])
check("mid ran our way -> POSITIVE drift (we are chasing)", d_up > 0, f"{d_up:+.1f}bps")
d_dn = ex8.entry_drift_bps(DriftSig("AAA", -1.0, "BURST", 99.5), m8.book["AAA"])
check("same move, opposite side -> NEGATIVE drift", d_dn < 0, f"{d_dn:+.1f}bps")
check("drift is sign-symmetric", abs(d_up + d_dn) < 1e-6, f"{d_up:+.2f} vs {d_dn:+.2f}")

ex8.MAXDRIFT = 20.0
ex8.on_signal(DriftSig("AAA", 1.0, "BURST", 99.5))          # ~50bps of chase, over the 20bps bar
check("guard aborts a chased entry", len(ex8.positions) == 0, f"{len(ex8.positions)} positions")
ex8.MAXDRIFT = 500.0
ex8.on_signal(DriftSig("AAA", 1.0, "BURST", 99.5))
check("loose guard allows it", len(ex8.positions) == 1, f"{len(ex8.positions)} positions")
check("drift + slip recorded on the position",
      "drift_bps" in ex8.positions[0] and "slip_bps" in ex8.positions[0])
ex8._close(ex8.positions[0], ex8.positions[0]["entry_px"], "maker")
check("drift + slip survive onto the closed trade",
      "drift_bps" in ex8.closed[0] and "slip_bps" in ex8.closed[0], f"{sorted(ex8.closed[0])[:4]}...")

# ── 9. stale depth ladder must not be sized against ─────────────────────────────────────────────────
print("\n9. stale-ladder guard (the primary cascade guard reads the ladder)")
cfg9, s9, m9, ex9 = mk(COINS, cap=10_000.0, size=10_000.0)
t9 = NS * 10_000
book(m9, "AAA", t9, mid=100.0); ex9._now = lambda: t9
bk9 = m9.book["AAA"]
check("fresh ladder is usable", ex9._size_for(Sig("AAA", 1.0, "BURST"), bk9) > 0)

# advance ONLY the touch (as bbo does), leaving the ladder behind -- exactly the public-feed situation
m9.update_bbo("AAA", 99.995, 10.0, 100.005, 10.0, t9 + 20 * NS)
age_ms = (bk9["t"] - bk9["t_ladder"]) / 1e6
check("ladder ages while the touch moves on", age_ms > ex9.MAXLADDER_MS, f"{age_ms:.0f}ms")
check("stale ladder refuses to size", ex9._size_for(Sig("AAA", 1.0, "BURST"), bk9) == 0.0)
check("stale-ladder skips are counted", ex9.n_stale_ladder == 1, f"{ex9.n_stale_ladder}")

# a stale ladder is NOT a capital problem -- it must not be logged as opportunity cost
n_shadow_before = len([p for p in ex9.positions if p.get("shadow")])
ex9.on_signal(Sig("AAA", 1.0, "BURST"))
check("stale ladder does NOT create a shadow (not a capital skip)",
      len([p for p in ex9.positions if p.get("shadow")]) == n_shadow_before)

# ── 10. per-token rolling sw_span median (§AH.15) ───────────────────────────────────────────────────
print("\n10. sw_span threshold is PER TOKEN and causal")
cfgA = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
cfgA["universe"] = COINS; cfgA["_root"] = ROOT
cfgA["detection"].update(reach_min_count=10 ** 9)
cfgA["swspan"].update(win=4, min_count=2)
cfgA["deep"]["enabled"] = False   # DEEP has no sw_span gate; it would mask this test
cfgA["breadth"]["min_k"] = 0      # isolate sw_span: breadth is a separate gate, tested in §2
seedA = {"reach_p998": {c: 30.0 for c in COINS},
         "swspan_median": 999.0,                      # global fallback deliberately unreachable
         "swspan_median_by_token": {"AAA": 10.0, "BBB": 90.0}}
mA = MarketState(vol_grid_s=1, vol_win=8)
lgA = logging.getLogger("st10"); lgA.addHandler(logging.NullHandler())
sA = StrategyA(cfgA, seedA, mA)
check("per-token seeds are loaded", sA.swseed == {"AAA": 10.0, "BBB": 90.0}, f"{sA.swseed}")
check("global median kept only as fallback", sA.swmed_global == 999.0)

# each token must use ITS OWN seed, not one global number
t = warm_vol(mA, COINS, NS * 10_000)
t1 = sweep(sA, mA, "AAA", t + NS, +1, 60.0); sigA = sA.poll(t1 + NS)   # 60bps span vs AAA seed 10 -> fires
t2 = sweep(sA, mA, "BBB", t1 + NS, +1, 60.0); sigB = sA.poll(t2 + NS)  # 60bps span vs BBB seed 90 -> blocked
check("low-seed token fires on a 60bps span",
      any(s.coin == "AAA" and s.sleeve == "BURST" for s in sigA), f"{[(s.coin,s.sleeve) for s in sigA]}")
check("high-seed token is blocked by ITS OWN seed",
      not any(s.coin == "BBB" and s.sleeve == "BURST" for s in sigB), f"{[(s.coin,s.sleeve) for s in sigB]}")

# the threshold must come from PRIOR observations only, never the current one
cfgB = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
cfgB["universe"] = COINS; cfgB["_root"] = ROOT
cfgB["detection"].update(reach_min_count=10 ** 9)
cfgB["swspan"].update(win=10, min_count=2)
cfgB["deep"]["enabled"] = False
cfgB["breadth"]["min_k"] = 0
mB = MarketState(vol_grid_s=1, vol_win=8)
sB = StrategyA(cfgB, {"reach_p998": {c: 30.0 for c in COINS}, "swspan_median": 40.0}, mB)
sB.swhist["AAA"].extend([10.0, 20.0])                # own history -> median 15
tb = warm_vol(mB, COINS, NS * 10_000)
tb1 = sweep(sB, mB, "AAA", tb + NS, +1, 60.0)
n_before = len(sB.swhist["AAA"])
sigC = sB.poll(tb1 + NS)
check("uses own rolling median once warm (fires at 60 vs 15)",
      any(s.coin == "AAA" and s.sleeve == "BURST" for s in sigC), f"{[(s.coin,s.sleeve) for s in sigC]}")
check("observation is recorded AFTER the test (causal)", len(sB.swhist["AAA"]) == n_before + 1,
      f"{n_before} -> {len(sB.swhist['AAA'])}")
check("window is bounded", sB.swhist["AAA"].maxlen == 10, f"{sB.swhist['AAA'].maxlen}")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
