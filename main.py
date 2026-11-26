"""Trend-following long/cash bot for the make.fun bars-replay/v1 harness.

Strategy (per symbol, hourly bars, long only, cash otherwise):
  * EMA(8) / EMA(21) crossover on closes. Enter long when the fast EMA
    sits above the slow EMA while the slow EMA itself is rising (its
    value now is above its value 6 bars ago) -- a level entry, not just
    the cross bar, so a trend that started during warm-up or while the
    filter was off is still caught, and sideways chop (flat slow EMA)
    keeps the bot in cash. After an exit, a symbol rests 6 bars before
    it may re-enter, so a stop-loss exit is not bought straight back on
    the next bar.
  * Exit the whole position when the fast EMA crosses back below the slow
    EMA, or when the position is worth 8% under its cost (stop-loss), or
    40% over its cost (take-profit).
  * Equity brake: if equity falls 8% below its running peak, flatten every
    position at once and reset the peak, so a sharp regime break cannot
    bleed the account before the per-symbol exits fire.
  * Sizing: equity split evenly over the 7 symbols, capped at the harness
    25% max weight per symbol; sells are sent before buys in each reply.

Protocol: a minimal hand-written agent-fun/bars-replay/v1 shim -- read
hello / batch / done as NDJSON on stdin, reply exactly one JSON line per
batch on stdout. Orders decided on bar k fill at bar k+1's open. No file
writes, no clock, no environment reads, no randomness: fully
deterministic, so two runs produce byte-identical action logs.
"""

import json
import sys

FAST_SPAN = 8
SLOW_SPAN = 21
SLOPE_BARS = 6          # slow EMA must be above its value this many bars ago
COOLDOWN_BARS = 6       # bars a symbol rests after an exit before re-entry
STOP_NUM = 92           # exit when position value <= 92% of cost
STOP_DEN = 100
TAKE_NUM = 140          # exit when position value >= 140% of cost
TAKE_DEN = 100
BRAKE_DD_BPS = 800      # flatten all when equity is 8% below its peak
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on
BPS = 10000


class Ema:
    """One EMA: seeded as the mean of its first span of closes."""

    def __init__(self, span):
        self.span = span
        self.alpha = 2.0 / (span + 1)
        self.warm = []
        self.value = None

    def step(self, close):
        if self.value is not None:
            self.value += self.alpha * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span


class Series:
    """Per-symbol indicator state."""

    def __init__(self):
        self.fast = Ema(FAST_SPAN)
        self.slow = Ema(SLOW_SPAN)
        self.slow_hist = []      # past slow EMA values, for the rising check
        self.gap = None          # fast - slow after the previous bar
        self.now = None          # fast - slow after this bar
        self.down = False        # sell signal held while fast stays under slow

    def push(self, close):
        self.fast.step(close)
        self.slow.step(close)
        self.gap = self.now
        if self.fast.value is not None and self.slow.value is not None:
            self.now = self.fast.value - self.slow.value
            self.slow_hist.append(self.slow.value)
        else:
            self.now = None
        if self.now is None or self.now >= 0:
            self.down = False

    def rising(self):
        """True when the slow EMA is above its value SLOPE_BARS bars ago."""
        if len(self.slow_hist) <= SLOPE_BARS:
            return False
        return self.slow_hist[-1] > self.slow_hist[-1 - SLOPE_BARS]


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.symbols = {s["symbol"]: s for s in hello["symbols"]}
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.quote_scale = hello["capital"]["quote_scale"]
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.positions = {}      # symbol -> (qty, cost) as ints
        self.series = {}
        self.peak_equity = self.cash
        self.cooldown = {}   # symbol -> first seq it may re-enter on
        self.seq = -1
        self.next_id = 1

    def price_scale(self, symbol):
        return self.symbols[symbol]["price_scale"]

    def qty_scale(self, symbol):
        return self.symbols[symbol]["qty_scale"]

    def value(self, symbol, qty, price):
        """Quote units qty is worth at price (both raw ints)."""
        s = self.symbols[symbol]
        return qty * price * 10 ** self.quote_scale // 10 ** (s["qty_scale"] + s["price_scale"])

    def client_id(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid

    def apply_account(self, account):
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {
            p["symbol"]: (int(p["qty"]), int(p["cost_quote_units"]))
            for p in account["positions"]
        }
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

    def on_batch(self, batch):
        self.seq = batch["seq"]
        self.apply_account(batch["account"])
        fresh = set()
        closes = {}
        for bar in batch["bars"]:
            symbol = bar["symbol"]
            fresh.add(symbol)
            closes[symbol] = int(bar["c"])
            s = self.series.get(symbol)
            if s is None:
                s = Series()
                self.series[symbol] = s
            # A zero-volume bar signals nothing (same guard as the house
            # EMA baseline): update nothing for it.
            if bar.get("v") == "0":
                continue
            s.push(float(bar["c"]) / (10 ** self.price_scale(symbol)))
            if s.gap is not None and s.now is not None:
                if s.now < 0 and s.gap >= 0:
                    s.down = True
        if self.seq < self.warmup_bars:
            return []

        orders = []
        max_orders = self.caps["max_orders_per_bar"]
        # Equity brake: flatten everything, reset the peak, trade no more
        # on this bar.
        brake = self.equity * BPS < self.peak_equity * (BPS - BRAKE_DD_BPS)
        if brake:
            for symbol in self.hello["symbols"]:
                sym = symbol["symbol"]
                if sym in self.positions and len(orders) < max_orders:
                    orders.append(self._sell(sym))
                    self.cooldown[sym] = self.seq + COOLDOWN_BARS
            self.peak_equity = self.equity
            return orders

        sells = []
        entries = []
        for symbol in self.hello["symbols"]:
            sym = symbol["symbol"]
            if sym not in fresh:
                continue
            held = self.positions.get(sym)
            s = self.series[sym]
            if held is not None:
                qty, cost = held
                val = self.value(sym, qty, closes[sym])
                if (s.down
                        or val * STOP_DEN <= cost * STOP_NUM
                        or val * TAKE_DEN >= cost * TAKE_NUM):
                    sells.append(self._sell(sym))
                    self.cooldown[sym] = self.seq + COOLDOWN_BARS
            elif (s.now is not None and s.now > 0 and s.rising()
                    and self.seq >= self.cooldown.get(sym, -1)):
                entries.append(sym)

        orders.extend(sells[:max_orders])
        slots = min(len(self.hello["symbols"]), self.caps["max_open_positions"])
        stake = self.equity // slots
        by_weight = self.caps["max_weight_bps"] * self.equity // BPS
        room = slots - len(self.positions)
        cash = self.cash
        min_notional = int(self.fees["min_notional_quote_units"])
        for sym in entries:
            if room <= 0 or len(orders) >= max_orders:
                break
            size = stake if stake < by_weight else by_weight
            if cash < size:
                size = cash
            if size <= 0 or size < min_notional:
                break
            orders.append({
                "side": "buy",
                "client_id": self.client_id(),
                "symbol": sym,
                "notional_quote_units": str(size),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            cash -= size
            room -= 1
        return orders

    def _sell(self, symbol):
        qty = self.positions[symbol][0]
        return {
            "side": "sell",
            "client_id": self.client_id(),
            "symbol": symbol,
            "qty": str(qty),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            orders = bot.on_batch(msg)
            out.write(json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
