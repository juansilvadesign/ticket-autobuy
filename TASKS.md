# ticket-autobuy — Tasks

**State: autobuy ARMED on 11/09 ONLY, on a rising ladder — 114 tests green offline.**
The tool reserves and hands over a Pix code; it never pays. `buy --dry-run` passes end
to end and headless is verified. 🔲 **The final click and the Pix read are still the one
unverified leg** — two real orders exist, but neither was placed by `autobuy` closing a
purchase on its own. 🔴 The binding constraint is **not** the ceiling: it is the ~2 h
session, which killed a live R$ 165,00 dip on 06/09 at the login wall.

> **Provenance.** This file was reconstructed on 2026-09-06 from the 23 commits,
> `README.md`, `CLAUDE.md`, `RECON.md` and the project memory record — it was not kept
> as work happened. Every item below is sourced from one of those; where a claim is an
> inference rather than something someone watched, it says so. ⛔ Do not read a `[x]`
> here as "observed live" unless the line says it was.

## Done — recon (2026-09-02) → `RECON.md`

- [x] **The whole checkout mapped end to end, all 5 screens**, against the live site.
- [x] ⭐ **The finding the tool rests on: `matriz_preco[...].id_ref` *is* the event
      page's buy-button `c_anuncio`.** So the buyer navigates straight to
      `/r?event=&c_anuncio=` and never touches the ticket dropdowns — which matters
      because those dropdowns are the part that cannot be selected on reliably.
- [x] 🔴 **It is not a guest checkout.** It sits behind `/entrar`. This was assumed
      false and was wrong; it is why a session exists at all, and therefore why the
      session is now the project's real limit.
- [x] ⚠️ **Class names carry a build hash (`CLLKWG`) and change on any redeploy.**
      Encoded as an invariant: roles, `aria-label`s and text only, substring if forced.
- [x] **The buy button is inert until both dropdowns are set**; sold-out rows are marked
      and `matriz_preco` is not the whole picture.
- [x] **ToS checked: no anti-automation clause** — but antifraud scores *purchase
      behaviour*, which is the reason `login` stays a human at a real browser.
- [x] **`robots.txt` re-read and found unchanged** — and irrelevant to this path.
- [x] ⛔ **Three traps that each produced a confident, wrong result** are written down
      in `RECON.md` rather than fixed silently. The recon overturned five assumptions
      across two passes; "the DOM shows the price" (it shows one of nineteen) was one.
- [x] **Field mapping — all 9 fields matched by placeholder**, not by class.
- [x] **The checkout is a Bubble app behind a Next.js front end.**

## Done — v1, the buyer (2026-09-02)

- [x] Scaffold: venv, `requirements.txt`, `.gitignore`, `.env.example`,
      `fields.example.json` (`20d1529`).
- [x] **`buy.py` — resolve · login · map · buy**, reserving a listing and handing the
      Pix code to a human (`2564641`).
- [x] **Resolver + checkout guards tested against a real captured payload** (`ba18455`).
- [x] **Money is integer centavos through `Decimal(str(x))`** — never `int(x * 100)`,
      which turns `220.30` into `22029` and makes a ceiling one centavo low that never
      fires, silently, only on binary-inexact values.
- [x] **`ResolveError` (blind) and `NoMatch` (nothing cheap enough) never collapsed.**
      Inherited verbatim from price-watcher's `AdapterError` vs `[]`.
- [x] **A `buy` enabled without `max_price_brl` refuses to load** — an armed buy with no
      ceiling takes any price.
- [x] **Everything validated at startup, including the session.**
- [x] ✅ **Headless verified — and *faster* than headed.**
- [x] ✅ **`buy --dry-run` drives the whole flow and stops one click short.**

## Done — the Pix leg (2026-09-02) — three wrong guesses, in order

- [x] 🔴 **Guess 1 (wrong): the code renders on the checkout page.** It does not — the
      page sits on its `/checkout` URL showing "Aguarde…" and no code ever appears.
      Two real purchases were reported as **failures** before this was understood, and
      no amount of extra waiting would have fixed either: the tool was reading a page
      the payload does not appear on (`a89d336`, `f1f7a51`, `ed827eb`).
- [x] 🔴 **Guess 2 (wrong): it is in the DOM.** It is on the **clipboard** —
      `91bc14f`, pinned by `b1413fb` along with the misleading recovery message.
- [x] 🔴 **Guess 3 (wrong): the first pending order is the right one.** A *stale*
      pending hold shadowed the run's real order; the fix tries every pending order
      (`2432f89`), pinned by `027c7bc`.
- [x] **The code's real home: `/ingressos` → Comprados → the pending row.**
      ⛔ Never point a human at the *checkout* URL to recover it — reopening that starts
      a FRESH checkout showing no order, which reads as "nothing was reserved" while a
      real reservation runs down its clock. That misread happened live on 2026-09-02.
