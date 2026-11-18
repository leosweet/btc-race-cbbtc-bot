"""cbBTC 1-minute trading bot for make.fun btc-race-24h-20261118-1444.

Strategy: two long-only legs, both buying sharp dips and exiting on a
take-profit, a stop-loss, a trailing stop (ride leg) or a time limit.

Leg 1 -- dip bounce: after a 10-minute drop of at least 60 bps, buy the
whole cash balance. A thin on-chain pool overshoots on sharp drops and
mean-reverts within minutes, so exit quickly: take profit at +90 bps from
the fill, stop-loss at -100 bps, or a 15-bar time limit. A 30-bar cooldown
after each exit keeps it from re-entering the same falling knife.

Leg 2 -- recovery ride: once the price has fallen at least 200 bps below
its 240-bar high, arm; buy when the last 15 minutes turn up at least
20 bps. Ride the recovery with a take-profit at +200 bps, a stop-loss at
-100 bps, a 100 bps trailing stop from the peak since entry, or a 360-bar
time limit. Entries stop after bar 1080 so a ride can never be cut short
by the window's final flatten; dip entries stop after bar 1320.

Costs: every round trip pays about 26 bps (4 bps fee + 2 bps slippage +
pool impact each way), so both legs only trade moves well above that.
The bot is fully deterministic: integer comparisons only, no clock, no
randomness, no environment reads, no files outside the repository.

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

# Leg 1 -- dip bounce.
DIP_LOOKBACK = 10       # bars over which the drop is measured
DIP_DROP_BPS = 60       # buy when the close fell this far over DIP_LOOKBACK bars
DIP_TP_BPS = 90         # take profit, from the fill price
DIP_SL_BPS = 100        # stop loss, from the fill price
DIP_HOLD_BARS = 15      # time-limit exit
DIP_COOLDOWN_BARS = 30  # bars to wait after an exit before re-entering
DIP_LAST_BAR = 1320     # no new dip entries after this batch

# Leg 2 -- recovery ride.
RIDE_HIGH_BARS = 240    # rolling window for the reference high
RIDE_DIP_BPS = 200      # arm once the close is this far below the rolling high
RIDE_TURN_BARS = 15     # bars over which the turn-up is measured
RIDE_TURN_BPS = 20      # buy once the close rose this far over RIDE_TURN_BARS
RIDE_TP_BPS = 200       # take profit, from the fill price
RIDE_SL_BPS = 100       # stop loss, from the fill price
RIDE_TRAIL_BPS = 100    # trailing stop, from the peak since entry
RIDE_HOLD_BARS = 360    # time-limit exit
RIDE_LAST_BAR = 1080    # no new ride entries after this batch


class Bot:
    """Keeps the price history and the open trade's bookkeeping."""

    def __init__(self):
        self.closes = []        # closes seen so far, oldest first
        self.entry_px = None    # fill price of the open trade (price units)
        self.entry_bar = None   # index into closes when the trade opened
        self.peak_px = 0        # highest close since the trade opened
        self.mode = None        # "dip" or "ride" for the open trade
        self.armed = False      # ride leg: a deep dip has been seen
        self.last_exit = -10 ** 9  # index into closes of the last exit

    def _fill_price(self, state, batch):
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
        in_position = holding is not None and holding.qty > 0

        if in_position:
            return self._manage(state, batch, idx, close, holding)

        # Flat: look for an entry. A fill decided now lands at the next
        # bar's open, so the entry price is only known on the next batch;
        # _fill_price() picks it up then.
        self.entry_px = None
        self.entry_bar = None
        self.mode = None

        # Leg 2 -- recovery ride (checked first: it is the bigger trade).
        if idx >= RIDE_HIGH_BARS and state.seq <= RIDE_LAST_BAR:
            high = max(self.closes[idx - RIDE_HIGH_BARS:idx])
            if close * BPS <= high * (BPS - RIDE_DIP_BPS):
                self.armed = True
            if self.armed:
                base = self.closes[idx - RIDE_TURN_BARS]
                if close * BPS >= base * (BPS + RIDE_TURN_BPS):
                    self.armed = False
                    self.mode = "ride"
                    self.entry_bar = idx
                    return [self._buy(state)]
        # Leg 1 -- dip bounce.
        if idx >= DIP_LOOKBACK and state.seq <= DIP_LAST_BAR:
            if idx - self.last_exit >= DIP_COOLDOWN_BARS:
                base = self.closes[idx - DIP_LOOKBACK]
                if close * BPS <= base * (BPS - DIP_DROP_BPS):
                    self.mode = "dip"
                    self.entry_bar = idx
                    return [self._buy(state)]
        return []

    def _manage(self, state, batch, idx, close, holding):
        # Learn the fill price on the first batch after the buy filled.
        if self.entry_px is None:
            self.entry_px = self._fill_price(state, batch)
            self.peak_px = close
            if self.entry_px is None:
                return []
        if close > self.peak_px:
            self.peak_px = close
        entry = self.entry_px

        if self.mode == "ride":
            tp_bps = RIDE_TP_BPS
            sl_bps = RIDE_SL_BPS
            hold_bars = RIDE_HOLD_BARS
        else:
            tp_bps = DIP_TP_BPS
            sl_bps = DIP_SL_BPS
            hold_bars = DIP_HOLD_BARS

        exit_now = False
        if close * BPS >= entry * (BPS + tp_bps):
            exit_now = True
        elif close * BPS <= entry * (BPS - sl_bps):
            exit_now = True
        elif self.mode == "ride" and close * BPS <= self.peak_px * (BPS - RIDE_TRAIL_BPS):
            exit_now = True
        elif self.entry_bar is not None and idx - self.entry_bar >= hold_bars:
            exit_now = True

        if exit_now:
            self.last_exit = idx
            self.entry_px = None
            self.entry_bar = None
            self.mode = None
            self.armed = False
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": holding.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []

    def _buy(self, state):
        return {
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": state.cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


_BOT = Bot()

makefun_bars.run(on_batch)
