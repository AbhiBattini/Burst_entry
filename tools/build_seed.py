"""LIVE-ENG #2.1 — build the SEED for the live monitor so thresholds are correct from t0 (no cold warm-up).
Per-token p99.8 of FRESH reach (detection threshold seed) + global sw_span median (gate seed), from June archive.
Saves seed.json. Usage: python build_seed.py"""
import sys, json, glob
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
import pyarrow.parquet as pq
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "shared" / "data_cache"
DIRS = ["hl_mm_june", "hl_mm_june2"]        # both June weeks
TOKENS = ["XRP", "SUI", "DOGE", "LTC", "SOL", "TON", "ADA", "AVAX", "LINK", "BCH",
          "HYPE", "AAVE", "CRV", "INJ", "MORPHO", "XPL", "PUMP"]
FRESH_MS = 100.0

seed = {}
for tok in TOKENS:
    reaches = []
    for dd in DIRS:
        dest = str(BASE / dd)
        lf = sorted(glob.glob(dest + rf"\l2_{tok}_*.parquet")); ff = sorted(glob.glob(dest + rf"\fills_{tok}_*.parquet"))
        if not lf or not ff: continue
        L = pd.concat([pq.read_table(f).to_pandas() for f in lf], ignore_index=True).sort_values("ts_ns").reset_index(drop=True)
        L = L[L.bid_px.apply(len) > 0].reset_index(drop=True)
        if len(L) < 5000: continue
        lt = L.ts_ns.values.astype(np.int64)
        Lba = np.array([a[0] for a in L.ask_px]); Lbb = np.array([b[0] for b in L.bid_px]); LM = 0.5 * (Lba + Lbb)
        F = pd.concat([pq.read_table(f).to_pandas() for f in ff], ignore_index=True)
        F["ts_ns"] = pd.to_datetime(F["timestamp"], utc=True).dt.tz_localize(None).values.astype("datetime64[ns]").astype("int64")
        A = F[F.crossed].sort_values("ts_ns").reset_index(drop=True)
        at = A.ts_ns.values.astype(np.int64); ap = A.price.values.astype(float); d = np.where(A.side.values == "buy", 1.0, -1.0)
        ix = np.clip(np.searchsorted(lt, at, "right") - 1, 0, len(lt) - 1)
        reach = np.clip(np.where(d > 0, ap - Lba[ix], Lbb[ix] - ap) / LM[ix] * 1e4, 0, None)
        fresh = (at - lt[ix]) / 1e6 < FRESH_MS
        reaches.append(reach[fresh])
    if reaches:
        r = np.concatenate(reaches)
        seed[tok] = round(float(np.percentile(r, 99.8)), 2)
        print(f"{tok:>7}: p99.8 fresh reach {seed[tok]:6.1f} bps  (n_fresh {len(r):,})", flush=True)

sw = pd.concat([pd.read_parquet(HERE / f) for f in ["june_A_events.parquet", "june_A_events_wk2.parquet"]], ignore_index=True).sw_span
out = {"reach_p998": seed, "swspan_median": round(float(sw.median()), 2)}
(HERE / "seed.json").write_text(json.dumps(out, indent=2))
print(f"\nglobal sw_span median: {out['swspan_median']}  -> saved seed.json")