- [x] ⏰ **The hold is ~10 minutes, not ~30** — measured on order **#7707X57Q**, whose
      checkout says *"Você tem 10 minutos para completar sua compra"*. Every earlier doc
      said 30. Not a rounding error: three times the real margin for a human who has to
      be handed a code and pay it (`dd3173c`).
- [x] **The alert-to-code budget is measured phase by phase** (`9853034`), and the
      per-candidate budget is parameterised to keep the suite fast (`ee9c0e4`).
- [x] **The Pix code prints to stdout BEFORE any network call.** Telegram is the
      convenient path, not the load-bearing one. A delivery failure is raised, never
      swallowed → exit 3. The QR *image* is the one exception: it rides on a code that
      already landed, so its failure must not turn a success into exit 3.

## Done — unattended auto-buy (2026-09-02)

- [x] **`buy.py autobuy`** — a cron entry point that fires once per night on a dip
      (`923a3d9`), documented in `97b3ef2`, tested in `809cea8` *including the invariant
      the first implementation got wrong*.
- [x] **One reservation per invocation; one per night, ever.** Ledger in
      `state/autobuy.json`, plus the target disarms itself the moment it buys.
- [x] ⛔ **Exclusive `flock` — load-bearing, not hygiene.** A buy takes ~40 s and cron
      fires every minute, so invocations *will* overlap. Two runs seeing one dip is
      "one intended ticket becomes two" arriving by schedule instead of by retry.
- [x] ⛔ **The fire is recorded in the ledger BEFORE notifying.** A delivery failure must
      never leave a real reservation absent from the ledger — that is how the next
      minute buys a second ticket for the same night.
- [x] ⛔ **The window is pinned to `America/Sao_Paulo`, never the daemon's TZ.** cron on
      WSL frequently runs UTC, where a 10:00–04:00 window becomes 07:00–01:00 BRT: it
      refuses to buy for three morning hours *and* fires at 02:00 while nobody is awake
      to pay. Both halves are invisible in a log.
- [x] **The trigger is price-watcher's history; the authority is a live re-resolve.**
      Reading the file the watcher just wrote costs zero extra requests; `cmd_buy`
      re-resolves before it spends, so an evaporated dip becomes a `NoMatch`.
- [x] ⛔ **History age is NOT freshness — it is volatility.** price-watcher appends only
      when a reading *changes*, so a healthy poller on a stable market writes nothing
      for hours. An age cutoff would call a price that dropped to R$150 and *held*
      "blind" and skip the exact dip this tool exists for. Liveness comes from the
      poller's own cron log via `assert_poller_alive`.
- [x] ⛔ **Never retry a failed checkout automatically.** A half-finished flow may or may
      not have created a reservation.

## Done — the 11/09 replan, the ladder and keepalive (2026-09-04 → 06)

- [x] **Notification + runner flow reworked** across `649ca13`, `a4672f7`, `12729d7`,
      `2e0da84` (checkout logic + error-handling tests).
- [x] 🔴 **The plan changed (Juan, 2026-09-06): 11/09 ONLY.** 04/09 and 05/09 were
      attended and both tickets were bought **by hand at over R$300** — the auto-buy
      never closed a purchase. Everything now points at one night, one ticket, cheapest
      possible. ⛔ 04, 05, 06, 07, 12, 13 are all disarmed **and** unwatched.
- [x] **`set_ceiling.py` — the rising ladder** (`fc90390`, 14 tests in `test_ladder.py`):
      R$ 200,00 now → **R$ 250,00 from 10/09 00:00** → **R$ 300,00 from 11/09 00:00**.
- [x] ⚠️ **The ceiling is the fee-INCLUSIVE total.** `preco_min` is the checkout's
      *Valor total*, so R$200 means R$200 out of pocket, not R$200 + 10% taxa.
- [x] ⭐ **The ladder steps are MONOTONE (raise-only) with no expiry window**, which is
      why they are safe on a coarse cron forever and safe in any order. That is
      deliberately the **opposite** choice from `stop_night`, and the asymmetry is the
      reason: a `stop_night` that fails to run leaves a night ARMED, so it must be
      bounded; a `set_ceiling` that fails to run leaves the ceiling LOW, which spends
      nothing, so it re-asserts instead.
- [x] ⛔ **Three `stop_night` lines, not one** — 16:00 → 22:00 → 04:00. It acts only
      inside `[deadline, deadline+6h)`, so a single 16:00 line would leave the night
      armed at R$300 if the box were asleep 16:00–22:00 — and it would then buy a ticket
      for a show that had already ended.
- [x] 🔴 **`set_ceiling` refuses a disarmed night** — the fix for the **04/09-at-R$1.000
      bug**, where a ladder climbed over a night that had already bought. It also
      **re-reads inside the lock**, so it cannot re-arm a night a concurrent buy just
      disarmed.
- [x] ⭐ **`entry_class` is deliberately `null` (any class)** — Rock in Rio does not check
      ticket type at the gate; Juan attended on a *Meia Idoso* on 05/09. This matters
      more than it looks: on 11/09 the `Inteira` class has been under R$200 on exactly
      **one** day, while the sub-R$200 market is almost entirely *Meia Estudante / Até
      21 / PCD / Professor*. Restricting the class would quietly make the ladder
      unreachable. ⚠️ **This is a per-venue fact, not a general one** — see
      `NEW_EVENT.md`, which asks it as a question for every new event.
