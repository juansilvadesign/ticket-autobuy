# ticket-autobuy — Project Identity (Layer 2)

Reserves a BuyTicket listing when the price drops, and hands the **Pix code to a human**.
Personal, audience of one. Sibling of [`price-watcher`](../price-watcher/), never part of it.

Recon that produced it: [`RECON.md`](RECON.md) — read it before touching a selector.

## Why this is a separate project

price-watcher's `CLAUDE.md` states, as a permanent boundary: *"It never buys. No
checkout, no card, no order placement, no queue automation, no authenticated session."*

That boundary is **kept, not dropped**. This tool is where buying lives; the watcher
does not gain a buy path. Two consequences follow, and both are load-bearing:

- **price-watcher stays stdlib-only.** Driving a checkout needs a browser. Putting
  Playwright in the watcher would mean the thing that must still run after six months
  untouched now needs a venv rebuilt first.
- **The two communicate through data, not imports.** This tool *reads* a price-watcher
  target file and adds one key (`buy`) that price-watcher ignores. Nothing in
  `pricewatch/` changed, and nothing in it needs to.

## The one architectural rule

**It reserves; it never pays.** The site holds a reserved ticket against an unpaid Pix.
That hold **is** the human gate: the worst a bug can do is create a reservation that
lapses on its own. Nothing in this repo may ever acquire the ability to settle a payment
— not a saved card, not a bank integration, not a Pix automation. This is not a v1
limitation.

⏰ **The hold is ~10 minutes, not ~30.** Measured on the first real order (#7707X57Q,
2026-09-02): the checkout says *"Você tem 10 minutos para completar sua compra"* and the
order screen counts down from there. Every earlier version of this file said 30, which
is not a rounding error — it is three times the real margin for a human who has to be
handed a code and pay it.

## Invariants — do not quietly break these

- **Money is integer centavos.** `brl_to_cents` goes through `Decimal(str(x))`, never
  `int(x * 100)` — the float path turns `220.30` into `22029`, and a ceiling one centavo
  low simply never fires, silently, only on binary-inexact values.
- **`ResolveError` means blind; `NoMatch` means nothing was cheap enough.** Never
  collapse them. A resolver that returned "no match" on a changed payload would report
  "nothing under your price" forever and look exactly like a market that never dipped.
  Inherited verbatim from price-watcher's `AdapterError` vs `[]`.
- **A buy enabled without `max_price_brl` must refuse to load.** An armed buy with no
  ceiling takes any price. Same shape as a price-watcher rule enabled without its
  parameter: it looks configured and can never behave.
- **Everything is validated at startup, including the session.** The checkout is behind
  `/entrar`; discovering a dead session at the payment step is discovering it after the
  listing you wanted is gone.
- **The Pix code is printed to stdout BEFORE any network call**, then delivered.
  Telegram is the convenient path, not the load-bearing one.
- **A delivery failure is raised, never swallowed** — exit 3, matching price-watcher.
  A notifier that fails quietly here does not degrade the feature, it deletes it while
  logging success. The QR *image* is the one exception: it rides on top of a code that
  already landed, so its failure must not turn a complete success into exit 3.
- **The only ENFORCEABLE budget is the walk: resolve → the order-creating click**
  (`WALK_BUDGET_S`, 45 s, checked before each screen and once immediately before the click).
  ⛔ Never enforce a budget after that click — a reservation may be live and its code must be
  captured however long it takes. `BUDGET_S` (60 s) is a REPORT and always was; it measures a
  span whose overrun sits entirely past the point of no return, so it could only ever fire
  where firing is forbidden. Print both, and label which one binds.
- **A refusal to click is a no-op, not a failed purchase.** `BudgetExceeded` and
  `NoOrderCreated` are strictly pre-click: no ledger row, no disarm, the night stays ARMED,
  exit 0. Only `OrderMayExistError` — genuine ambiguity after the click — fails closed.
  ⛔ `NoOrderCreated` needs TWO independent signals (the page bounced out of the flow AND
  Comprados rendered with zero pending rows); either alone still fails closed, because a false
  negative leaves a real reservation unrecorded and the next cron minute buys a second ticket.
- **A night that is switched OFF must say so.** A fired/disarmed target used to be skipped by a
  bare `continue`, which is byte-identical to a calm market: on 2026-09-10/11 that hid a disarm
  for 25 h while 67 in-window polls sat under the ceiling. ⛔ Distinguish *closed by a failure*
  (ledger row → announce, and nag while `UNCONFIRMED`) from *parked by hand* (no ledger row →
  stay silent), and never nag about a night that really bought.
