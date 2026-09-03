# AGENTS.md — guide for future agents editing this repo

You are looking at the **deployable distillation of Track 69 Strategy A**. The research, every design decision,
and the honest caveats are in `../CLAUDE.md` (⭐⭐ CURRENT STATE) and `../notes.md` (§Z–§AG). **Read the CURRENT
STATE block before changing strategy logic** — most "improvements" were already tested and killed.

## What this strategy is (one paragraph)
Follow the deep sweeps on Hyperliquid. A sweep = one aggressor **order** eating through the L2 book. Two sleeves
share one book: **BURST-ENTRY** (the core) fires when a sweep is part of a market-wide burst — the edge IS the
common factor, and solo sweeps are net-negative; **DEEP-FOLLOW** (the overlay) takes very deep single-book sweeps
but only while that book's own trailing vol is low. Enter taker, ride ~120 s, exit maker. Validated on two calm
June-2026 weeks and two disjoint 2025 cascades, with guards. It is NOT BTC beta; it is self-exciting (once ≥3
books fire, the next comes in ~7 s, ~90 % same direction).

## Architecture (data flow)
```
HLFeed.stream()  --msg-->  run.main loop
   bbo     -> MarketState.update_bbo  (THE TOUCH: bid/ask/mid/ts + top sizes + 10s vol grid)
   l2Book  -> MarketState.update_l2   (DEPTH ONLY: ladder + tick)              + Recorder.l2_snap
   trades  -> StrategyA.on_trade      (aggregate prints -> ORDER)             + Paper/LiveExecution.on_trade
                                                                              + Recorder.trade
   every loop: StrategyA.poll(now) -> [Signal(sleeve=BURST|DEEP)] -> Execution.on_signal   (open position)
               Execution.poll(now)                                             (stop / post exit / close)
               Recorder.maybe_flush()
```
- `src/strategy.py` — **the signal engine** (pure logic). `MarketState` (book, trades, trailing vol),
  `StrategyA` (order aggregation, both sleeves' reach bars, breadth, sw_span, dedup) emitting `Signal`.
  This is the heart; changes here change the strategy.
- `src/execution.py` — `PaperExecution` (simulator: entry walk, 100 bps stop, maker-improve queue-aware exit,
  shared gross cap + BURST reserve) and `LiveExecution` (real orders, gated by `live_safety`; see §Live).
- `src/feed.py` — reconnect-hardened HL WS async stream.
- `src/recorder.py` — tees trades + L2 to parquet (archive schema).
- `src/run.py` — orchestrator + CLI (`--duration`, `--test`).
- `src/config.py` — loads `config.yaml` + `seed.json`. **All tunables are in `config.yaml`.**
- `src/capital.py` — `CapitalManager`: reads wallet equity and DERIVES gross cap, per-trade size, `nmax`,
  min trade, per-order cap and the daily stop. **No dollar amount is hand-set anywhere else.** Resizes only
  while flat. Read the module docstring before changing the derivation — it encodes §AD.
- `tools/selftest.py` — offline logic check (no network). **Run it after any change to strategy/execution.**
- `OPERATIONS.md` — operator runbook: service control, reading P&L / exit quality / the shadow book, expected
  rates, and the looks-broken-but-isn't list. Point a new operator or agent there first.
- `tools/build_seed.py` — rebuilds `seed.json` from the research pool (or `--from-archive` from raw HL data).

## How the code maps to the research (so you don't re-derive)
| config / code | research | note |
|---|---|---|
| `bbo` = touch, `l2Book` = depth | §AH.2 | HL's public `l2Book` publishes on a fixed ~5.4s cycle (0.19/s for EVERY coin); `bbo` is event-driven at ~2-10/s. Reading the TOUCH off `l2Book` gave a median book age of 1,761ms and failed 75% of orders on `fresh_ms` -- and worse, reach measured against a stale touch MANUFACTURES fake deep sweeps (the artifact §Data warns about, and §W.1 measured on Binance at 97%). `MarketState._touch()` takes whichever source is NEWER so a late `l2Book` cannot re-stale a fresh touch. Sizing tolerates a stale ladder; reach does not. |
| `detection.agg_gap_ms: 100` | §AB.3 | prints -> ORDERS. Per-print scoring threw 339,774 candidates for 776 real orders on HYPE (fill fragmentation, $17 median print). The biggest fidelity bug this repo ever had. |
| `detection.reach_floor_bps: 30` | §Z.1, LIVE-ENG #1 | post-gate break-even ≈10; 30 is the deployable trigger. The floor is load-bearing; a pure percentile fails OOS. |
| `detection.reach_pctile: 99.8` + seed | §L, §AB.3 | tight books floor at 30, wide books adaptive (seed.json, now on ORDER reach). |
| `breadth.min_k: 1` | §O, §Z.9, §AE | the edge IS the common factor; solo is net-negative for BURST. Re-verified after the universe doubled: ≥1 stays optimal, monotonically. |
| `swspan` gate + rolling median | §K, LIVE-ENG #1 | causal trailing global median reproduces the in-sample gate. |
| `capital.*` -> cap/size/nmax/min-trade | §AD, §AE, §AF, §AH | sizing is DERIVED from wallet equity. Per-trade size is CAPPED at `per_trade_cap_usd` ($10k) — extra capital raises `nmax` instead, because bigger trades buy headline and no income (net bps 9.8 -> 5.2 -> 2.2). Reproduces the $500 / $10k / $100k configs exactly; the self-test pins all three. |
| `deep.reach_floor_bps: 40` | §AC.5 | lowering is uniformly worse (marginal trades net −1.2 bps AND displace BURST); 35–50 is a plateau. |
| `deep.k_vol: 2.5` | §AB.1/§AB.2 | inert under the ceiling, but kept: a RATIO bar does not switch off in a crisis the way a level bar does. |
| `deep.vol_ceiling_bps_min: 10` | §AC.4 | the load-bearing DEEP dial. Must be ABSOLUTE — a per-token percentile version fails in both regimes. Makes a market-wide breaker redundant. |
| shared cap + `execution.burst_reserve_frac` | §AC.2 | one book, shared cap, 25 % reserved for BURST — without the reserve DEEP crowds out 58 of 140 BURST triggers. |
| maker-improve exit, `hold 120 / exitwin 60` | §Z.3/Z.4/Z.6 | queue-jump barely helps, ladder HURTS (momentum ≠ reversion); one patient improve-quote is best. |
| `guards.max_feed_lag_ms: 1000` | §Z.5 | HALT new entries while the rolling MEDIAN TOUCH lag (from `bbo`) exceeds this. MEASURED baseline in ap-northeast-1 is ~285-310ms — that is HL's own publish/consensus floor, not your network (connect RTT 1.7ms), so the halt is a FAULT threshold at ~3x baseline, NOT a budget check. 400 was tried first and sits too close to baseline to survive jitter. Measured on the `bbo` touch ONLY -- NOT on trades, which HL REPLAYS on reconnect with old stamps and which gave a false 150s+ spike and an instant halt. Median, so one GC pause cannot halt the book. Open positions keep managing their exits. A negative median = local clock BEHIND the exchange (we stamp receipt earlier than HL stamps send) -- an NTP problem, not a fast feed. |
| `stop_bps: 100` + `slip_budget_bps: 30` | §R.1, §AF, §AG | the two guards that make this cascade-robust. Depth-aware sizing is the PRIMARY guard and needs no regime detection. |

## Invariants — do NOT break these
1. **Causal only.** Every threshold uses trailing or seeded data. If you add a feature it must be computable live.
2. **Fresh-book filter.** `fresh_ms` is applied at ORDER START and guards against stale books, including the
   seconds after a reconnect. Keep it.
2b. **The touch comes from `bbo`, never from `l2Book` alone.** `l2Book` is a ~5.4s periodic snapshot; using it
   as the touch silently degrades the SIGNAL (fake sweeps), not just the rate. If you add a venue or channel,
   measure the cadence of the specific field the signal reads -- `tools/feed_doctor.py` prints the
   l2Book-only counterfactual next to the live number precisely so this regression is visible.
3. **Aggregate prints into orders before any percentile.** A per-print bar is a different filter on every tape
   (§AB.3, and [[threshold-units-mis-scale]]). Normalise the event unit first.
4. **Hot path stays cheap.** Per-trade work is a dict lookup plus one comparison; percentile/median recompute is
   amortized (`reach_refresh`, `swspan.refresh`). Measured ~2–20 µs (0.006 % of budget) — do not move an O(n)
   computation into `on_trade`.
5. **Config-driven.** New knobs go in `config.yaml`, read via `cfg`. No magic numbers in code.
5b. **Money is derived, never hand-set.** Anything dollar-denominated comes from `CapitalManager`. If you
   add a sizing knob, derive it there and adopt it in `apply_capital()` on BOTH strategy and execution —
   a rail that doesn't track equity (e.g. a $10k per-order cap left on a $500 book) is the failure mode.
   Never resize while a position is open.
6. **The execution boundary.** Do NOT hard-code keys, do NOT weaken `live_safety`, do NOT flip `dry_run: false`
   on live order code you haven't verified over a full `dry_run` session against your SDK version. Default mode
   stays `paper`. **The exit is reduce-only + post-only and the stop/fallback are reduce-only market closes —
   keep them reduce-only so an exit can never open or flip a position.**
7. **Run `tools/selftest.py` after touching strategy or execution.** It catches the silent failures: an order that
   fires per-print, a solo sweep that fires BURST (edge inverted), a DEEP that ignores the vol ceiling, a reserve
   that doesn't reserve.

## Live execution — what's done vs what you must verify (§Live)
`LiveExecution` runs the **full open-position lifecycle** with real orders: **entry** (`market_open`),
**maker-improve exit** (post-only `Alo` reduce-only limit at the improve level), **taker-fallback** at the exit
window end (`market_close`), **100 bps path stop** (cancel resting exit + `market_close`), and **real fill
polling** of the resting exit (`Info.query_order_by_oid`, throttled by `live_safety.fill_poll_ms`). Realized closes
feed `daily_loss_stop`. `poll()` is a **full reimplementation** of the paper timing (not inherited) so the exact
live code path runs under `dry_run: true` too — dry_run logs every intended order and models fills off the paper
tape, so a dry session reconciles 1:1 against `PaperExecution`.

**What you MUST still verify before `dry_run: false`** (these move real money and cannot be validated here):
1. **SDK method signatures** for YOUR `hyperliquid-python-sdk` version — they drift release to release. The
   wire-touching code is isolated to the `_live_*` helpers plus the `_avg_px`/`_oid` response parsers; audit those.
   The four calls: `ex.market_open(coin,is_buy,sz,None,slippage)`,
   `ex.order(coin,is_buy,sz,px,{"limit":{"tif":"Alo"}},reduce_only=True)`, `ex.cancel(coin,oid)`,
   `ex.market_close(coin)`, and `info.query_order_by_oid(addr,oid)`.
2. **Response shapes** — `_avg_px`/`_oid`/`_live_exit_fill_frac` parse `response.data.statuses[*]` and the order
   query defensively and fall back to a reference px with a WARNING; confirm the real shapes and remove the
   guesswork. A failed fill query returns 0 (never fakes a fill) — good, but a persistently-failing query will
   ride to the taker-fallback at the window end. Watch for `[exit poll ERR]` / `[parse]` in the log.
3. **Minimum order size.** HL enforces a minimum notional per order. On a small book (`size_usd` at $500, and a
   BURST that gets only the reserve when DEEP is open) an order can land near it. Verify the floor for your
   account and raise `execution.min_trade_usd` above it rather than discovering it as a rejected order.
4. **Restart reconciliation** — `LiveExecution.reconcile_on_start()` runs once on boot (called by `run.py` before
   the feed loop). It cancels stray resting orders and **flattens** any pre-existing position on `cfg.universe`
   (the strategy horizon is ~120 s, so anything that outlived a restart is stale). It does NOT adopt a position
   back into the state machine (entry_ts/px/breadth are unrecoverable). Knobs: `live_safety.reconcile_on_start`
   and `reconcile_mode` (`flatten` | `report`). **For the very first live boot, set `reconcile_mode: report`** to
   see what it would touch before it sends anything. If the account queries fail it raises (fail-closed: don't
   trade blind to account state). Verify `info.open_orders` and `info.user_state` shapes for your SDK version.

Process: implement/verify in `LiveExecution`, run `dry_run: true`, watch a full session, reconcile against the
paper sim, then flip live yourself.

## Common edits
- **Change capital:** fund the wallet. Sizing follows within `capital.refresh_s` once the book is flat. The
  only edit needed for a big step up is raising `capital.max_equity_usd` above the new balance (the hard
  ceiling is deliberate — it exists so a deposit or a bad parse cannot silently size the book up).
- **Universe:** edit `config.yaml: universe` (source of truth is `../A_book/universe.json`) and rebuild the seed
  with `tools/build_seed.py`. More tokens is the strongest lever found — but the cascade evidence covers 27–28
  names, so new names are unvalidated in stress.
- **Trade only the US session:** `clock.restrict_utc_hours: [14,15,16,17]`.
- **Turn the overlay off:** `deep.enabled: false` (BURST alone is ~90 % of the income at a $10 k cap).
- **Test the whole path fast:** `python -m src.run --duration 90 --test` (lowered thresholds fire in ~90 s; also
  shrinks the vol window so DEEP can warm up).

## Known caveats (from the research — keep them in mind)
- 14 calm days + 2 cascades. Week 2 of the calm window had **negative** ex-tail income. Fills are
  front-of-queue-optimistic (§Z.2: realistic ≈0.55× on the exit). Signal real; net is thin and execution-gated.
- Both DEEP constants (ceiling 10, floor 40) are levels fitted on ONE 14-day calm window. Only their DIRECTION is
  cross-regime robust.
- In the one severe cascade DEEP is nearly absent (n=10) — "cascade-safe" reads as "switched off", not "proven".
- The gross cap nearly binds on a cascade day at 28 tokens ($94 k against a $100 k cap). Grow the universe and it
  binds hard, making cascade-day selection first-come-first-served.
- Reconnect leaves a ~3 s gap (could miss a burst). Consider a redundant WS connection for production.