- [x] 🔴 **`keepalive.py` — the fix for the half that is fixable** (`fc90390`).
      On a `*/20` cron it **detects** a dead session and Telegrams within ~20 minutes,
      dip or no dip. That is the part `autobuy` structurally cannot do.
      ⛔ It is a probe, never a login. ⛔ Its Telegram uses a **separate throttle key**
      from the buy path's, so a routine probe cannot eat the slot belonging to the
      message that names a real lost dip. `keepalive` saves only on a **positive**
      login, so it cannot overwrite a working session with a logged-out one.
- [x] **114 tests green offline** — `test_listing` 44 · `test_runner` 40 ·
      `test_ladder` 14 · `test_checkout_errors` 8 · `test_notify` 8. Run 2026-09-06.

## 🔴 The failure that defines the project — 2026-09-06, 10:34–11:10 BRT

- [x] **Recorded, not guessed.** `Gramado || Meia Até 21` sat at **R$ 165,00** on the
      11/09 night — R$35 under the ceiling, qty 26 — for **36 minutes**. `autobuy` fired
      on it **four times** and every one died identically: `stopped: not authenticated
      … the browser is sitting on the login wall`.
- [x] **Nothing was wrong with the tool.** The ceiling was never the binding constraint;
      the session was — and **nothing said so until a dip arrived to discover it**.
      That is the whole argument for `keepalive.py`, and the reason a dead session must
      SHOUT rather than degrade.
- [x] **Observed session lifetimes: ~1 h 56 m and ~2 h 20 m** — not the "under four
      hours" earlier drafts claimed. Expect to re-run `buy.py login` more than once a day.

## Next — in priority order

- [ ] ⏰🔴 **Do NOT delete the five dated cron lines before 2026-09-12.** They were
      deleted five days early once (06/09), which silently killed the ladder **and** the
      hard stop, and were restored. After 11/09 they are dead weight and should go.
      ⛔ Verify on **ACTIVE** crontab lines only — a bare `grep` also matches the
      comments, which is how "they're still there" was believed once while they were not.
- [ ] ⏰🔴 **HARD STOP 11/09 16:00 BRT.** Disarm; buy nothing after.
- [ ] 🔲 **The final click and the Pix read remain UNVERIFIED end to end.** Two real
      orders exist and the code was recovered from Comprados both times, but `autobuy`
      has never closed a purchase by itself. Until 11/09 this cannot be retired.
- [ ] 🔴 **`keepalive`'s *warming* leg is a hypothesis under test, not a known fix.**
      It extends nothing unless BuyTicket's TTL is *idle*-based, and the observed deaths
      are consistent with either idle or absolute. Its **detection** leg is what earns
      the file. ⚠️ Do not record "session fixed" on the strength of a quiet night.
- [x] ✅ **`README.md`'s test count corrected 27 → 114** (2026-09-06). It was stale by
      4×, and a test count is exactly the kind of number that is believed without being
      re-run. ⚠️ It is hand-maintained and will go stale again — re-read it from
      `pytest -q`, never from the last thing a doc said.
- [ ] **SOAD — the next event** (Maracanã, 15/01/2027). ✅ **Every decision is now
      made** and the watch half has shipped; see `ROADMAP.md`. ⛔ The buy half stays
      **unarmed until after 11/09** — one ~2 h session, and 11/09 gets it.
      ⭐ Both open questions resolved themselves rather than needing a compromise:
      `critical_price` at R$400 is coherent because `lowest_ever` is off, so the anomaly
      floor has no baseline to protect; and Maracanã **does** verify the ticket type,
      which is why `entry_class` is a list there and `null` here.

## Known-unknown — do not assume

- [ ] **Whether the session TTL is idle-based or absolute.** Everything `keepalive`
      warms depends on this, and it has not been measured.
- [ ] **Whether `autobuy` survives the last click.** See above — expected, not observed.
- [ ] **Whether a second event behaves like Rock in Rio at all.** Every selector, the
      `entry_class: null` choice and the fee-inclusive `preco_min` reading were learned
      on **one** event at **one** venue. `NEW_EVENT.md` exists to stop those being
      carried over as facts.
      ⭐ **First evidence, 2026-09-06:** a read-only probe of the SOAD event parsed
      cleanly through the existing adapter — 14 listings, 8 sectors, sector/entry-class
      split intact. So the *adapter* generalises within this site. ⛔ The **defaults**
      demonstrably do not: the discounted class undercuts `Inteira` by 9% there against
      ~50% on Rock in Rio, and the market is 2 rows deep instead of 21.
- [ ] ⛔ **The refund window for the 04/09 night has already closed** (`RECON.md` §7) —
      a reserved-and-paid mistake is not recoverable by refund on this site.
