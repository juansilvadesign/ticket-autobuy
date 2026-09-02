# ticket-autobuy

Reserves a [BuyTicket](https://buyticketbrasil.com) listing when it drops under a price
you set, and sends you the **Pix code to pay**. It never pays — the ~30-minute hold on an
unpaid Pix is the human gate.

Sibling of [`price-watcher`](../price-watcher/), which stays stdlib-only and still never
buys. See [`CLAUDE.md`](CLAUDE.md) for why that split is load-bearing, and
[`RECON.md`](RECON.md) for what was verified against the live site.

## Status

| | |
|---|---|
| resolve a target → the exact listing URL | ✅ live-verified |
| session / login | ✅ working |
| checkout flow, all 5 screens | ✅ mapped live |
| `buy --dry-run` (drives everything, stops 1 click short) | ✅ **passes end to end** |
| headless | ✅ verified, and *faster* than headed |
| the final click + reading the Pix code | 🔲 unverified — needs one real purchase |

27 tests green.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env            # Telegram token + chat id, same bot as price-watcher
cp fields.example.json fields.json   # your details; gitignored
```

## Use

```bash
# read the market; works on a disarmed target, buys nothing
python buy.py resolve --target ../price-watcher/targets/rockinrio2026-09-04.json

# log in once, by hand, in a real browser. Tick "Lembrar de mim".
python buy.py login

# walk the checkout and dump every step's real controls. Never buys.
python buy.py map --target ../price-watcher/targets/rockinrio2026-09-04.json

# drive the WHOLE flow and stop one click short. Orders nothing.
python buy.py buy --dry-run --target ../price-watcher/targets/rockinrio2026-09-04.json

# for real: reserve + send the Pix code
python buy.py buy --target ../price-watcher/targets/rockinrio2026-09-04.json
```

## Arming a target

Add a `buy` block to any price-watcher target. price-watcher ignores the key.

```json
"buy": {
  "enabled": true,
  "max_price_brl": 220.00,
  "quantity": 1,
  "sector": ["Gramado"],
  "entry_class": null
}
```

`entry_class: null` means any class. `min_price_brl` is an optional anomaly floor and is
**off** by default — unlike price-watcher, where the floor is mandatory because a
mispriced row poisons `lowest_ever` permanently. Here a R$ 66,00 outlier is a decision to
put in front of you, not a trap: the bot reserves it, you look, and you either pay or let
the hold lapse.

## Exit codes

`0` ok / nothing matched · `1` config · `2` blind (site unreadable) · `3` you were not told
