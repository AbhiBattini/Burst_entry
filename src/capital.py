"""Capital manager — derive every money-shaped parameter from the wallet's equity.

WHY THIS EXISTS: the six sizing values used to be hand-edited together, which is exactly the kind of
multi-place edit that gets half-done. Here you set the account and the book follows it: fund the wallet with
$500, it runs a $500 book; add $9,500, it runs a $10k book at the next flat refresh.

THE DERIVATION IS NOT LINEAR, and that is the whole point (notes §AD/§AE):

    gross_cap  = equity * cap_frac                      (1.0 => at most 1x gross exposure)
    size_usd   = min(gross_cap, per_trade_cap_usd)      <- PER-TRADE SIZE IS CAPPED AT $10k
    nmax       = clip(floor(gross_cap / size_usd), 1, nmax_max)
    min_trade  = clip(gross_cap * min_trade_frac, hl_min_order_usd, 500) capped at size_usd
    daily_stop = equity * daily_loss_frac

**Per-trade size stops growing at $10k** because raising it buys headline and NO underwritable income
(§AD: net bps 9.82 -> 5.17 -> 2.16 going $10k -> $50k -> $250k; book ex-tail +$166 -> −$72 -> −$886).
Capital beyond that must be spent on MORE CONCURRENT BETS (nmax), not bigger ones. The rule reproduces all
three published configs exactly:

    $500    -> cap 500,    size 500,    nmax 1, min_trade 50    (§AH small book)
    $10,000 -> cap 10,000, size 10,000, nmax 1, min_trade 500   (§AE/§AH: MINQ 500 is the research constant)
    $100,000-> cap 100,000,size 10,000, nmax 8, min_trade 500   (§AD capacity-optimised book)

SAFETY. Auto-sizing off an API number is the kind of thing that turns a parse bug into a position, so:
  * `max_equity_usd` is a HARD ceiling applied before anything is derived — the book cannot size above it no
    matter what the exchange returns. Raise it deliberately when you actually fund more.
  * `min_equity_usd` floors it — below that the book refuses to trade rather than sending dust orders.
  * a non-finite / non-positive / absurd reading is REJECTED and the last good value is kept.
  * resizing happens ONLY WHILE FLAT, so a cap never changes underneath an open position (and equity read
    while flat has no unrealized PnL in it).
  * paper mode, and any failed query, fall back to `paper_equity_usd`.
"""
import math
import os


