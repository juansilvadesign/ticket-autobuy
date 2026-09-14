# ticket-autobuy

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"  srcset="assets/showcase/price-watcher-ticket-autobuy-showcase-dark.webp">
    <source media="(prefers-color-scheme: light)" srcset="assets/showcase/price-watcher-ticket-autobuy-showcase-light.webp">
    <img src="assets/showcase/price-watcher-ticket-autobuy-showcase-light.webp"
         alt="price-watcher and ticket-autobuy: the shared mark resolves into a browser tab, a listing page showing one price opens a payload drawer holding nineteen lots, watch.py streams its polls beside the target file's buy block, a Telegram alert lands stamped ALWAYS PRECISE, two cards face each other across the one config key that crosses between them, a checkout stepper walks to a reserved order left for you to approve, and the counts settle at 424 tests, 7 nights and 1 real order"
         width="100%">
  </picture>
</p>

*One reel, two repos.* [`price-watcher`](../price-watcher/) watches and **cannot buy**; `ticket-autobuy`
holds the browser and reserves. The only thing that crosses the line between them is a `buy`
block in a target file that the watcher parses and ignores. Source composition:
[`juansilva.design/motion/price-watcher-ticket-autobuy-showcase`](https://github.com/juansilvadesign/juansilva.design).

Reserves a [BuyTicket](https://buyticketbrasil.com) listing when it drops under a price
you set, and sends you the **Pix code to pay**. It never pays — the **~10-minute** hold on
an unpaid Pix is the human gate. (Measured on order #7707X57Q: the checkout says *"Você
tem 10 minutos para completar sua compra"*. Earlier drafts of this file said 30, which is
not a rounding error — it is three times the real margin for a human who has to be handed
a code and pay it.)

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
| the final click + reading the Pix code | ✅ **proven end to end** 2026-09-11 (order `6731D321`, Pix in 5.49s) |

196 tests green.

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

### ⏹️ CLOSED 2026-09-11 — kept below as the record of how it was run

⛔ **Nothing is armed and nothing is scheduled.** The crontab has **zero active job lines**,
all eight targets are `enabled: false` / `buy.enabled: false`, and the five dated `set_ceiling`
/ `stop_night` lines below carry **past** dates — do not uncomment them to reuse. The section
that follows is preserved because the ladder and its asymmetries are the interesting part, not
because any of it is still running.

### 🎯 The plan as it stood — 11/09 ONLY, on a rising ladder (Juan, 2026-09-06)

04/09 and 05/09 were attended, both bought **by hand at over R$300** because the
auto-buy never closed a purchase. Everything is now pointed at one night: **11/09
(Friday, the event day)**, one ticket, as cheap as possible.

| from | ceiling | why |
|---|---|---|
| now → Wed 09/09 | **R$ 200,00** | the target price; three of the five watched days have gone under it |
| Thu 10/09 00:00 | **R$ 250,00** | day before the show — a ticket is worth more than the saving |
| Fri 11/09 00:00 | **R$ 300,00** | event day |
| Fri 11/09 **16:00** | — | ⏰ **HARD STOP.** Disarm; buy nothing after. |

⚠️ **The ceiling is the fee-INCLUSIVE total.** `preco_min` is the checkout's `Valor
total` (RECON §"three traps"), so R$200 means R$200 out of pocket, not R$200 + 10% taxa.

⛔ **Every other night is disarmed AND unwatched** — 04, 05, 06, 07, 12, 13 all have
`enabled: false` and `buy.enabled: false`. Re-enabling one means editing both the target
file and the crontab's `--only` list.

⭐ **`entry_class` is deliberately `null` (any class).** Rock in Rio does not check the
ticket type at the gate — Juan attended on a *Meia Idoso* on 05/09. This matters more
than it looks: on 11/09 the `Inteira` class has been under R$200 on exactly **one** day,
while the sub-R$200 market is almost entirely *Meia Estudante / Até 21 / PCD / Professor*.
Restricting the class would quietly make the whole ladder unreachable.

### The schedule

```cron
# check every minute; costs ZERO extra requests to the site (it reads the history
# price-watcher just wrote) and only touches the network when it buys.
* * * * * /abs/path/.venv/bin/python /abs/path/buy.py autobuy >> /abs/path/state/autobuy.log 2>&1

# keep the session warm and shout within ~20 min when it dies (see below)
*/20 * * * * /abs/path/.venv/bin/python /abs/path/keepalive.py >> /abs/path/state/keepalive.log 2>&1

# the ladder, and the hard stop. ⏰ Delete all five after 11/09.
*/5 * * * * … set_ceiling.py rockinrio2026-09-11 2026-09-10T00:00 250 …
*/5 * * * * … set_ceiling.py rockinrio2026-09-11 2026-09-11T00:00 300 …
*/5 * * * * … stop_night.py  rockinrio2026-09-11 2026-09-11T16:00 …
*/5 * * * * … stop_night.py  rockinrio2026-09-11 2026-09-11T22:00 …
*/5 * * * * … stop_night.py  rockinrio2026-09-11 2026-09-12T04:00 …
```

⛔ **Three `stop_night` lines, not one.** It acts only inside `[deadline, deadline+6h)`,
so the chain 16:00 → 22:00 → 04:00 covers every hour from the hard stop until the buy
window has closed. A single 16:00 line would leave the night armed at R$300 if the box
happened to be asleep 16:00–22:00 — and it would then buy a ticket for a show that had
already ended.

⭐ **The ladder steps are MONOTONE (raise-only) and have no expiry window**, which is why
they are safe on a coarse cron forever and safe in any order: once R$300 has applied, the
R$250 line reads the file, sees 300 ≥ 250, and does nothing. That is deliberately the
opposite choice from `stop_night`, and the asymmetry is the reason — a `stop_night` that
fails to run leaves a night ARMED, so it must be bounded; a `set_ceiling` that fails to
run leaves the ceiling LOW, which spends nothing, so it re-asserts instead. A WSL box
asleep 23:00–07:00 would miss a 6-hour window entirely and hold R$200 through a day the
plan says to pay R$250.

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
| `set_ceiling` refuses a disarmed night | a ladder climbing over a night that already bought — the 04/09-at-R$1.000 bug |
| `set_ceiling` re-reads **inside** the lock | re-arming a night a concurrent buy just disarmed |
| `keepalive` saves only on a **positive** login | overwriting a working session with a logged-out (or half-rendered) one |

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

## 🔴 The session is the real limit — and it has already cost a ticket

Observed lifetimes: **~1 h 56 m and ~2 h 20 m**, not the "under four hours" earlier
drafts claimed. Expect to re-run `python buy.py login` more than once a day.

**2026-09-06, 10:34–11:10 BRT** is the case that proves it. `Gramado || Meia Até 21` sat
at **R$ 165,00** on the 11/09 night — R$35 under the ceiling, qty 26 — for **36 minutes**.
`autobuy` fired on it four times and every one died identically:

```
stopped: not authenticated at https://buyticketbrasil.com/entrar?… --
the browser is sitting on the login wall.
```

Nothing was wrong with the tool. The ceiling was never the binding constraint; the
session was, and **nothing said so until a dip arrived to discover it**.

### `keepalive.py` — the fix for the half that is fixable

```bash
python keepalive.py     # exit 0 alive · 3 dead (and Telegrammed)
```

On a `*/20` cron it does two things, and only the second is guaranteed:

1. **Warms** the session — loads the homepage with it and writes the refreshed
   `storage_state` back. ⚠️ This is a **hypothesis under test, not a known fix**: it
   extends nothing unless BuyTicket's TTL is *idle*-based. The observed deaths are
   consistent with either idle or absolute.
2. **Detects** — a dead session becomes a Telegram inside ~20 minutes, dip or no dip.
   This is the part `autobuy` structurally cannot do, and it is what earns the file.

⛔ It is a probe, never a login. Establishing a session is still a human at
`buy.py login`; automating it would mean storing the password, and a scripted login is
the least ordinary-looking thing this tool could do at the moment it most wants to look
ordinary (RECON §6, antifraud).

⛔ Its Telegram uses a **separate throttle key** from the buy path's. If they shared one,
a routine 20-minute probe could eat the slot belonging to the message that names a real
lost dip.
