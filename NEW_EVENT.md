# Arming a new event — the buy half

⛔ **Do the watch half first.** This tool has no poller of its own: `autobuy` fires off
the history **price-watcher** writes. Until that target exists and is on a cron line,
nothing here can trigger, and it will look exactly like a market that never dipped.

➡️ [`../price-watcher/NEW_TARGET.md`](../price-watcher/NEW_TARGET.md) — the same four
questions, answered for the watching side.

---

## The four questions, and what they cost *here*

The watch half asks these to decide what to *record*. This half asks the same four to
decide what to *spend*. ⛔ Answer them again; the answers are not interchangeable.

### 1 · Which site?

Only **buyticketbrasil** is mapped. `RECON.md` is a map of *one* checkout, and it
overturned five assumptions across two passes to get there — including "it's a guest
checkout" (it is not) and "the DOM shows the price" (it shows one of nineteen).

⛔ **A different site is not a config change; it is a new recon and a new checkout
driver.** Assume nothing carries over, and re-run `buy.py map` before writing a single
selector. Never select on a CSS-module class name — they carry a build hash that changes
on any redeploy.

### 2 · What is the price range?

> **Q: what is the most you would pay, all-in?** → `buy.max_price_brl`

- ⚠️ **The ceiling is the fee-INCLUSIVE total.** `preco_min` is the checkout's *Valor
  total*, so R$400 means R$400 out of pocket, not R$400 + 10% taxa.
- ⛔ **A `buy` enabled without `max_price_brl` refuses to load.** An armed buy with no
  ceiling takes any price.
- **`min_price_brl` is optional and OFF by default**, and that is the opposite of
  price-watcher's mandatory floor. Deliberate: there, a mispriced row poisons
  `lowest_ever` permanently; here a suspiciously cheap row is a *decision to put in
  front of you*. The bot reserves it, you look, and you either pay or let the hold lapse.
  ⛔ Do not "fix" this by copying the watcher's floor across — two jobs, one number.
- **Will the price move over the run-up?** If so you want a **ladder**, §6.
- ⚠️ **Check the ceiling against the actual floor before arming it.** A ceiling far below
  the trading range is a coherent stance — *buy only if this crashes* — and it costs
  nothing while it waits. But it fires **never** at today's market, and a quiet log looks
  identical to a working one. Know which you are arming: SOAD's R$400 sits ~3× under a
  R$ 1.197,90 floor, where 11/09's R$200 sat just under R$214,50.

### 3 · Which ticket type?

`buy.sector` and `buy.entry_class` — the same strings as the watch half, read off a live
payload, never guessed. `buy.quantity` is how many, and `buy.min_available` guards
against a row that cannot actually fill the order.

```bash
python buy.py resolve --target ../price-watcher/targets/<id>.json   # buys nothing
python buy.py map     --target ../price-watcher/targets/<id>.json   # dumps every control
```

### 4 · 🔴 Does the venue check the ticket type at the gate?

**This is the question that decides whether the ceiling is reachable at all**, and it is
the one most likely to be answered from habit.

| answer | `entry_class` | consequence |
|---|---|---|
| **No** | `null` — any class | ⭐ The cheap market **is** the discounted market. On Rock in Rio's 11/09 night `Inteira` went under R$200 on exactly **one** day, while the sub-R$200 rows were almost entirely *Meia Estudante / Até 21 / PCD / Professor*. Restricting the class would have made the whole ladder unreachable — silently, while looking configured. |
| **Yes** | only what you can present | A cheap row you are turned away with is not a saving. Expect a thinner market; raise the ceiling accordingly or accept fewer chances. |
| **Unknown** | ⛔ **do not arm** | Both wrong answers are invisible in a log: one buys a ticket you cannot use, the other never fires. |

⛔ **Rock in Rio does not check — that is a fact about Rock in Rio**, established by
attending on a *Meia Idoso* on 05/09. It is not a fact about the site, not a fact about
the next venue, and not a default.

