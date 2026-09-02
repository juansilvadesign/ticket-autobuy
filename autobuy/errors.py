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
