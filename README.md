# Strategy A — live (Track 69)

A within-Hyperliquid **sweep-follow** book. An aggressor order eating deep through an HL L2 book predicts
short-horizon continuation; the edge lives in the **common factor** (several books swept the same way at once),
with a second, smaller sleeve that takes very deep single-book sweeps in quiet conditions. Taker in, ~120 s hold,
maker-improve exit, with depth-aware sizing and a 100 bps path stop.

Research and every honest caveat live in `../CLAUDE.md` (CURRENT STATE) and `../notes.md` (§Z–§AG); this folder is
the **deployable system** distilled from that.

> **Status: paper-first.** Ships in `mode: paper`. The evidence is 14 calm days + 2 cascade windows, maker fills
> are front-of-queue-optimistic, and Track 69 has deliberately **not** been promoted to the desk's deployed-book
> table. Run paper, reconcile against the backtest, then decide.

## The spec (encoded in `config.yaml`)

**One book, two sleeves, one shared gross cap.**

| | trigger | gates | capital |
|---|---|---|---|
| **BURST-ENTRY** (core) | order `reach ≥ max(30 bps, trailing p99.8)` | ≥1 other book swept same dir in prior 120 s **AND** `sw_span ≥` trailing median; dedup `nmax`/dir/120 s | may use the whole cap |
| **DEEP-FOLLOW** (overlay) | order `reach ≥ max(2.5 × own vol, 40 bps, trailing p99.8)` | none — but own trailing vol **≤ 10 bps/min** | capped at `cap × (1 − reserve)` |

- **Detector:** consecutive same-side prints within 100 ms are ONE aggressor **order**; reach is measured against
  the touch at order start. Both sleeves are non-overlapping per token over the position lifetime (~120 s).
  Where both qualify on the same order, BURST wins.
- **Execution:** taker entry ~0.4 s after the trigger, depth-aware size (largest notional filling within 30 bps of
  touch), 120 s hold, 100 bps path stop, maker post-only exit one tick inside the touch (60 s window, taker
  fallback).
- **Feed-health halt:** new entries stop while the rolling median l2Book lag exceeds `guards.max_feed_lag_ms`
  (400 ms — the entire reaction budget). Open positions keep managing their own exits, and entries resume on
  their own when lag recovers. In-region expect single-digit to ~50 ms.
- **Universe: 31 names** (HL top-40 perps by 24 h notional). BTC/ETH almost never fire a trade of their own but
  supply breadth for everyone else — monitor them anyway.

**What the research says NOT to do** (all tested): don't raise `breadth.min_k` as a cascade guard (breadth is
*elevated* in a cascade — it tilts you toward them and costs 58 % of calm PnL); don't add a per-direction sub-cap
or a market-wide vol breaker (redundant once the DEEP vol ceiling is on); don't raise per-trade size to earn more
(net bps 9.8 → 5.2 → 2.2 from $10 k → $50 k → $250 k). The lever that works is **more tokens**.

## Backtest reference (31 tokens, size-aware maker exit)

| window | cap | $/day | ex-tail $/day | days + |
|---|---|---:|---:|---:|
| CALM Jun-2026, 14 d | $10 k, nmax 1 | +$447.9 | **+$197.5** | 93 % |
| CALM Jun-2026, 14 d | $100 k, nmax 8 | +$992.8 | +$291.6 | 93 % |
| SEVERE Oct-2025, 5 d | $100 k | +$2,155.8 | −$79.3 | 80 % |
| MODERATE Nov-2025, 4 d | $100 k | +$2,053.2 | +$1,471.8 | 100 % |

Underwrite the **ex-tail** column. Sleeve split (calm, $10 k): BURST +$403/day, DEEP +$45/day — BURST carries the
book and is the only sleeve with positive body income in both regimes.

## Capital — the book sizes itself to the wallet

**You never edit dollar amounts.** `capital:` in `config.yaml` reads the wallet's equity and derives everything:

```
gross_cap  = equity × cap_frac                    (1.0 → at most 1× gross)
size_usd   = min(gross_cap, per_trade_cap_usd)    ← per-trade size STOPS at $10k
nmax       = clip(floor(gross_cap / size_usd), 1, nmax_max)
min_trade  = clip(gross_cap × 0.10, hl_min_order_usd, 500), capped at size_usd
daily_stop = equity × 0.05
```

