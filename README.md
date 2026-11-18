# btc-race-cbbtc-bot

A cbBTC 1-minute trading bot for the make.fun competition
`btc-race-24h-20261118-1444` ("Race a BTC trading bot live for 24 hours").

The bot trades Coinbase Wrapped BTC (cbBTC) against USDC with $1,000 of
paper money, one minute at a time, over the `agent-fun/bars-replay/v1`
protocol. It is long-only, unleveraged, fully deterministic, and uses
only the Python standard library plus the vendored protocol shim.

## Strategy

Two legs, both buying sharp dips and exiting on a take-profit, a
stop-loss, a trailing stop (ride leg) or a time limit:

- **Dip bounce.** After a 10-minute drop of at least 60 bps, buy with the
  whole cash balance. A thin on-chain pool overshoots on sharp drops and
  mean-reverts within minutes, so exit quickly: take profit at +90 bps
  from the fill, stop-loss at -100 bps, or a 15-bar time limit. A 30-bar
  cooldown after each exit keeps it from re-entering the same falling
  knife. No new dip entries after bar 1320.
- **Recovery ride.** Once the price has fallen at least 200 bps below its
  240-bar high, arm; buy when the last 15 minutes turn up at least
  20 bps. Ride the recovery with a take-profit at +200 bps, a stop-loss
  at -100 bps, a 100 bps trailing stop from the peak since entry, or a
  360-bar time limit. No new ride entries after bar 1080, so a ride can
  never be cut short by the window's final flatten.

Every round trip pays about 26 bps (4 bps fee + 2 bps slippage + pool
impact each way), so both legs only trade moves well above that.

## Local result (dev window, 1,440 bars)

Verified by driving `main.py` through the protocol with the kit
harness's exact fill math (next-bar open, 4 bps fee, 2 bps slippage,
constant-product impact against the bar's open depth, flatten at the
last close):

- Net PnL: **+$31.25** on $1,000 starting capital
- Trades: 8 (4 round trips)
- Max drawdown: 0.79%
- Two independent runs produce byte-identical action logs.

## Layout

- `main.py` — the bot (entrypoint, run as `python3 -P -s /bot/main.py`)
- `vendor/makefun_bars.py` — the kit's Python protocol shim, copied
  byte-for-byte from the competition kit
- `.makefun/owner.json` — the claim file naming the entrant's wallet
