"""Build seed.json — the per-token detection thresholds the live strategy warms from at t0.

Two quantities, both on the §AB.3 ORDER-AGGREGATED reach (NOT per-print reach — the old per-print seed was
built before the detector fix and under-stated the bar on fragmented tapes like HYPE):
  reach_p998[token]  seed for the rolling p99.8 order-reach bar  (live code uses max(floor, rolling p99.8))
  swspan_median      seed for the global trailing sw_span median (the BURST cluster-depth gate)

DEFAULT MODE (`--from-pool`, no args): derive both from the canonical research pool
`../A_book/june31_ladder.parquet`, which is already order-aggregated and non-overlap-selected. Per token we
take the MEDIAN of the rolling p99.8 values the research detector actually used. That is the same statistic
the live rolling buffer converges to, and it is free (no archive re-read).
  NB it is conditioned on candidate timestamps (active periods), so it is mildly BIASED HIGH. That is the
  safe direction for a seed: too-high = miss a few sweeps in the first minutes, too-low = fire on noise. The
  live rolling buffer overrides it after `detection.reach_min_count` fresh orders anyway.

FULL MODE (`--from-archive`): recompute from the raw HL cache (needs the USB at D:). Slower (~20 min) and
only worth it when the universe changes or the archive is refreshed.

Universe comes from `../A_book/universe.json` (single source of truth) — never hard-code a token list here.
Usage:  python tools/build_seed.py [--from-archive]
"""
import sys, json, glob
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent          # .../Github_for_live/tools
REPO = HERE.parent                              # .../Github_for_live
TRACK = REPO.parent                             # .../69_informed_sweep_follow
BASE = TRACK.parents[0] / "shared" / "data_cache"
POOL = TRACK / "A_book" / "june31_ladder.parquet"
UNI = json.loads((TRACK / "A_book" / "universe.json").read_text())["calm31"]["tokens"]
FRESH_MS = 100.0; GAPNS = 100_000_000; FIXED = 30.0; NS = 1_000_000_000


def canonical_swspan_median(P):
    """Reproduce combined_book.select(0.0): reach >= max(30, rolling p99.8), then non-overlapping per token.
    The gate seed is the MEDIAN sw_span of that canonical candidate set."""
    HOLD_NS = int((0.4 + 120) * NS)
    S = P[P.reach.values >= np.maximum(FIXED, P.p998.values)]
    keep = []
    for _, g in S.groupby("token"):
        busy = -1
        for i, t in zip(g.index.values, g.ts.values.astype(np.int64)):
            if t < busy: continue
            keep.append(i); busy = t + HOLD_NS
    return float(P.loc[keep].sw_span.median()), len(keep)


def from_pool():
    P = pd.read_parquet(POOL).sort_values("ts").reset_index(drop=True)
    seed = {}
    for tok in UNI:
        g = P[P.token == tok]
        if len(g) < 20:
            print(f"{tok:>9}: only {len(g)} candidates -> no seed (live warms from the floor)"); continue
        seed[tok] = round(float(np.median(g.p998.values)), 2)
        print(f"{tok:>9}: p99.8 order-reach seed {seed[tok]:6.1f} bps  (n_cand {len(g):,})")
    med, n = canonical_swspan_median(P)
    print(f"\ncanonical candidate set: {n:,} events -> sw_span median {med:.2f}")
    return seed, med


def from_archive():
    """Recompute p99.8 of FRESH, ORDER-AGGREGATED reach straight from the L2+fills archive."""
    seed = {}
    for tok in UNI:
        reaches = []
        for dd in ["hl_mm_june", "hl_mm_june2"]:
            dest = str(BASE / dd)
            lf = sorted(glob.glob(dest + rf"\l2_{tok}_*.parquet")); ff = sorted(glob.glob(dest + rf"\fills_{tok}_*.parquet"))
            if not lf or not ff: continue
            import pyarrow.parquet as pq
            L = pd.concat([pq.read_table(f).to_pandas() for f in lf], ignore_index=True).sort_values("ts_ns").reset_index(drop=True)
            L = L[L.bid_px.apply(len) > 0].reset_index(drop=True)
            if len(L) < 5000: continue
            lt = L.ts_ns.values.astype(np.int64)
            Lba = np.array([a[0] for a in L.ask_px]); Lbb = np.array([b[0] for b in L.bid_px]); LM = 0.5 * (Lba + Lbb)
            F = pd.concat([pq.read_table(f).to_pandas() for f in ff], ignore_index=True)
            F["ts_ns"] = pd.to_datetime(F["timestamp"], utc=True).dt.tz_localize(None).values.astype("datetime64[ns]").astype("int64")
            A = F[F.crossed].sort_values("ts_ns").reset_index(drop=True)
            at = A.ts_ns.values.astype(np.int64); ap = A.price.values.astype(float)
            d = np.where(A.side.values == "buy", 1.0, -1.0)
            ix = np.clip(np.searchsorted(lt, at, "right") - 1, 0, len(lt) - 1)
            # §AB.3: group consecutive same-side prints (gap <= 100ms) into ORDERS; reach = the order's
            # max penetration past the touch AT ORDER START.
            newg = np.ones(len(at), bool); newg[1:] = (np.diff(at) > GAPNS) | (d[1:] != d[:-1])
            gid = np.cumsum(newg) - 1; gs = np.where(newg)[0]; gix = ix[gs]
            ext = np.where(d[gs] > 0, pd.Series(ap).groupby(gid).max().values - Lba[gix],
                           Lbb[gix] - pd.Series(ap).groupby(gid).min().values)
            greach = np.clip(ext / LM[gix] * 1e4, 0, None)
            fresh = (at[gs] - lt[gix]) / 1e6 < FRESH_MS
            reaches.append(greach[fresh])
        if reaches:
            r = np.concatenate(reaches)
            seed[tok] = round(float(np.percentile(r, 99.8)), 2)
            print(f"{tok:>9}: p99.8 fresh ORDER reach {seed[tok]:6.1f} bps  (n_orders {len(r):,})", flush=True)
    med, _ = canonical_swspan_median(pd.read_parquet(POOL))
    return seed, med


seed, med = from_archive() if "--from-archive" in sys.argv else from_pool()
out = {"_doc": "Seeds the live rolling thresholds at t0. reach_p998 = per-token p99.8 of ORDER-aggregated "
                "reach (§AB.3); swspan_median = global BURST cluster-depth gate. Rebuild with tools/build_seed.py.",
       "_source": "june31_ladder.parquet (--from-archive to recompute from raw HL cache)",
       "reach_p998": seed, "swspan_median": round(med, 2)}
(REPO / "seed.json").write_text(json.dumps(out, indent=2))
print(f"\n{len(seed)} tokens seeded, sw_span median {out['swspan_median']} -> seed.json")