So funding the wallet with $500 runs a $500 book; **add $9,500 and at the next flat refresh it runs a $10k book**
— no config edit, no restart. The rule reproduces all three published configs exactly, and the self-test pins it:

| equity | gross cap | per-trade | nmax | min trade |
|---|---:|---:|---:|---:|
| $500 | $500 | $500 | 1 | $50 |
| $10,000 | $10,000 | $10,000 | 1 | $500 |
| $100,000 | $100,000 | $10,000 | **8** | $500 |

**The non-linearity is the point.** Per-trade size stops growing at $10k because raising it buys headline and no
underwritable income (§AD: net bps 9.8 → 5.2 → 2.2 at $10k → $50k → $250k). Capital past that goes into *more
concurrent bets* (`nmax`), not bigger ones.

**Safety.** `max_equity_usd` (default $25k) is a hard ceiling applied before anything is derived, so a parse bug
or a stray deposit can't size the book past it — raise it deliberately when you fund more. `min_equity_usd` floors
it. Implausible readings are rejected and the last good sizing is kept. **Resizing only happens while flat**, so a
cap never moves under an open position. Paper mode, and any failed query, fall back to `paper_equity_usd`.

The equity query is **read-only and needs no private key** — just `HL_ACCOUNT_ADDRESS` — so paper mode can size
itself to your real wallet before any key is on the box.

At $500 the strategy is mechanically identical, just 1/20 the notional. **It's a wiring test, not an income test**:
at ~$1.4/trade one bad print or a missed reconnect dominates the P&L. What it validates is that live fills, fill
rates and trade counts (~24/day) match paper — not whether the edge is there.

## Quick start

```bash
./setup.sh                              # venv + deps + .env + a 90s paper smoke test
```

```bash
python tools/selftest.py                # offline logic check: aggregation, gates, sleeves, cap+reserve
```

```bash
python -m src.run --duration 90 --test  # 90s end-to-end against the live feed, lowered thresholds
```

```bash
python -m src.run                       # paper, runs until stopped
```

Install as a service with `./setup.sh --service`, then `journalctl -u strat-a -f`.

## Layout

```
config.yaml     all tunables (universe, thresholds, sizing, guards, endpoints, mode)  <- edit this
seed.json       per-token p99.8 ORDER reach + sw_span median, so thresholds warm from t0
src/            config.py  capital.py  strategy.py  execution.py  feed.py  recorder.py  run.py
tools/          build_seed.py (rebuild seed.json)   selftest.py (offline logic check)
systemd/        strat-a.service  (installed by setup.sh --service)
data/           recorder output (live trades + L2 snapshots; git-ignored)  <- free forward-OOS data
.env            secrets for live mode (git-ignored; copy from .env.example)
```

## Modes & safety

- **`mode: paper`** (default) — full simulation on the live feed. No orders, no keys, no risk. Writes
  `paper_trades.parquet` + logs. Run it for days and compare against the backtest before anything else.
- **`mode: live`** — real orders via the Hyperliquid SDK. **Read `src/execution.py::LiveExecution` and
  AGENTS.md §Live first.** Requires:
  1. `./setup.sh --live` (installs `hyperliquid-python-sdk`, `eth-account`),
  2. an **HL API/agent wallet** (create in the HL UI — it can trade but **cannot withdraw**; never put your main
     MetaMask key on a server), its key in `.env` as `HL_PRIVATE_KEY`,
  3. `live_safety.dry_run: true` first — it **logs the exact order it would place without sending**. Watch a full
     session, reconcile against paper, then flip it to `false` yourself.
  - Rails (all derived from equity): per-order notional cap, a 5 %-of-equity daily loss stop, and a
    killswitch file (`touch KILL` halts new entries).

## Latency / infra

Strategy compute is ~2–20 µs per trade (≈0.006 % of the ~200–900 ms HL round trip) — **latency is HL-gated, not
compute-gated.** HL is an L1 with no colocation; the win is being **in-region**: an AWS box in `ap-northeast-1`
(Tokyo), same region as HL's validators. Spending beyond a modest Tokyo VM buys almost nothing. Optional: a
redundant WS connection to cover the ~3 s reconnect gap.

## Data

The recorder tees all trades and ~2/s L2 snapshots per coin to `data/` in the research archive schema — free
forward-OOS data, and the only way this book gets more than 14 calm days behind it. Re-run `tools/build_seed.py`
periodically (the rolling buffers also adapt on their own).
