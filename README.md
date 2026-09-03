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


## Unattended auto-buy (`buy.py autobuy`)

Fires once per night when the market dips under that target's `buy.max_price_brl`.
Armed 2026-09-02 for all 7 Rock in Rio nights at **R$ 200,00**, one reservation per
night, **10:00–04:00 BRT**.

```cron
# ticket-autobuy — check every minute; costs ZERO extra requests to the site (it reads
# the history price-watcher just wrote) and only touches the network when it buys.
* * * * * /abs/path/ticket-autobuy/.venv/bin/python /abs/path/ticket-autobuy/buy.py autobuy >> /abs/path/ticket-autobuy/state/autobuy.log 2>&1
```

⚠️ **The venv python is load-bearing** — `autobuy` drives Playwright, which `/usr/bin/python3`
does not have. price-watcher's cron lines deliberately use the system python; this one
must not.

### What stops it running away

| guard | what it prevents |
|---|---|
| ledger in `state/autobuy.json` + the target disarms itself | a night buying twice |
| exclusive `flock` | two overlapping cron runs both seeing one dip |
| window 10:00–04:00 BRT, timezone pinned | reserving a ~10-minute hold while you sleep |
| live re-resolve before the click | buying at a price that already moved |
| `assert_poller_alive` | reading a dead price-watcher as a calm market |

### Turning it off

```bash
crontab -e            # delete the autobuy line
# or, to disarm without touching cron:
python - <<'EOF'
import json, pathlib
for p in pathlib.Path("../price-watcher/targets").glob("*.json"):
    d = json.loads(p.read_text()); d.get("buy", {})["enabled"] = False
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
EOF
```

🔴 **The session is the real limit.** The observed BuyTicket session lasted under four
hours. Until that is understood, expect to re-run `python buy.py login` roughly daily —
the runner sends a Telegram alert the first time it finds a dip it cannot buy.
