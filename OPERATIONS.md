# OPERATIONS — running and reading the paper book

Operator/agent runbook for the deployed box. **What the strategy IS** lives in `README.md`; **why every
parameter has its value** lives in `AGENTS.md` and `../notes.md`. This file is only: how to run it, how to
read it, and what "normal" looks like.

Everything below assumes the service install (`bash setup.sh --service`) on the AWS Tokyo box, repo at
`~/Burst_entry`, running `mode: paper`.

---

## 0. Context a fresh operator needs

| | |
|---|---|
| Where | AWS `ap-northeast-1` (Tokyo) — same region as HL's validators. Region is the only latency lever that matters. |
| Mode | `paper` — simulates on the live feed, **sends no orders, needs no key** |
| Universe | 31 HL perps (`config.yaml: universe`) |
| Sizing | **Derived from wallet equity** by `src/capital.py`. Never hand-edit dollar amounts. |
| Service | `strat-a` (systemd, `Restart=always`, survives reboot) |

---

## 0b. Bringing up a FRESH box

**Instance:** AWS **`ap-northeast-1` (Tokyo)** — this is the only latency choice that matters, HL's validators
are there. Ubuntu Server 24.04 LTS **(Arm)** for `c7g.medium`, or x86 for a `t3.*`. 50 GiB gp3. Security group
SSH from **My IP** only. Any AZ (research puts validators in az1/2/4; cross-AZ is ~0.3–2 ms against a ~200 ms
consensus floor, so it does not matter).

```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip git
```

Clock sync — the feed-lag guard depends on it (`System clock synchronized: yes`):

```bash
timedatectl
```

```bash
git clone https://github.com/AbhiBattini/Burst_entry.git && cd Burst_entry
```

```bash
bash setup.sh
```

```bash
.venv/bin/python tools/selftest.py
```

**Verify the feed before trusting any run** — this is the check that would have caught the 5.4 s stale-touch
bug. Want `fresh%` ~99 %, book age median ~0 ms, `bbo/s` ~2–10, `l2/s` ~0.2:

```bash
.venv/bin/python tools/feed_doctor.py 120
```

Confirm the network placement took (~1–15 ms connect; 100 ms+ means the wrong region):

```bash
curl -o /dev/null -s -w "connect %{time_connect}s  total %{time_total}s
" https://api.hyperliquid.xyz/info
```

Then install the service and watch it:

```bash
bash setup.sh --service
```

```bash
journalctl -u strat-a -f
```

The `START` line should read `mode=paper`, `universe=31`, `seed=31tok`, and the equity/sizing you expect.

---

## 1. Service control

```bash
systemctl is-active strat-a
```

```bash
sudo systemctl start strat-a
sudo systemctl stop strat-a
sudo systemctl restart strat-a
```

**Halt new entries without killing the process** (open positions still manage their own exits — this is the
safe "stop trading now" button, and the one to use in live mode):

```bash
touch ~/Burst_entry/KILL      # remove the file to resume
```

---

## 2. Reading the logs

Follow live (**Ctrl-C only stops the tailing, not the service**):

```bash
journalctl -u strat-a -f
```

Scope to the CURRENT run only — old runs cannot pollute the counts:

```bash
journalctl -u strat-a --since "$(systemctl show -p ActiveEnterTimestamp --value strat-a)"
```

Confirm the config that actually loaded (mode, universe, equity, sizing, DEEP thresholds):

```bash
journalctl -u strat-a | grep -m1 START
```

Warnings and errors only:

```bash
journalctl -u strat-a -p warning --since "6 hours ago"
```

### Log line vocabulary

| Line | Meaning |
|---|---|
| `[capital]` | equity read and the sizing derived from it; also logs any resize |
| `[feed]` | rolling median TOUCH lag (from `bbo`), once a minute; also names any book with a stale touch |
| `SIGNAL` | a gated signal fired (BURST or DEEP) |
| `[OPEN ]` | position opened (paper) |
| `[CLOSE]` | position closed — carries `maker`/`taker`/`stop` and net bps |
| `[SKIP ]` | signal not taken. THREE distinct reasons, and the line says which: **no room** (capital — the only one that feeds the shadow book), **drift** (the move already happened), **ladder stale** (depth book too old to size against) |
| `[SHADOW]` | a skipped signal's counterfactual result (**not traded**) |
| `flush` | recorder wrote parquet; carries cumulative traded and shadow P&L |

---

## 3. Results

Fastest — the recorder logs a running total every 10 minutes:

```bash
journalctl -u strat-a | grep flush | tail -1
```

Full breakdown (safe before any trade has closed):