⚠️ **Measure what the answer is worth before spending effort on it.** On 11/09 the
discounted classes were essentially the whole sub-R$200 market, so the choice decided
whether the ladder could reach at all. In SOAD's `Pista Premium Itaú Personalité`,
*Meia Estudante* undercuts *Inteira* by **9%** (R$ 1.197,90 vs R$ 1.320,00) — the same
question, with almost nothing riding on it.

---

## 5 · Add the `buy` block

To the **same** price-watcher target file. price-watcher ignores the key.

```json
"buy": {
  "enabled": true,
  "max_price_brl": 400.00,
  "quantity": 1,
  "sector": ["<exact string from a live read>"],
  "entry_class": null,
  "min_available": 1
}
```

`enabled: true` means **armed**, never "will buy repeatedly": the runner fires only
inside its window and disarms this block the moment it buys.

## 6 · The ladder and the hard stop

If the ceiling should rise as the event approaches, use `set_ceiling.py`; if there is a
point past which buying is pointless, use `stop_night.py`. Both are cron lines.

- ⭐ **`set_ceiling` is MONOTONE (raise-only) and has no expiry window**, so it is safe on
  a coarse cron forever and safe in any order — once R$300 has applied, an R$250 line
  reads the file, sees 300 ≥ 250, and does nothing.
- ⛔ **`stop_night` is bounded — it acts only inside `[deadline, deadline+6h)` — so it
  needs a CHAIN of lines**, e.g. 16:00 → 22:00 → 04:00. The asymmetry is the reason: a
  `stop_night` that fails to run leaves a night **ARMED**, while a `set_ceiling` that
  fails to run leaves the ceiling **LOW**, which spends nothing. A WSL box asleep
  23:00–07:00 misses a 6-hour window entirely.
- 🔴 **`set_ceiling` refuses a disarmed night**, which is the fix for the
  04/09-at-R$1.000 bug — a ladder climbing over a night that had already bought.
- ⏰ **Dated cron lines are temporary and must be deleted after the event — and NOT
  BEFORE.** Deleting them early kills the ladder *and* the stop in one move; that has
  happened once. ⛔ When you check whether they are still installed, check **ACTIVE**
  lines only: a bare `grep` also matches the commented ones.

## 7 · Cron

```cron
# reads the history price-watcher just wrote; costs ZERO extra requests to the site
# and only touches the network when it actually buys.
* * * * * /abs/path/.venv/bin/python /abs/path/buy.py autobuy >> /abs/path/state/autobuy.log 2>&1

# the session probe — a dead session becomes a Telegram inside ~20 minutes
*/20 * * * * /abs/path/.venv/bin/python /abs/path/keepalive.py >> /abs/path/state/keepalive.log 2>&1
```

⚠️ **The venv python is load-bearing.** `autobuy` drives Playwright, which
`/usr/bin/python3` does not have. price-watcher's cron lines deliberately use the system
python; these must not.

⛔ **The window is pinned to `America/Sao_Paulo`, never the daemon's TZ.** cron on WSL is
frequently UTC, where a 10:00–04:00 window becomes 07:00–01:00 BRT: it refuses to buy for
three morning hours *and* fires at 02:00 while nobody is awake to pay.

## 8 · Prove it before you trust it

```bash
python buy.py login                      # a human, in a real browser. Tick "Lembrar de mim".
python buy.py buy --dry-run --target ../price-watcher/targets/<id>.json
python keepalive.py                      # 0 alive · 3 dead (and Telegrammed)
.venv/bin/python -m pytest tests -q      # 114 tests, offline
```

## 9 · 🔴 The thing that will actually break it

**Not the ceiling — the session.** Observed lifetimes are ~1 h 56 m and ~2 h 20 m, so
expect to re-run `buy.py login` more than once a day.

On 2026-09-06, 10:34–11:10 BRT, a `Gramado || Meia Até 21` row sat at **R$ 165,00** —
R$35 under the ceiling, qty 26 — for **36 minutes**. `autobuy` fired four times and every
one died at the login wall. Nothing was wrong with the tool, and **nothing said so until
a dip arrived to discover it.** That is what `keepalive.py` exists for, and why its
detection leg matters more than its warming leg (which is still only a hypothesis about
whether the site's TTL is idle-based).

⛔ Arming a new event without `keepalive` on a cron means finding out the same way.