class CapitalManager:
    def __init__(self, cfg, log):
        c = cfg["capital"]
        self.log = log
        self.mode = c["mode"]                                   # auto | fixed
        self.fallback = float(c["paper_equity_usd"])
        self.max_eq = float(c["max_equity_usd"])
        self.min_eq = float(c["min_equity_usd"])
        self.refresh_s = float(c["refresh_s"])
        self.cap_frac = float(c["cap_frac"])
        self.per_trade_cap = float(c["per_trade_cap_usd"])
        self.min_trade_frac = float(c["min_trade_frac"])
        self.hl_min_order = float(c["hl_min_order_usd"])
        self.nmax_max = int(c["nmax_max"])
        self.daily_loss_frac = float(c["daily_loss_frac"])
        self.base_url = cfg["endpoint"]["rest"]
        self.addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
        self._info = None
        self._last_read = 0.0
        self.equity = None
        self.params = None

    # --- reading the wallet ------------------------------------------------
    def _info_client(self):
        """Read-only Info client. Needs only an ADDRESS — no private key — so paper mode can size itself to
        the real wallet without any key on the box. Returns None if unavailable (SDK missing / no address)."""
        if self._info is None:
            if not self.addr:
                return None
            try:
                from hyperliquid.info import Info
                self._info = Info(self.base_url, skip_ws=True)
            except Exception as e:
                self.log.warning(f"[capital] Info client unavailable ({type(e).__name__}: {e}) -> using fallback")
                return None
        return self._info

    def _read_equity(self):
        """Wallet account value in USD, or None if it can't be read or looks wrong."""
        if self.mode != "auto":
            return None
        info = self._info_client()
        if info is None:
            return None
        try:
            st = info.user_state(self.addr)                     # verify shape for YOUR SDK version
            v = float(st["marginSummary"]["accountValue"])
        except Exception as e:
            self.log.warning(f"[capital] equity query FAILED ({type(e).__name__}: {e}) -> keeping current size")
            return None
        if not math.isfinite(v) or v <= 0:
            self.log.error(f"[capital] REJECTED implausible equity reading {v!r} -> keeping current size")
            return None
        return v

    # --- the derivation ----------------------------------------------------
    def derive(self, equity):
        eq = min(max(equity, 0.0), self.max_eq)                 # hard ceiling FIRST
        cap = eq * self.cap_frac
        size = min(cap, self.per_trade_cap)
        nmax = 1 if size <= 0 else int(min(max(math.floor(cap / size), 1), self.nmax_max))
        min_trade = min(max(cap * self.min_trade_frac, self.hl_min_order), 500.0)
        min_trade = min(min_trade, size)                        # never demand more room than one trade needs
        return dict(equity=eq, gross_cap_usd=cap, size_usd=size, nmax=nmax,
                    min_trade_usd=min_trade, max_notional_usd=size,
                    daily_loss_stop_usd=eq * self.daily_loss_frac,
                    tradable=(eq >= self.min_eq and size >= self.hl_min_order))

    # --- lifecycle ---------------------------------------------------------
    def initial(self):
        eq = self._read_equity()
        src = "wallet"
        if eq is None:
            eq, src = self.fallback, ("fixed" if self.mode != "auto" else "FALLBACK (wallet unreadable)")
        if eq > self.max_eq:
            self.log.warning(f"[capital] equity ${eq:,.0f} exceeds max_equity_usd ${self.max_eq:,.0f} "
                             f"-> CLAMPED. Raise capital.max_equity_usd deliberately to use the rest.")
        self.equity = eq
        self.params = self.derive(eq)
        self._log_params(f"initial ({src})")
        if not self.params["tradable"]:
            self.log.error(f"[capital] equity ${eq:,.0f} below capital.min_equity_usd "
                           f"${self.min_eq:,.0f} -> NO NEW ENTRIES")
        return self.params

    def maybe_refresh(self, now_s, is_flat):
        """Re-read equity and resize — ONLY while flat, so a cap never moves under an open position.
        Returns the new params dict if anything changed, else None."""
        if self.mode != "auto" or now_s - self._last_read < self.refresh_s:
            return None
        self._last_read = now_s
        if not is_flat:
            return None
        eq = self._read_equity()
        if eq is None:
            return None
        if eq > self.max_eq:
            self.log.warning(f"[capital] equity ${eq:,.0f} exceeds max_equity_usd ${self.max_eq:,.0f} -> CLAMPED")
        new = self.derive(eq)
        old = self.params
        if all(abs(new[k] - old[k]) < 1e-6 for k in ("gross_cap_usd", "size_usd", "min_trade_usd")) \
                and new["nmax"] == old["nmax"]:
            self.equity = eq
            return None
        self.log.warning(f"[capital] EQUITY CHANGED ${old['equity']:,.2f} -> ${eq:,.2f} - RESIZING (book flat)")
        self.equity, self.params = eq, new
        self._log_params("resize")
        if not new["tradable"]:
            self.log.error(f"[capital] equity ${eq:,.0f} below capital.min_equity_usd -> NO NEW ENTRIES")
        return new

    def _log_params(self, why):
        p = self.params
        self.log.warning(
            f"[capital] {why}: equity ${p['equity']:,.2f} -> gross cap ${p['gross_cap_usd']:,.0f} | "
            f"per-trade ${p['size_usd']:,.0f} | nmax {p['nmax']} | min trade ${p['min_trade_usd']:,.0f} | "
            f"daily stop ${p['daily_loss_stop_usd']:,.0f}"
            + ("" if p["size_usd"] < self.per_trade_cap else
               f"  [per-trade capped at ${self.per_trade_cap:,.0f} (AD); extra capital goes to nmax]"))