```bash
cd ~/Burst_entry && .venv/bin/python -c "
import pandas as pd, os
for f,lbl in [('paper_trades.parquet','TRADED'),('shadow_trades.parquet','SHADOW')]:
    if os.path.exists(f):
        d=pd.read_parquet(f)
        print(f'{lbl:7} n={len(d):4d}  \${d.usd.sum():+8.2f}  {d.net_bps.mean():+6.2f} bps  win {100*(d.net_bps>0).mean():3.0f}%')
    else: print(f'{lbl:7} (none yet)')
"
```

### Exit quality — the most important number here

```bash
cd ~/Burst_entry && .venv/bin/python -c "
import pandas as pd; d=pd.read_parquet('paper_trades.parquet')
print(d.groupby('kind').agg(n=('usd','size'), usd=('usd','sum'), bps=('net_bps','mean')))
print('maker share', round(100*(d.kind=='maker').mean(),1), '%')
"
```

Or straight from the log:

```bash
journalctl -u strat-a | grep "\[CLOSE" | grep -oE "maker|taker|stop" | sort | uniq -c
```

**Why it matters:** the backtest's exit fills are *front-of-queue optimistic*. §Z.2 measured realistic
queue-aware fills at ≈0.55× that, which is why the honest $/day estimate is roughly half the headline. The
live maker share is what replaces that assumed 0.55× with a measured one. **This is the single number the
paper run exists to produce.**

### Entry quality — drift vs slippage (the front-running measure)

```bash
cd ~/Burst_entry && .venv/bin/python -c "
import pandas as pd; d=pd.read_parquet('paper_trades.parquet')
print(d[['drift_bps','slip_bps','net_bps']].describe().loc[['count','mean','50%','75%','max']].round(2))
print()
print('drift > 20bps:', round(100*(d.drift_bps>20).mean(),1), '% of entries')
print('net_bps when drift<=10 :', round(d.loc[d.drift_bps<=10,'net_bps'].mean(),2))
print('net_bps when drift> 10 :', round(d.loc[d.drift_bps> 10,'net_bps'].mean(),2))
"
```

Every entry records the implementation shortfall, split into its two causes:

- **`drift_bps`** — how far the mid moved OUR WAY between the trigger and our fill. Positive = we are buying
  after the move. This is the cost of latency **and of anyone acting on the same public sweep faster than
  us**. It is the thing that cannot be measured before trading.
- **`slip_bps`** — our own book-walk from the touch, i.e. the cost of size. Already researched (§J found
  entry slippage ~= the whole gross edge at $10k on an 8-name basket).

**How to act on it.** If `net_bps` is materially worse in the high-drift bucket, you are being beaten to the
move and `execution.max_entry_drift_bps` should come down toward the point where the buckets diverge. If the
buckets look the same, drift is not costing you and the guard should stay loose — **tightening it deviates
from the researched spec**, which prices entry at te+0.4s and therefore already contains normal drift.
Collect a few hundred entries before touching it.

### Per-sleeve split

```bash
cd ~/Burst_entry && .venv/bin/python -c "
import pandas as pd; d=pd.read_parquet('paper_trades.parquet')
print(d.groupby('sleeve').agg(n=('usd','size'), usd=('usd','sum'), bps=('net_bps','mean')))
"
```

BURST should carry the book; DEEP is a small overlay.

### Shadow book — the opportunity cost of the current book size

Signals skipped **for lack of capital** are run through the identical lifecycle at the full intended size,
committing no capital. Compare `SHADOW` to `TRADED` above:

- shadow P&L ≈ 0 or negative → capital is **not** the constraint; adding funds buys little
- shadow P&L consistently positive → direct evidence for sizing up

Shadows never touch traded P&L, and are **hard-off in live mode**.

### Activity counts

```bash
journalctl -u strat-a --since "1 day ago" | grep -oE "\[feed\]|SIGNAL|\[OPEN|\[CLOSE|\[SKIP|\[SHADOW" | sort | uniq -c
```

`[feed]` prints once a minute, so its count doubles as a runtime check (1440 ≈ a full day with no gaps).

---

## 4. What "normal" looks like

Compare against these before concluding anything is wrong.

| Metric | Expected | Source |
|---|---|---|
| Signals | ~1.1 / hour (~27/day) | backtest, 31 tokens |
| Trades taken | ~1.0 / hour (~24/day) | 341 trades / 14 days |
| — BURST | ~0.9 / hour | 304 / 14 days |
| — DEEP | **~1 per 9 hours** | 37 / 14 days |
| Maker exit share | ~62% (backtest assumption) | §AD pool |
| Feed lag median | **~285–310 ms** | measured in-region 2026-09-02 |
| bbo updates | ~9–10 /s /coin (~78 ms gap) | measured 2026-09-03 |
| l2Book updates | ~0.2 /s /coin (~5.4 s gap) — depth only | measured 2026-09-03 |
| Orders passing `fresh_ms` | **~99%** | with bbo as the touch |
| Reconnects | rare; each leaves a ~3 s gap | — |
| Recorder disk | **~50 MB/day** (measure it) | l2Book is only ~0.2/s, so ~10x less than the 400 MB/day first assumed at 2/s |

