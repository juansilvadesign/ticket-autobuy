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
- **One reservation per run.** No loops, no queue-jumping, no parallel sessions.

## Before writing a checkout selector

Run `python buy.py map` and read the dump. The site's own recon overturned five
assumptions across two passes — including "it's a guest checkout" (it is not) and
"the DOM shows the price" (it shows one of nineteen). Assume the same about the next
step you have not personally watched.
