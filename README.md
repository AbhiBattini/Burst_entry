# Strategy A — live (Track 69)

A within-Hyperliquid **common-factor sweep-follow** ("burst-entry") strategy. When several HL order books get
swept the same direction at once (a market-wide burst), it follows the move: taker in, ~120s hold, maker-improve
exit, with depth-aware sizing + a 100 bps path stop. Research + validation live in `../notes.md` (§Z–§Z.13) and
`../CLAUDE.md`; this folder is the **deployable system** distilled from that.

> **Status:** paper-trading proven (6h live run: reconnect-hardened, maker-fill exit, ~+8 bps/trade realistic).
> **Ships in `mode: paper` by default. Live execution is a documented adapter you verify and enable yourself.**

## The deployable spec (encoded in `config.yaml`)
- **Detect** a sweep: `reach ≥ max(30 bps, trailing p99.8 of fresh reach)` per book, fresh book (< 100 ms).
- **Gate (burst-entry):** at least **1 other book** swept the **same direction** in the prior 120 s **AND**
  the 0.4 s cluster depth `sw_span ≥ trailing median`. Dedup to **1 entry / direction / 120 s**.
- **Enter** taker (~0.4 s reaction), **depth-aware size** ≤ $10 k within 30 bps of touch.
- **Hold** 120 s with a **100 bps path stop**. **Exit** maker post-only one tick inside the touch (60 s window,
  queue-aware fill, taker fallback). **Per-burst gross cap** = `max_open` concurrent positions.
- Edge concentrates 14–17 UTC and in market-wide bursts; set `clock.restrict_utc_hours` if you want to trade only then.

## Quick start (AWS Tokyo box)
```bash
# on a fresh Ubuntu box in ap-northeast-1 (Tokyo):
git clone <your-private-repo> && cd Github_for_live
./setup.sh                 # venv + deps + .env + a 90s paper smoke test
# review config.yaml (stays mode: paper), then run:
.venv/bin/python -m src.run            # runs forever (Ctrl-C to stop)
#   or as a service (runs on boot, auto-restart):
./setup.sh --service                   # installs systemd unit
journalctl -u strat-a -f               # follow logs
```

## Layout
```
config.yaml     all tunables (universe, thresholds, sizing, guards, endpoints, mode)  <- edit this
seed.json       precomputed per-token p99.8 reach + sw_span median (thresholds warm from t0)
src/            config.py  strategy.py  execution.py  feed.py  recorder.py  run.py
tools/          build_seed.py  (rebuild seed.json from archive data; optional)
systemd/        strat-a.service  (installed by setup.sh --service)
data/           recorder output (live trades + L2 snapshots; git-ignored)  ← free forward-OOS data
.env            secrets for live mode (git-ignored; copy from .env.example)
```

## Modes & safety
- **`mode: paper`** (default) — full simulation on the live feed. No orders, no keys, no risk. Writes
  `paper_trades.parquet` + logs. Run this first (ideally for days) and compare to the backtest.
- **`mode: live`** — real orders via the Hyperliquid SDK. **Read `src/execution.py::LiveExecution` and
  AGENTS.md §Live before enabling.** Requires:
  1. `./setup.sh --live` (installs `hyperliquid-python-sdk`, `eth-account`),
  2. an **HL API/agent wallet** (create in the HL UI — it can trade but **cannot withdraw**; never use your
     main MetaMask key on a server), its key in `.env` as `HL_PRIVATE_KEY`,
  3. `live_safety.dry_run: true` first — it **logs the exact order it would place without sending** — verify
     over a full session, then flip to `false` yourself.
  - Rails: `max_notional_usd`, `daily_loss_stop_usd`, and a **killswitch file** (`touch KILL` halts new entries).

## Latency / infra
The strategy compute is ~2–20 µs per trade (≈0.006 % of the ~200–900 ms HL round-trip) — **latency is HL-gated,
not compute-gated.** HL is an L1 (no colocation); the win is being **in-region**: an AWS box in **ap-northeast-1
(Tokyo)**, same region as HL's validators (AZ1/2/4), talking to `api.hyperliquid.xyz` (resolves to Tokyo infra).
Spending beyond a modest Tokyo VM buys almost nothing. Optional: a redundant WS connection to cover the ~3 s
reconnect gap.

## Data
The recorder tees all trades + ~2/s L2 snapshots per coin to `data/` in the research archive schema — this is
free forward-OOS data. Periodically re-run `tools/build_seed.py` (or let the rolling buffers adapt) to keep
thresholds current.
