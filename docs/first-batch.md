# The first batch: twelve coins, six formats, testnet 46630

Twelve coins launched through `Launcher` on the live stack, two copies each of
six launch formats, every one graduated on a 0.0001 ETH target and traded three
times with the same size. Raw per-spec income in
`income-46630-first-batch.json`.

The point was to give the selection loop fee income to divide instead of a
number that does not exist. It produced that, and it also showed that the
number it produces is the wrong one.

## What every coin earned in ETH, identically

```
curve platform  0.000000452711 ETH
curve creator   0.000000050301 ETH
```

The same to the wei on all twelve, across every format. That is correct and it
is the problem: curve fees are a fixed 50/100 bps of the raise, every coin
raised the same 0.0001 ETH, and the founder tax -- the thing the formats vary --
is not charged on the curve at all. It is charged by the hook on trades after
graduation.

One coin shows `swaps 1` and the same ETH income as the coins with `swaps 3`,
which is the same fact from the other side.

## What the formats actually did, in the coin

The hook took its fee in the coin on every trade, so the income that separates
the formats is denominated in the coin and not in ETH:

| format | buy/sell tax | devBps | poolDevCoin |
| --- | --- | --- | --- |
| dev-heavy | 300/300 | 7000 | 1,059,973 |
| max-tax | 400/400 | 4000 | 808,562 |
| baseline | 200/300 | 4000 | 410,826 |
| tiered-dividends | 300/300 | 2500 | 378,982 |
| liquidity-heavy | 300/300 | 1000 | 151,589 |
| exit-tax-only | 0/400 | 2500 | 0 |

The ordering is exactly founder tax times dev bucket, and the two copies of
each format agree to the token, so the mechanism works and the formats are
genuinely distinguishable. `exit-tax-only` earned nothing because the trades
were all buys and it taxes only sells -- a real result about that format under
buy-only flow, not a failure.

## Why the breeder cannot use this yet

`TokenIncome.poolPlatformCoin` and `poolDevCoin` are collected and then
excluded from every total, by design: they are not ETH. So `totalEthWei` is
identical across all six formats, and a fitness function that divides it would
rank them equal and select on noise.

Valuing them in ETH is not a small fix at this size. The fee balances are
hundreds of thousands of coins against a pool holding 0.0001 ETH, so a sale
quote is slippage rather than a price, and a spot-price mark is worse. The
cheapness that made twelve coins affordable is what made their fee income
unmeasurable in ETH.

## The two numbers selection needs are not the same number

- **Ranking formats against each other** does not need ETH. Coin-denominated
  fees per unit of traded volume, on one stack, are directly comparable between
  formats and independent of pool depth. The table above is already that
  ranking.
- **The reap test** does need ETH, because it asks whether a format covered
  what a launch cost, which is a cash question (`paysItsWay`, income per copy
  against `costPerLaunchWei`). The honest ETH figure for that is what the keeper
  actually realises when it sells the fee coins -- it got 0.0000359 ETH for 25%
  of one coin's holding -- not a mark-to-market of a position too large for its
  pool.

Which of those to wire up, and whether to spend more per coin for pools deep
enough that a quote means something, is a money decision and is not settled
here.