Rate is **not** uniform — §Z.8 found the edge concentrates 14–17 UTC, so expect clustering. Judge over a
full day or more; hour-to-hour counts are Poisson noise at n≈1.

### Looks broken, isn't

- **Long stretches with no DEEP trades** — expected, it fires ~once per 9 hours.
- **Frequent `[SKIP ] no room`** — expected at small equity; the book funds one position at a time.
- **Nothing at all for the first ~15 min after a restart** — the rolling p99.8 re-warms from `seed.json` and
  DEEP's vol window needs ~15 min of book before that sleeve can fire.
- **Negative `[feed]` median lag** — the local clock is AHEAD of the exchange, i.e. an NTP problem, not a
  fast feed. Fix with `sudo timedatectl set-ntp true`. The lag guard is unreliable until you do.
- **~300 ms feed lag** — that is HL's own publish/consensus floor (HyperBFT ~200 ms), not your network.
  Verify with the RTT check below; ~1–15 ms connect means the network is fine.
- **`STALE touch` naming a book or two in the `[feed]` line** — normal in small numbers. A book only needs to
  go a couple of seconds without a quote change to appear. It matters when the SAME book is named
  persistently: that book is contributing no candidates and no breadth.
- **`TON` missing entirely (2026-09-03)** — HL accepts the `bbo` subscription for TON and never publishes on
  it, and its `l2Book` is intermittent too; observed windows with 30/31 books held. This is a VENUE gap, not
  a subscription problem (all 93 subscribe-acks return, and there is no throttling — see below). It degrades
  SAFELY: with no fresh touch, TON's orders fail `fresh_ms`, so it yields no candidates and no breadth, and
  `on_signal` returns early when there is no book at all. Left in the universe because `A_book/universe.json`
  is the research source of truth; remove it only deliberately.

### Is HL throttling us?

```bash
cd ~/Burst_entry && .venv/bin/python tools/throttle_check.py 90
```

Measures per-coin `bbo` rate on `bbo` alone vs the full 3-channel subscription (93 subs at 31 coins). If the
full arm is materially slower, the touch stream is being degraded and channels should be split across
sockets. **Measured 2026-09-03: 4.48/s alone vs 4.76/s full = 106% — no throttling, the full subscription is
free.**

---

## 4b. Feed doctor — run this FIRST when the trade rate looks wrong

```bash
cd ~/Burst_entry && .venv/bin/python tools/feed_doctor.py 120
```

Reports book coverage, the trade-vs-book staleness distribution, and per-coin cadence. Read it as:

- **low `fresh%` with healthy `bbo/s`** -> the FILTER is the constraint, not the market
- **low `bbo/s`, or a coin missing entirely** -> the FEED is the constraint (a missing book contributes
  nothing to breadth, and is invisible in the strategy log: it just looks like a quiet market)

**Expected (measured 2026-09-03):** `bbo/s` ~9-10 per coin, `l2/s` ~0.2, fresh% ~99%+, book age median ~0 ms.

**If fresh% is ~25% and book age median is ~1,700 ms, the touch is coming from `l2Book` instead of `bbo`** —
that was the pre-2026-09-03 bug. HL's public `l2Book` arrives only every ~5.4 s; `bbo` arrives every ~78 ms.

## 4c. Latency — what is worth buying, and what is not

**Do not buy a faster feed for this strategy.** Entry is pinned to `trigger + swspan.cluster_s` (0.4 s), not
to when you learn about the trigger, so reducing feed lag does **not** move your entry time.

Where the time actually goes:

```
trigger  -> we learn      ~300 ms   HL's publish floor, not your network
         -> we WAIT       to trigger+400 ms   (deliberate: the sw_span cluster gate)
         -> send            ~2 ms   in-region
         -> consensus     ~200 ms   fixed, HyperBFT
   fill lands around trigger + 600-700 ms
```

At ~300 ms lag you already hold ~100 ms of slack against the 400 ms deadline. Paid options exist (dedicated
HL nodes benchmark ~51 ms / ~24 % faster; vendors sell Tokyo-peered API access and gRPC market data) but that
51 ms buys **margin** — jitter robustness and headroom under `guards.max_feed_lag_ms` — not edge. Treat a
premium feed as an availability/SLA purchase, not a performance one.