- **Ship the verification step as a command.** `buy.py status` (is anything armed?) and
  `buy.py orders` (what does Comprados actually hold?) are read-only and take no lock.
  ⛔ A verification this file *demands* but provides no instrument for is one that gets skipped —
  which is exactly how an `UNCONFIRMED` ledger row went unchecked for a day.
  ⚠️ `orders` reports an unreachable tab as **exit 3**, never as "nothing pending": "I could not
  look" must never print like "I looked and it was clean".
- **Never select on a CSS-module class name.** They carry a build hash (`CLLKWG`) that
  changes on any redeploy. Roles, `aria-label`s and text; substring match if forced.
- ⛔ **Never retry a failed checkout automatically.** A half-finished flow may or may not
  have created a reservation, and a blind retry is how one intended ticket becomes two.
  Re-read the state and decide by hand.
- **The Pix code is never on the checkout page.** The final click leaves the page on
  its `/checkout` URL showing "Aguarde…", and no code ever renders there. It is on the
  order — `/ingressos` → **Comprados** → the pending row. Two real purchases were
  reported as failures before this was understood; a longer wait would never have fixed
  either, because the tool was reading a page the payload does not appear on.
- **An order without a captured code is bad, but RECOVERABLE — say so.** The order is
  findable afterwards at `/ingressos` → **Comprados**, and its code re-readable from the
  order screen. ⛔ Never point a human at the *checkout* URL to look for it: reopening
  that starts a FRESH checkout showing no order, which reads as "nothing was reserved"
  while a real reservation runs down its clock. That misread happened, live, on
  2026-09-02 before the Comprados tab settled it.
- ⛔ **`.env` is parsed, never sourced.**
- ⛔ **The session file is worth more than the password** — it skips the login gate
  entirely. Same for `fields.json` (CPF, address). Both gitignored *in this tree*: a
  sibling project inherits **no** parent `.gitignore`, which is exactly how a live
  `cookies.txt` once reached a public repo in this workspace.

## Boundaries

- **No account creation, no listing, no selling.** It buys what a human already decided
  to buy, on an account a human already made.
- **No credential storage.** The session comes from `buy.py login`, where a human types
  the password into a real browser.
- **One reservation per invocation. One per night, ever.** ⚠️ This SUPERSEDES the
  original "no loops" rule, and it is narrower than it sounds — every clause carries
  weight. `buy.py autobuy` is a cron entry point that may reserve **at most one** ticket
  per run, on **at most one** night ever (a ledger in `state/`, plus the target disarms
  itself the moment it buys), inside a **time window**, behind an **exclusive lock**.
  - ⛔ The lock is load-bearing, not hygiene. A buy takes ~40 s and cron fires every
    minute, so invocations *will* overlap; two runs seeing one dip is "one intended
    ticket becomes two" arriving by schedule instead of by retry.
  - ⛔ Record the fire in the ledger **before** notifying. A delivery failure must never
    leave a real reservation absent from the ledger — that is how the next minute buys
    a second ticket for the same night.
  - Still no queue-jumping and no parallel sessions.
- **The trigger is price-watcher's history; the authority is a live re-resolve.** Reading
  the file price-watcher just wrote costs zero extra requests, where a second 7-night
  poller would nearly double a request budget the README treats as a design input.
  `cmd_buy` re-resolves live before it spends, so a dip that has evaporated becomes a
  `NoMatch` — stale-by-a-minute is safe in the only direction that matters.
- ⛔ **History age is NOT freshness — it is volatility.** price-watcher appends only when
  a reading *changes* (`unchanged since the last recording — nothing written`), so a
  healthy poller on a stable market writes nothing for hours. An age cutoff over those
  files would call a price that dropped to R$150 and *held* "blind" and skip the exact
  dip this tool exists for. Liveness comes from the poller's own cron log, which is
  touched on every run. Carry the last reading forward, whatever its age.
- ⛔ **The window is pinned to `America/Sao_Paulo`, never the daemon's TZ.** cron inherits
  whatever the daemon has — frequently UTC on WSL — and a 10:00–04:00 window read in UTC
  becomes 07:00–01:00 BRT: it refuses to buy for three morning hours *and* fires at 02:00
  while nobody is awake to pay. Both halves are invisible in a log.
- ⛔ **A dead session must SHOUT.** It is the failure that deletes this feature rather than
  degrading it: the observed session lasted under four hours, and without an alert the
  cron goes on finding dips and failing to buy them into a log nobody reads. Alerted on
  Telegram, throttled — 1,440 identical messages a day would mute the channel the Pix
  code arrives on.

## Before writing a checkout selector

Run `python buy.py map` and read the dump. The site's own recon overturned five
assumptions across two passes — including "it's a guest checkout" (it is not) and
"the DOM shows the price" (it shows one of nineteen). Assume the same about the next
step you have not personally watched.
