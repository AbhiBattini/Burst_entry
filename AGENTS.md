# AGENTS.md — guide for future agents editing this repo

You are looking at the **deployable distillation of Track 69 Strategy A**. The research, every design decision,
and the honest caveats are in `../notes.md` (§Z–§Z.13, LIVE-ENG #1–#3) and `../CLAUDE.md`. **Read the §Z banner
in `../CLAUDE.md` before changing strategy logic** — most "improvements" were already tested and killed.

## What this strategy is (one paragraph)
Follow the deep, market-wide sweeps on Hyperliquid. A sweep = an aggressor eating through the L2 book
(`reach ≥ max(30, trailing p99.8)`). The edge lives in the **common factor**: when ≥1 other book sweeps the same
way in the prior 120 s (breadth), continuation is real; **solo sweeps are net-negative**. Enter taker, ride ~120 s,
exit maker. Validated OOS over two June weeks (burst-entry ~+8 bps realistic, CI clears zero) and cascade-robust
with guards. It is NOT BTC beta; it is self-exciting (once ≥3 books fire, the next comes in ~7 s, ~90 % same dir).

## Architecture (data flow)
```
HLFeed.stream()  --msg-->  run.main loop
   l2Book  -> MarketState.update_l2   (book + tick per coin)         + Recorder.l2_snap
   trades  -> StrategyA.on_trade      (detect sweep -> pending)      + Paper/LiveExecution.on_trade (exit fills)
                                                                     + Recorder.trade
   every loop: StrategyA.poll(now) -> [Signal]  -> Execution.on_signal   (open position)
               Execution.poll(now)                                    (stop / post exit / close)
               Recorder.maybe_flush()
```
- `src/strategy.py` — **the signal engine** (pure logic). `MarketState` (book/trades), `StrategyA` (detection,
  breadth, sw_span, burst-entry dedup) emitting `Signal`. This is the heart; changes here change the strategy.
- `src/execution.py` — `PaperExecution` (proven simulator: entry walk, 100 bps stop, maker-improve queue-aware
  exit, per-burst cap) and `LiveExecution` (real orders, gated by `live_safety`; see §Live).
- `src/feed.py` — reconnect-hardened HL WS async stream.
- `src/recorder.py` — tees trades + L2 to parquet (archive schema).
- `src/run.py` — orchestrator + CLI (`--duration`, `--test`).
- `src/config.py` — loads `config.yaml` + `seed.json`. **All tunables are in `config.yaml`** — do not hard-code.

## How the code maps to the research (so you don't re-derive)
| config / code | research | note |
|---|---|---|
| `detection.reach_floor_bps: 30` | §Z.1, LIVE-ENG #1 | post-gate break-even ≈10; 30 is the deployable trigger. The floor is load-bearing; a pure percentile fails OOS. |
| `detection.reach_pctile: 99.8` + seed | §L, #2.1 | tight books floor at 30, wide books adaptive (seed.json). |
| `breadth.min_k: 1` | §O, §Z.9 | the edge IS the common factor; solo (breadth 0) is net-negative. `min_k` is the burst-entry threshold. |
| burst dedup `cooldown_s: 120` | §Z.9 | one entry/burst > per-sweep once you respect the per-burst gross cap (§Z.8). |
| `swspan` gate + rolling median | §K, LIVE-ENG #1 | causal trailing GLOBAL median reproduces the in-sample gate (calm +15.4, cascade +$2,149). |
| maker-improve exit, `hold 120 / exitwin 60` | §Z.3/Z.4/Z.6 | queue-jump barely helps, ladder HURTS (momentum≠reversion); a single patient improve-quote is best. |
| `stop_bps: 100` + depth-aware size | §R.1, §Z.12/Z.13 | the two guards make A cascade-robust; queue-sim confirms the crash profit is real. |
| `max_open` (per-burst gross cap) | §Z.8 | events 26× clustered, 94% same-dir → size gross-directional per burst, not per name. |

## Invariants — do NOT break these
1. **Causal only.** Every threshold uses trailing/seeded data (no future). The in-sample median was replaced by a
   rolling median for exactly this reason (LIVE-ENG #1). If you add a feature, it must be computable live.
2. **Fresh-book filter.** `fresh_ms` guards against stale books — including the seconds after a reconnect. Keep it.
3. **Hot path stays cheap.** Per-trade work = dict lookup + reach + one compare vs a *cached* threshold. Percentile/
   median recompute is amortized (`reach_refresh`, `swspan.refresh`). Measured ~2–20 µs (0.006 % of budget) — do
   not move an O(n) computation into `on_trade`.
4. **Config-driven.** New knobs go in `config.yaml`, read via `cfg`. No magic numbers in code.
5. **The execution boundary.** Do NOT hard-code keys, do NOT weaken `live_safety`, do NOT flip `dry_run: false`
   on live order code you haven't verified over a full `dry_run` session against your SDK version. Default mode
   stays `paper`. The exit is reduce-only + post-only and the stop/fallback are reduce-only market closes — keep
   them reduce-only so an exit can never open or flip a position.

## Live execution — what's done vs what you must verify (§Live)
`LiveExecution` now runs the **full open-position lifecycle** with real orders: **entry** (`market_open`),
**maker-improve exit** (post-only `Alo` reduce-only limit at the improve level), **taker-fallback** at the
exit window end (`market_close`), **100 bps path stop** (cancel resting exit + `market_close`), and **real
fill polling** of the resting exit (`Info.query_order_by_oid`, throttled by `live_safety.fill_poll_ms`).
Realized closes feed `daily_loss_stop`. `poll()` is a **full reimplementation** of the paper timing (not
inherited) so the exact live code path runs under `dry_run: true` too — dry_run logs every intended order and
models fills off the paper tape, so a dry session reconciles 1:1 against `PaperExecution`.

**What you MUST still verify before `dry_run: false`** (these move real money and cannot be validated here):
1. **SDK method signatures** for YOUR `hyperliquid-python-sdk` version — they drift release-to-release. The
   wire-touching code is isolated to the `_live_*` helpers + the `_avg_px`/`_oid` response parsers; audit those.
   The four calls: `ex.market_open(coin,is_buy,sz,None,slippage)`, `ex.order(coin,is_buy,sz,px,{"limit":{"tif":"Alo"}},reduce_only=True)`,
   `ex.cancel(coin,oid)`, `ex.market_close(coin)`, and `info.query_order_by_oid(addr,oid)`.
2. **Response shapes** — `_avg_px`/`_oid`/`_live_exit_fill_frac` parse `response.data.statuses[*]` / the order
   query defensively and fall back to a reference px with a WARNING; confirm the real shapes and remove the
   guesswork. A failed fill query returns 0 (never fakes a fill) — good, but means a persistently-failing query
   will ride to the taker-fallback at the window end. Watch for `[exit poll ERR]`/`[parse]` in the log.
3. **Restart reconciliation** — `LiveExecution.reconcile_on_start()` runs once on boot (called by `run.py`
   before the feed loop). It cancels stray resting orders and **flattens** any pre-existing position on
   `cfg.universe` (the strategy horizon is ~120 s, so anything that outlived a restart is stale). It does NOT
   adopt a position back into the state machine (entry_ts/px/breadth are unrecoverable). Knobs:
   `live_safety.reconcile_on_start` (default true) and `reconcile_mode` (`flatten` | `report`). **For the very
   first live boot, set `reconcile_mode: report`** to see what it would touch before it sends anything. If the
   account queries fail it raises (fail-closed: don't trade blind to account state). Verify `info.open_orders`
   and `info.user_state` shapes for your SDK version.
Process: implement/verify in `LiveExecution`, run `dry_run: true`, watch a full session, reconcile against the
paper sim, then flip live yourself.

## Common edits
- **Universe:** edit `config.yaml: universe` (and rebuild seed for new tokens: `tools/build_seed.py`).
- **Trade only US session:** `clock.restrict_utc_hours: [14,15,16,17]`.
- **More/less selective:** raise `breadth.min_k` (≥3 = only big bursts) or shorten the sw_span median window.
- **Rebuild thresholds:** `tools/build_seed.py` needs archive data (per-token L2+fills) — see the script header.
- **Test the whole path fast:** `python -m src.run --duration 90 --test` (lowered thresholds fire in ~90 s).

## Known caveats (from the research — keep them in mind)
- Validated on ~2 early-June weeks + 1 cascade; fills are front-of-queue/congestion-optimistic (paper ≈ 0.55×
  in calm, but ≈1× in a cascade — §Z.13). Signal real; net is thin (~+4–8 bps) and execution-gated.
- Reconnect leaves a ~3 s gap (could miss a burst). Consider a redundant WS connection for production.
- Breadth-vs-sw_span relative weight wobbles week to week — don't over-fit either on one window.
