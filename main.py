"""cbBTC 1-minute trading bot for make.fun btc-race-24h-20261118-1444 (v2).

Two long-only legs, both buying sharp dips and exiting on a take-profit, a
stop-loss, a trailing stop (ride leg) or a time limit. v2 changes vs v1,
each chosen from the dev window (1,440 one-minute bars) and kept only when
its neighbourhood stayed clearly positive:

* Dip take-profit 90 -> 130 bps. After a sharp 10-minute drop the bounce
  keeps running past +90: the three dev dip trades all ran further, and
  the 90 -> 130 move is worth about +$5.4 on dev with the same trades.
* Ride arm 200 -> 150 bps below the 240-bar high. The dev window's second
  recovery only dipped ~150 bps below its 4-hour high, so v1's 200 bps arm
  never saw it. A 150 bps arm catches that second ride (a small winner)
  without firing on noise: the 130-160 bps arm range gives identical dev
  trades, so 150 sits mid-plateau.
* Ride turn lookback 15 -> 20 bars. A slower, steadier 20-minute turn
  catches gradual recoveries a 15-minute window misses: the second ride
  is entered 7 bars earlier, ~200 bps cheaper. 20-22 bars is a plateau;
  18 bars is a cliff, so 20 is the safe middle of the good region.

What did NOT make it (tested, rejected): a third slow-dip leg (K=30,
drop >=80) steals the dip leg's entries at worse prices; a fast 5-minute
spike leg adds nothing (every 5-minute >=80 bps drop is already covered
by a 10-minute >=60 bps drop nearby); lower dip thresholds, shorter
cooldowns and late-window entries all lose money on dev; momentum legs
lose (forward returns after rises are negative on this window).

Costs: a full-size round trip pays about 26 bps (4 bps fee + 2 bps
slippage + pool impact each way), so both legs only trade moves well
above that. The bot is fully deterministic: integer comparisons only,
no clock, no randomness, no environment reads, no files outside the
repository. One position at a time, full cash on every buy.

Protocol: agent-fun/bars-replay/v1, spoken through the vendored shim
(vendor/makefun_bars.py). One reply per batch, at most one order.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10_000
MAX_SLIPPAGE_BPS = 300

# One table of everything the strategy can tune. Values are in basis
# points unless a name says bars.
CONFIG = {
    # Dip bounce: a sharp 10-minute drop, bought for a quick mean-reversion.
    "dip_lookback_bars": 10,
    "dip_drop_bps": 60,
    "dip_take_profit_bps": 130,
    "dip_stop_bps": 100,
    "dip_hold_bars": 15,
    "dip_cooldown_bars": 30,
    "dip_last_batch": 1320,
    # Recovery ride: a deep dip below the 4-hour high, bought once the
    # price turns up over 20 minutes, ridden with a trailing stop.
    "ride_high_bars": 240,
    "ride_arm_drop_bps": 150,
    "ride_turn_bars": 20,
    "ride_turn_bps": 20,
    "ride_take_profit_bps": 200,
    "ride_stop_bps": 100,
    "ride_trail_bps": 100,
    "ride_hold_bars": 360,
    "ride_last_batch": 1080,
}


def _down_by(close, base, bps):
    """True when close has fallen at least bps below base (integer math)."""
    return close * BPS <= base * (BPS - bps)


def _up_by(close, base, bps):
    """True when close has risen at least bps above base (integer math)."""
    return close * BPS >= base * (BPS + bps)


class Trader:
    """Holds the price history and the open trade's bookkeeping.

    State that must survive across batches:
      closes     -- every close seen, oldest first
      entry_px   -- fill price of the open trade, learned on the batch
                    after the buy lands (from the fill report, or from
                    cost over qty as a fallback)
      entry_idx  -- index into closes when the trade was decided
      peak_px    -- highest close since the trade opened (trailing stop)
      mode       -- "dip" or "ride" for the open trade
      armed      -- ride leg: a deep dip below the rolling high was seen
      last_exit  -- index into closes of the most recent exit (cooldown)
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.closes = []
        self.entry_px = None
        self.entry_idx = None
        self.peak_px = 0
        self.mode = None
        self.armed = False
        self.last_exit = -10 ** 9

    # -- entry signals -------------------------------------------------

    def _rolling_high(self, idx):
        """Highest close of the last ride_high_bars bars before idx."""
        n = self.cfg["ride_high_bars"]
        return max(self.closes[idx - n:idx])

    def _ride_signal(self, idx, close):
        """Deep dip below the rolling high, then a turn up over 20 bars.

        Arms the first time the close sits far enough below the 4-hour
        high and stays armed until the turn fires (or the trade exits).
        Returns True on the batch the turn confirms.
        """
        cfg = self.cfg
        if idx < cfg["ride_high_bars"]:
            return False
        high = self._rolling_high(idx)
        if _down_by(close, high, cfg["ride_arm_drop_bps"]):
            self.armed = True
        if not self.armed:
            return False
        turn_from = idx - cfg["ride_turn_bars"]
        if turn_from < 0:
            return False
        return _up_by(close, self.closes[turn_from], cfg["ride_turn_bps"])

    def _dip_signal(self, idx, close):
        """Sharp drop over the last dip_lookback_bars bars, after cooldown."""
        cfg = self.cfg
        if idx < cfg["dip_lookback_bars"]:
            return False
        if idx - self.last_exit < cfg["dip_cooldown_bars"]:
            return False
        base = self.closes[idx - cfg["dip_lookback_bars"]]
        return _down_by(close, base, cfg["dip_drop_bps"])

    # -- fills ----------------------------------------------------------

    def _learn_fill(self, state, batch):
        """Fill price of the buy that landed on this batch, if any.

        The batch carries every fill of the orders decided on the
        previous batch, with its exact price. Falls back to the
        position's cost over qty when no fill is reported.
        """
        for fill in batch.get("fills", []):
            if (fill.get("symbol") == SYMBOL and fill.get("side") == "buy"
                    and fill.get("status") == "filled"):
                return int(fill["price"])
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return None
        scales = state.symbols[SYMBOL]
        num = holding.cost * 10 ** (scales["qty_scale"] + scales["price_scale"])
        den = holding.qty * 10 ** state.hello["capital"]["quote_scale"]
        return num // den

    # -- orders ----------------------------------------------------------

    def _buy_all(self, state):
        return {
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": state.cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def _sell_all(self, holding):
        return {
            "side": "sell",
            "symbol": SYMBOL,
            "qty": holding.qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def _open(self, mode, idx):
        self.mode = mode
        self.entry_idx = idx
        self.entry_px = None

    def _close(self, idx):
        self.last_exit = idx
        self.entry_px = None
        self.entry_idx = None
        self.mode = None
        self.armed = False

    # -- per-batch driver ------------------------------------------------

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)
        idx = len(self.closes) - 1

        holding = state.positions.get(SYMBOL)
        if holding is not None and holding.qty > 0:
            return self._manage(state, batch, idx, close, holding)

        # Flat: no open trade to track, look for an entry. A fill
        # decided now lands at the next bar's open, so the entry price
        # is only known on the next batch; _learn_fill() picks it up.
        self.entry_px = None
        self.entry_idx = None
        self.mode = None

        cfg = self.cfg
        # Ride first: it is the bigger trade, and it must not lose the
        # bar to a dip entry on the same signal.
        if state.seq <= cfg["ride_last_batch"] and self._ride_signal(idx, close):
            self.armed = False
            self._open("ride", idx)
            return [self._buy_all(state)]
        # Then the dip bounce.
        if state.seq <= cfg["dip_last_batch"] and self._dip_signal(idx, close):
            self._open("dip", idx)
            return [self._buy_all(state)]
        return []

    def _exit_reason(self, idx, close):
        """Why the open trade should close now, or None to hold.

        Priority order (first hit wins): take-profit, stop-loss,
        trailing stop (ride leg only), time limit. All comparisons are
        integer basis-point checks against the fill price, except the
        trailing stop, which is measured from the peak since entry.
        """
        entry = self.entry_px
        cfg = self.cfg
        if self.mode == "ride":
            take_profit = cfg["ride_take_profit_bps"]
            stop = cfg["ride_stop_bps"]
            hold = cfg["ride_hold_bars"]
            trail = cfg["ride_trail_bps"]
        else:
            take_profit = cfg["dip_take_profit_bps"]
            stop = cfg["dip_stop_bps"]
            hold = cfg["dip_hold_bars"]
            trail = None
        if _up_by(close, entry, take_profit):
            return "take-profit"
        if _down_by(close, entry, stop):
            return "stop-loss"
        if trail is not None and _down_by(close, self.peak_px, trail):
            return "trailing-stop"
        if self.entry_idx is not None and idx - self.entry_idx >= hold:
            return "time-limit"
        return None

    def _manage(self, state, batch, idx, close, holding):
        # Learn the fill price on the first batch after the buy filled.
        if self.entry_px is None:
            self.entry_px = self._learn_fill(state, batch)
            self.peak_px = close
            if self.entry_px is None:
                return []
        if close > self.peak_px:
            self.peak_px = close

        if self._exit_reason(idx, close) is not None:
            self._close(idx)
            return [self._sell_all(holding)]
        return []


def on_batch(state, batch):
    return _TRADER.on_batch(state, batch)


_TRADER = Trader(CONFIG)

makefun_bars.run(on_batch)
