"""Failure shapes. The distinctions here are the same ones price-watcher draws.

Collapsing any two of these produces a tool that looks like it is working and is not,
which on a buy night is the only failure mode that actually costs anything.
"""

from __future__ import annotations


class AutobuyError(RuntimeError):
    """Base for every deliberate refusal in this package."""


class ResolveError(AutobuyError):
    """The site could not be read at all -- transport failure, or the payload shape is
    gone. It is BLINDNESS, never "nothing matched".

    The sibling of price-watcher's `AdapterError`, and it exists for the same reason:
    a resolver that returned "no match" on a changed payload would report "no ticket
    under your price" forever, on every run, and look exactly like a market that never
    dipped. You would find out by never being alerted.
    """


class NoMatch(AutobuyError):
    """The read succeeded and nothing satisfied the buy config.

    This is DATA -- the ordinary outcome of almost every run. Distinct from
    `ResolveError` in the same way `[]` is distinct from `AdapterError`.
    """


class ConfigError(AutobuyError):
    """The buy block is unusable -- enabled without its parameters, or self-contradictory.

    Refused at load, never at buy time. price-watcher's registry makes the same call:
    a rule enabled without its threshold must refuse to load, because a buy config that
    is discovered to be broken at the moment it was supposed to fire has cost you the
    thing it existed to get.
    """


class SessionError(AutobuyError):
    """There is no usable logged-in session.

    Raised at startup, before anything races. The BuyTicket checkout is behind
    `/entrar`, so a missing session means the run cannot possibly complete -- finding
    that out one step before the Pix screen is finding it out too late.
    """


class CheckoutError(AutobuyError):
    """The checkout flow diverged from what the recon mapped.

    Always raised, never swallowed and never retried automatically. A half-finished
    checkout may or may not have created a reservation, and a blind retry is how one
    intended ticket becomes two. Re-read the state, then decide by hand.
    """


class BudgetExceeded(CheckoutError):
    """The walk to the order-creating click took longer than the exposure budget.

    ⛔ STRICTLY pre-click, and that is the only place a budget can be enforced at all.
    After the click a reservation may be live and capturing its code is the only thing
    that matters -- aborting there to save time would abandon a real unpaid hold, which
    is worse than any overrun. So this is raised BEFORE the click, never after, and it
    does not disarm the night: nothing was ordered and the next cron minute retries.

    ⭐🔴 Why a WALK budget and not the old 60 s total: on both 2026-09-10 failures the
    run was ~30 s at the click and ~83 s at the end, the whole overrun sitting in the
    47 s post-click Pix read. A 60 s TOTAL budget would therefore have fired at a moment
    when firing is forbidden, and never during the window it was meant to protect. The
    number was measuring the wrong span.
    """


class NoOrderCreated(CheckoutError):
    """The final click was made and provably created NOTHING.

    ⭐🔴 The sibling `OrderMayExistError` lacked, and the gap cost the 11/09 night.
    That error means "I cannot tell", and it correctly fails CLOSED -- ledger row plus
    disarm. This one means "I looked, and there is no order", which is a different fact
    and must NOT disarm: the listing simply evaporated before the click landed, which is
    the single most likely outcome on a fast market.

    ⛔ It is raised ONLY on two INDEPENDENT signals agreeing, never one:
      (a) the post-click page BOUNCED -- its URL is neither the checkout nor the orders
          page, so the click never reached an order-creating endpoint; and
      (b) the Comprados tab rendered and showed ZERO pending rows.
    Either alone stays `OrderMayExistError`. A false negative here is the catastrophic
    direction -- it would leave a real reservation out of the ledger and let the next
    cron minute buy a SECOND ticket -- so the bar is deliberately two signals, not one.

    Observed 2026-09-10, twice (10:55 and 14:24), both on the 11/09 night:
    `receipts/rockinrio2026-09-11/order.png` is the event DATE-PICKER page ("3 datas",
    11/12/13 Set), not a checkout and not an order. Juan verified `/ingressos` ->
    Comprados by hand after the first: no pending and no bought ticket. Both runs were
    nonetheless graded `OrderMayExistError`, and the second disarmed the night for good.
    """


class OrderMayExistError(CheckoutError):
    """Raised ONLY after the order-creating click, when the Pix code could not be read.

    ⛔ A distinct type, not a message, because the caller has to bookkeep differently:
    a reservation may be live, so the ledger and the disarm must fire. Every OTHER
    `CheckoutError` (nothing rendered, no control found, ran out of screens) happens
    strictly BEFORE the click and must NOT take a night out of play.

    Observed 2026-09-04: string-blind handling of `CheckoutError` recorded and disarmed
    11/09 for "checkout did not reach a final control in 8 screens" -- a pre-click
    failure. No order existed; the night was simply lost for the evening.
    """
