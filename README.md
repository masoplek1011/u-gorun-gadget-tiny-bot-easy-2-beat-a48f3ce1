[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![Rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/masoplek1011/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![Reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/masoplek1011/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

# Tiny Bot — Trend-Following SOL Paper Trader

A tiny paper-trading bot for make.fun's ["Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana"](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1) bounty. It trades hourly Solana bars (FARTCOIN, JITOSOL, POPCAT, RAY, SOL, WETH, WIF) against the `agent-fun/bars-replay/v1` harness, long only, and sits in USDC when there is no trend to ride.

## Strategy

Per symbol, the bot runs an EMA(8) / EMA(21) crossover on hourly closes:

- **Enter long** when the fast EMA crosses above the slow EMA, but only while the slow EMA itself is rising (above its value 6 bars ago) — this filters out sideways chop where crossovers whipsaw.
- **Exit** the whole position when the fast EMA crosses back below the slow EMA, when the position is worth 8% under its cost (stop-loss), or 40% over its cost (take-profit).
- **Equity brake:** if equity falls 8% below its running peak, the bot flattens every position at once and resets the peak, so a sharp regime break cannot bleed the account before the per-symbol exits fire.
- **Sizing:** equity split evenly across the 7 symbols, capped at the harness 25% max weight per symbol; sells are sent before buys in each reply.

The bot is deliberately simple and robust: few round parameters, no per-symbol tuning, no fitting to any particular window — it is meant to survive the unseen final window, not to maximise the dev score.

## Run

```sh
python3 main.py
```

The bot speaks the `agent-fun/bars-replay/v1` protocol over stdin/stdout (NDJSON): it reads `hello`, `batch`, and `done` messages and replies with exactly one `{"type": "reply", "seq": N, "orders": [...]}` per batch. It is stdlib-only, writes no files, reads no clock or environment, and uses no randomness, so two runs on the same data produce identical action logs.
