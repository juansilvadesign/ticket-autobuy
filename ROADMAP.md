# ticket-autobuy — Roadmap

Deliberately short, like [`price-watcher`](../price-watcher/ROADMAP.md)'s. A personal
tool with an audience of one; this is a queue, not a plan.

## v1 — shipped 2026-09-02
Live checkout recon (`RECON.md`) · `resolve` / `login` / `map` / `buy` · the Pix leg,
after three wrong guesses · headless verified · `buy --dry-run` end to end.

## v1.1 — shipped 2026-09-02
**Unattended `autobuy`** — one reservation per invocation, one per night ever, inside a
BRT-pinned window, behind an exclusive `flock`, triggered by price-watcher's history and
authorised by a live re-resolve.

## v1.2 — shipped 2026-09-06
**The ladder and the hard stop.** `set_ceiling.py` (monotone, raise-only, no expiry) ·
three `stop_night.py` lines covering 16:00 → 22:00 → 04:00 · `keepalive.py`, the `*/20`
probe that turns a dead session into a Telegram inside 20 minutes instead of into a
missed dip nobody hears about. 114 tests green.

## Now — the 11/09 night, and nothing else
One ticket, cheapest possible, on a rising ladder R$ 200,00 → 250,00 (10/09) → 300,00
(11/09), ⏰ **HARD STOP 11/09 16:00 BRT**. Every other night is disarmed and unwatched.
Full detail in `README.md`; the open items are in `TASKS.md`.

## Next — a second event

**SOAD** — *System of a Down + Faith No More*, Maracanã, **15/01/2027**, sector
`Pista Premium Itaú Personalité`. The watch half shipped 2026-09-06 as
`../price-watcher/targets/soad2027-01-15.json` (verified live: 14 listings, 2 after
filters) and was **⏸ PARKED 2026-09-07** — `enabled: false` until Rock in Rio is
finished. ⛔ **Both halves are now waiting on the same event**, so the whole of SOAD
resumes in one pass after the 11/09 16:00 BRT hard stop.

- [ ] **Create script for SOAD** — ⛔ **deliberately NOT armed until after 11/09.**
      The two tools share **one** browser session whose observed lifetime is ~2 h.
      Arming a second night now would put SOAD in contention with the night that
      actually matters, on the exact resource that already cost a R$ 165,00 dip.
      Everything below is decided and ready to apply the moment 11/09 closes:
    - ✅ **Params, date and sector** — all verified against the live payload; the whole
      of `Pista Premium Itaú Personalité` is the **sector** and does not split.
    - ✅ **`max_price_brl: 400.00`**, and ⚠️ know what that arms: the floor was
      **R$ 1.197,90** on 2026-09-06, so this is *buy only if it crashes by two thirds*.
      The ceiling costs nothing while it waits, but it fires **never** at today's market
      and a quiet log will look identical to a working one.
    - ✅ **`entry_class: ["Meia Estudante", "Inteira"]`** — ⛔ **not `null`, which is the
      opposite of every Rock in Rio target.** Maracanã **does** verify the ticket type,
      so a *Meia Idoso / PCD / Professor* row is a ticket Juan is turned away with.
      Both listed classes are usable and for different reasons: he holds a Meia
      Estudante, and an Inteira needs no proof from anyone. ⭐ This is the `NEW_EVENT.md`
      §4 question paying for itself on the very first event it was applied to.
    - ⚠️ **Almost nothing rides on the class choice here**, unlike 11/09: *Meia
      Estudante* undercuts *Inteira* by **9%** (R$ 1.197,90 vs R$ 1.320,00), where on
      Rock in Rio the discounted classes were essentially the whole sub-R$200 market.
    - 🔲 **A ladder is probably not wanted** — the R$400 is a hard "otherwise I don't
      go", not a rising willingness to pay. Revisit only if that changes.
    - ⚠️ **Thin market: 2 rows against 21+ on a Rock in Rio night**, ~16 months out.
      ⛔ Do not treat today's floor as the trading range.
    - ➡️ Apply via `NEW_EVENT.md`.

- [x] ✅ **`NEW_EVENT.md` — the arming checklist for any future event** (2026-09-06).
      An interview, not a form: the site, the price range, the ticket type, and whether
      the venue verifies the ticket type. Written because every selector and every
      default in this repo was learned on **one** event at **one** venue — and the
      fourth question immediately produced a different answer on the second.

## Explicitly not planned

- **Paying.** It reserves; the ~10-minute hold on an unpaid Pix **is** the human gate.
  No saved card, no bank integration, no Pix automation. This is not a v1 limitation.
- **Storing the password, or a scripted login.** A session comes from a human at
  `buy.py login`. Automating it would be the least ordinary-looking thing this tool
  could do at the moment it most wants to look ordinary (`RECON.md` §6, antifraud).
- **Automatic retry of a failed checkout.** A half-finished flow may or may not have
  created a reservation, and a blind retry is how one intended ticket becomes two.
- **Account creation, listing, selling, queue-jumping or parallel sessions.**
- **Moving any of this into price-watcher.** The watcher stays stdlib-only and never
  buys; the browser lives here. The two talk through a `buy` key in a target file that
  price-watcher ignores.
