"""How a FAILED CLICK is graded — the checkout's error grammar.

⛔🔴 Observed live 2026-09-05 13:05 BRT on a real R$132,00 dip. Bubble's busy overlay
(`<div class="greyout">`) intercepted the "Continuar" click on screen 2 and Playwright
raised its own `TimeoutError` — a class this package's error grammar does not know. It
travelled straight out of `run_checkout` -> `_execute_buy` -> `cmd_autobuy` -> `main()`
uncaught: raw traceback, exit 1, which `buy.py` documents as "usage or config error".
Neither the `CheckoutError` nor the `OrderMayExistError` branch ran.

That run was harmless — screen 2, strictly pre-click, and `/ingressos` -> Comprados
confirmed no order. The hazard is the SAME escape from the order-creating click, where
skipping `OrderMayExistError` means no ledger write and no disarm, and the night stays
armed for the next cron minute to reserve a SECOND ticket.

⭐ These tests raise a class that is NOT playwright's, deliberately: the grading must
depend on WHERE the click failed, never on which exception type the driver chose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autobuy import checkout                                          # noqa: E402
from autobuy.errors import CheckoutError, OrderMayExistError          # noqa: E402


class _DriverBlewUp(Exception):
    """Stands in for `playwright...TimeoutError` — an alien class, on purpose."""


class _Btn:
    def __init__(self, exc: Exception | None = None):
        self.exc, self.clicks = exc, 0

    def click(self, **kw):
        self.clicks += 1
        if self.exc is not None:
            raise self.exc


class _Page:
    url = "https://buyticketbrasil.com/checkout?p=2"

    def wait_for_timeout(self, ms):  # noqa: D102
        pass

    def screenshot(self, **kw):      # noqa: D102
        pass


def _world(monkeypatch, *, first_visible, price_cents=13200):
    """A checkout whose every collaborator is inert, so only the CLICK is under test."""
    monkeypatch.setattr(checkout, "settle", lambda page, **k: [{"x": 1}])
    monkeypatch.setattr(checkout, "_actionable", lambda e: True)
    monkeypatch.setattr(checkout, "_select_pix", lambda page: False)
    monkeypatch.setattr(checkout, "fill_known_fields", lambda page, person: [])
    monkeypatch.setattr(checkout, "_page_price_cents", lambda page: price_cents)
    monkeypatch.setattr(checkout, "_first_visible", first_visible)
    return _Page()


def test_an_intercepted_CONTINUAR_click_is_a_CheckoutError(monkeypatch):
    """Screen 2's "Continuar" — strictly BEFORE the order exists.

    ⛔ Must NOT be an `OrderMayExistError`: that type records the fire in the ledger and
    disarms the night, so mis-grading a pre-click failure takes a night out of play for
    an order that was never created. Exactly the 2026-09-04 regression that
    `test_a_PRE_click_checkout_failure_does_NOT_take_the_night_out_of_play` pins.
    """
    btn = _Btn(_DriverBlewUp("greyout intercepts pointer events"))
    page = _world(monkeypatch, first_visible=lambda p, text, limit=12:
                  btn if text == "Continuar" else None)

    with pytest.raises(CheckoutError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert not isinstance(ei.value, OrderMayExistError), "pre-click must not bookkeep"
    assert btn.clicks == 1
    assert "greyout" in str(ei.value), "the driver's own reason must survive"


def test_an_intercepted_SCREEN_1_click_is_a_CheckoutError(monkeypatch):
    """Screen 1's "Comprar agora" merely OPENS the checkout — also pre-order."""
    btn = _Btn(_DriverBlewUp("greyout intercepts pointer events"))
    page = _world(monkeypatch, first_visible=lambda p, text, limit=12:
                  None if text == "Continuar" else btn)

    with pytest.raises(CheckoutError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert not isinstance(ei.value, OrderMayExistError), "screen 1 creates no order"
    assert btn.clicks == 1


def test_a_failed_ORDER_CREATING_click_FAILS_CLOSED(monkeypatch):
    """⛔🔴 The point of no return. `click()` dispatches the input event and may only
    time out afterwards, so "it raised" does NOT prove the order was not created.

    Graded `OrderMayExistError` so the caller records the fire and disarms the night.
    Fails CLOSED on purpose: clearing one ledger key by hand is cheap, a duplicate
    reservation is not. Same rule as
    `feedback_an_error_after_the_side_effect_is_not_a_failure`.
    """
    steps = {"n": 0}
    final = _Btn(_DriverBlewUp("Timeout 30000ms exceeded"))
    opener = _Btn()

    def _fv(p, text, limit=12):
        if text == "Continuar":
            return None
        steps["n"] += 1
        return opener if steps["n"] == 1 else final   # screen 1 opens; screen 2 orders

    page = _world(monkeypatch, first_visible=_fv)

    with pytest.raises(OrderMayExistError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert opener.clicks == 1 and final.clicks == 1
    msg = str(ei.value)
    assert "Comprados" in msg, "must tell the human where to look for a live hold"
    assert "Timeout 30000ms exceeded" in msg, "the driver's own reason must survive"


def test_a_DRY_RUN_never_reaches_the_order_creating_click(monkeypatch):
    """The guard that makes iterating on this flow free."""
    steps = {"n": 0}
    final = _Btn(_DriverBlewUp("must never be clicked"))
    opener = _Btn()

    def _fv(p, text, limit=12):
        if text == "Continuar":
            return None
        steps["n"] += 1
        return opener if steps["n"] == 1 else final

    page = _world(monkeypatch, first_visible=_fv)
    out = checkout.run_checkout(page, None, dry_run=True, expect_cents=13200)

    assert out["dry_run"] is True
    assert final.clicks == 0, "dry run must stop one click short"


# ── after the point of no return ────────────────────────────────────────────────

def _reaches_the_order(monkeypatch, *, price_cents=13200):
    """A world where the order-creating click SUCCEEDS, so the tail is under test."""
    steps = {"n": 0}
    opener, final = _Btn(), _Btn()

    def _fv(p, text, limit=12):
        if text == "Continuar":
            return None
        steps["n"] += 1
        return opener if steps["n"] == 1 else final

    page = _world(monkeypatch, first_visible=_fv, price_cents=price_cents)
    return page, final


def test_a_RAW_failure_AFTER_the_order_exists_is_an_OrderMayExistError(monkeypatch):
    """⛔🔴 Everything past the order-creating click runs while a REAL reservation is
    already running down its ~10-minute clock. A raw driver exception there used to
    escape run_checkout uncaught exactly like the intercepted click did — so the ledger
    was never written and the night stayed armed, which on a 1-minute cron is how one
    intended ticket becomes two.

    Injected at the tail's `settle(page, timeout_ms=120_000)`, the one call the loop
    does not share.
    """
    page, final = _reaches_the_order(monkeypatch)

    def _blows_up_only_in_the_tail(pg, **k):
        if k.get("timeout_ms") == 120_000:
            raise _DriverBlewUp("Target page, context or browser has been closed")
        return [{"x": 1}]
    monkeypatch.setattr(checkout, "settle", _blows_up_only_in_the_tail)

    with pytest.raises(OrderMayExistError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert final.clicks == 1, "the order WAS created — that is the whole point"
    msg = str(ei.value)
    assert "Comprados" in msg
    assert "Target page, context or browser has been closed" in msg


def test_a_RAW_failure_in_the_PIX_READER_is_an_OrderMayExistError(monkeypatch):
    """Same hazard, the other realistic raiser: the orders-page navigation."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix",
                        lambda pg: (_ for _ in ()).throw(_DriverBlewUp("net::ERR_ABORTED")))

    with pytest.raises(OrderMayExistError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert final.clicks == 1
    assert "net::ERR_ABORTED" in str(ei.value)


def test_the_deliberate_NO_PIX_error_is_not_re_wrapped(monkeypatch):
    """⛔ The tail's own `OrderMayExistError` already says the right thing — pointing at
    Comprados and the 'Copiar código' button. A blanket wrapper that re-raised it would
    bury that guidance under a generic message.
    """
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix", lambda pg: {})
    monkeypatch.setattr(checkout, "_pix_from_orders_page", lambda pg: {})

    with pytest.raises(OrderMayExistError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert final.clicks == 1
    assert "no Pix code could be read" in str(ei.value), "the specific message survives"
    assert "Copiar código" in str(ei.value)


def test_the_HAPPY_path_still_returns_the_code(monkeypatch):
    """The wrapper must not change what success looks like."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix",
                        lambda pg: {"pix_code": "00020126" + "0" * 40, "source": "clipboard"})

    out = checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert final.clicks == 1
    assert out["dry_run"] is False
    assert out["pix_code"].startswith("00020126")
    assert out["source"] == "clipboard"


def test_the_no_PIX_message_never_points_the_human_AWAY_from_the_orders_page(monkeypatch):
    """⛔🔴 Printed verbatim on the real 2026-09-10 10:55 run, R$242,00:

        ⚠️ Find it at https://buyticketbrasil.com/ingressos -> the 'Comprados' tab...
        ⛔ NOT at https://buyticketbrasil.com/ingressos -- reopening a checkout URL...

    Self-contradictory, and it points AWAY from the only page a live reservation is
    visible on. Cause: the `⛔` line read `page.url` at RAISE time, and
    `_pix_from_orders_page` had already navigated the page to `/ingressos` looking for
    the code. The URL the warning means is the CHECKOUT one, which by then was gone.

    ⭐ Same shape as `feedback_an_instrument_that_mutates_its_own_precondition`: the
    step that gathers the evidence moved the thing the message was measuring.
    """
    page, final = _reaches_the_order(monkeypatch)
    page.url = "https://buyticketbrasil.com/checkout?c_anuncio=abc&p=2"

    def _hunts_the_orders_page(pg, **kw):
        pg.url = checkout.ORDERS_URL            # exactly what the real one does
        return None                             # ...and finds no payable hold
    monkeypatch.setattr(checkout, "_extract_pix", lambda pg: None)
    monkeypatch.setattr(checkout, "_pix_from_orders_page", _hunts_the_orders_page)

    with pytest.raises(OrderMayExistError) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)

    assert final.clicks == 1, "the order-creating click DID happen"
    lines = str(ei.value).splitlines()
    find = next(ln for ln in lines if ln.startswith("⚠️ Find it at"))
    away = next(ln for ln in lines if ln.startswith("⛔ NOT at"))

    assert checkout.ORDERS_URL in find, "must still send the human to Comprados"
    assert checkout.ORDERS_URL not in away, (
        "the ⛔ line must never name the orders page -- that is the one place the "
        f"reservation is visible. Got: {away!r}")
    assert "checkout?c_anuncio=abc" in away, "it must name the CHECKOUT url instead"