The one thing that would change this: if the drift buckets in §3 show a systematic cost. Even then the lever
is the **cluster window** (a research question — it is what makes the gate work) rather than infrastructure.

## 5. Health checks

Network round trip to HL (should be ~1–15 ms from Tokyo; 100 ms+ means the box is in the wrong region):

```bash
curl -o /dev/null -s -w "connect %{time_connect}s  total %{time_total}s\n" https://api.hyperliquid.xyz/info
```

Clock sync — the feed-lag guard depends on it (`System clock synchronized: yes`):

```bash
timedatectl
```

Disk — **a full disk crashes the strategy**. Original estimate was ~400 MB/day assuming 2 L2 snaps/s/coin; the
public l2Book turned out to be ~0.2/s, so expect roughly **~50 MB/day**. Measure rather than trust either number:

```bash
df -h / && du -sh ~/Burst_entry/data
```

If it's filling up, either prune weekly or turn the recorder off in `config.yaml`:

```bash
crontab -e    # add: 0 3 * * * find /home/ubuntu/Burst_entry/data -name '*.parquet' -mtime +7 -delete
```

Memory (2 GiB box; lower `recorder.flush_s` from 600 to 300 if it's tight):

```bash
free -h
```

---

## 6. Updating the code

```bash
cd ~/Burst_entry && git pull && sudo systemctl restart strat-a
```

**Always run the offline self-test after pulling** — it catches the silent failures (a detector that fires
per-print, a solo sweep firing BURST with the edge inverted, a DEEP ignoring its vol ceiling, a reserve that
doesn't reserve, a shadow leaking into traded P&L):

```bash
cd ~/Burst_entry && .venv/bin/python tools/selftest.py
```

Exit code 0 = all pass. **Do not run a book that fails this.**

---

## 7. Clean restart (wipe run data)

```bash
sudo systemctl stop strat-a
```

```bash
cd ~/Burst_entry && rm -f paper_trades.parquet shadow_trades.parquet stratA.log && rm -rf data/*
```

```bash
sudo systemctl start strat-a
```

To keep a run's results first:

```bash
cd ~/Burst_entry && mkdir -p runs && cp paper_trades.parquet shadow_trades.parquet runs/$(date +%Y%m%d_%H%M)_
```

---

## 8. Changing capital

**You do not edit dollar amounts.** `capital.mode: auto` reads wallet equity and derives gross cap,
per-trade size, `nmax`, min trade, per-order cap and the daily stop. Fund the wallet and it follows within
`capital.refresh_s`, applied only while flat.

The one edit a big step-up needs is raising the ceiling above the new balance:

```bash
grep -n "max_equity_usd" ~/Burst_entry/config.yaml
```

That ceiling is deliberate — it exists so a stray deposit or a bad parse cannot silently size the book up.

For paper without a wallet, `capital.paper_equity_usd` is the fallback. Verify what any equity would derive:

```bash
cd ~/Burst_entry && .venv/bin/python -c "
import sys,logging,yaml; sys.path.insert(0,'.')
from src.capital import CapitalManager
log=logging.getLogger('t'); log.addHandler(logging.NullHandler())
cfg=yaml.safe_load(open('config.yaml')); cfg['_root']='.'
for eq in (1500, 10000, 100000):
    p=CapitalManager(cfg,log).derive(eq)
    print(eq, {k:p[k] for k in ['gross_cap_usd','size_usd','nmax','min_trade_usd','daily_loss_stop_usd']})
"
```

---

## 9. Before going live — do not skip

Read `AGENTS.md` §Live in full. In order:

1. Paper for days; reconcile rate and maker share against §4.
2. `./setup.sh --live` (installs the HL SDK).
3. `.env`: `HL_ACCOUNT_ADDRESS` = main account, `HL_PRIVATE_KEY` = **agent/API wallet** key (it can trade
   but **cannot withdraw**). Never put a main MetaMask key on the box.
4. `mode: live` with **`live_safety.dry_run: true`** — logs every order it would send without sending.
   Watch a full session and reconcile against paper.
5. First live boot: `live_safety.reconcile_mode: report` so startup reconciliation shows what it *would*
   touch before it sends anything.
6. Verify the SDK method signatures for your installed version — they drift between releases.
7. Only then `dry_run: false`.

**Standing caveats.** Evidence is 14 calm days + 2 cascade windows. Maker-exit fills are
front-of-queue-optimistic. Track 69 has deliberately **not** been promoted to the desk's deployed-book
table. Do not tune thresholds on a day of live data — every value in `config.yaml` traces to a research
section, and most "obvious improvements" were already tested and killed (see `AGENTS.md`).
