"""The exposure window — how long the walk takes, and what happens when it is too long.

⛔🔴 Two defects, one cause: the tool spent the race window on FIXED SLEEPS and then
measured the wrong span to decide whether it had been too slow.

    0.51s  resolve market
    4.67s  browser + listing
   23.70s  five screens          ← ~3.5 s/screen of it was flat wait_for_timeout()
    1.13s  ORDER CREATED         ← ~30 s of exposure, and the listing was already gone
   47.17s  PIX CODE EXTRACTED    ← post-click; abort here is FORBIDDEN
   82.79s  TOTAL   budget 60s -> ❌ OVER by 22.79s

That printed verdict is the trap. It is loud, it is red, and it is about a span nobody
could act on: the run stood at ~30 s when it clicked, comfortably inside 60 s, and the
entire overrun happened after the point of no return. `BUDGET_S` was never read by
anything but the print statement — a threshold with no instrument behind it.

⭐ So the budget now measures resolve→click and is ENFORCED there; and the walk itself
waits on observable conditions instead of guessing with sleeps.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import buy as buy_mod                                                 # noqa: E402
from autobuy import checkout, runner, timing                          # noqa: E402
from autobuy.errors import BudgetExceeded, OrderMayExistError         # noqa: E402

from tests.test_checkout_errors import _reaches_the_order             # noqa: E402


def _el(text, tag="button", cls="x"):
    return {"tag": tag, "text": text, "aria": None, "placeholder": None, "cls": cls}


# ── the signature the waits are built on ──────────────────────────────────────

def test_signature_ignores_class_soup():
    """⛔ Two renders of ONE screen differ in Bubble's class names. A signature that
    flickered on that would report a transition that never happened."""
    a = [_el("Continuar", cls="btn-module__aaa__")]
    b = [_el("Continuar", cls="btn-module__zzz__")]
    assert checkout._screen_signature(a) == checkout._screen_signature(b)


def test_signature_separates_two_screens():
    assert (checkout._screen_signature([_el("Continuar")])
            != checkout._screen_signature([_el("Comprar agora")]))


def test_signature_ignores_spinners():
    """⭐ Only ACTIONABLE controls count -- the checkout's first paint carries five
    text-less spinners, and a signature including them would change as they vanish."""
    spinner = {"tag": "a", "text": "", "aria": None, "placeholder": None, "cls": "s"}
    assert (checkout._screen_signature([_el("Continuar"), spinner])
            == checkout._screen_signature([_el("Continuar")]))


# ── quiesce: stops when the tree stops moving, never later than the old ceiling ─

class _Page:
    """A page whose control set follows a script, one entry per probe."""
    def __init__(self, script):
        self.script = list(script)
        self.waits = 0
        self.url = "https://buyticketbrasil.com/checkout?p=2"

    def wait_for_timeout(self, ms):
        self.waits += 1

    def evaluate(self, js):
        return self.script.pop(0) if self.script else (self.script or [_el("Continuar")])


def test_quiesce_returns_once_the_tree_is_stable(monkeypatch):
    """Three identical polls and it stops — it does not sit out the full 1500 ms."""
    stable = [_el("Continuar")]
    page = _Page([stable] * 12)
    out = checkout._quiesce(page, stable, budget_ms=1500, poll_ms=200, stable_polls=3)
    assert checkout._screen_signature(out) == checkout._screen_signature(stable)
    assert page.waits == 3, f"stopped after 3 stable polls, not {page.waits}"


def test_quiesce_keeps_waiting_while_the_tree_is_still_growing():
    """⛔ The behaviour the flat sleep was protecting: a page still painting must not be
    read early. Quiescence decides WHEN to stop, and a moving tree never qualifies."""
    growing = [[_el("A")], [_el("A"), _el("B")], [_el("A"), _el("B"), _el("C")]]
    page = _Page(growing + [growing[-1]] * 9)
    out = checkout._quiesce(page, growing[0], budget_ms=1500, poll_ms=200)
    assert len(out) == 3, "it must end on the fully-painted view"
    assert page.waits > 3, "and it must not have stopped during the growth"


def test_quiesce_keeps_the_RICHEST_view():
    """⛔ Preserved verbatim from the code it replaces: a Bubble re-render mid-settle
    must never turn a populated step into an empty one."""
    rich = [_el("A"), _el("B")]
    page = _Page([rich, [], [], [], [], []])
    out = checkout._quiesce(page, rich, budget_ms=1500, poll_ms=200)
    assert len(out) == 2, "an empty re-render must not win"


def test_quiesce_survives_a_probe_that_raises():
    """Mid-navigation `evaluate` throws. That is a wait condition, not a result."""
    class _Boom(_Page):
        def evaluate(self, js):
            raise RuntimeError("Execution context was destroyed")
    page = _Boom([])
    out = checkout._quiesce(page, [_el("A")], budget_ms=600, poll_ms=200)
    assert len(out) == 1, "it falls back to what it was given"


def test_quiesce_is_bounded_by_ITERATIONS_not_only_the_clock():
    """⛔ Pacing comes from `page.wait_for_timeout`, a NO-OP on a double. A clock-only
    bound busy-spins the whole budget against any page that does not sleep — and a loop
    that terminates only via a collaborator's side effect means two different things in
    two environments."""
    flip = [[_el("A")], [_el("B")]] * 40
    page = _Page(flip)
    t0 = time.monotonic()
    checkout._quiesce(page, flip[0], budget_ms=1500, poll_ms=200)
    assert time.monotonic() - t0 < 0.5, "a non-sleeping page must not burn wall-clock"
    assert page.waits <= 1500 // 200


# ── await_transition ──────────────────────────────────────────────────────────

def test_await_transition_returns_as_soon_as_the_screen_swaps():
    before = checkout._screen_signature([_el("Continuar")])
    page = _Page([[_el("Comprar agora")]] * 8)
    assert checkout._await_transition(page, before, budget_ms=2000) is True
    assert page.waits == 1, "the first poll already saw the new screen"


def test_await_transition_gives_up_without_grading_it_a_failure():
    """⚠️ A stuck screen returns False. The caller proceeds exactly as the fixed sleep
    did — this narrows a window, it does not adjudicate the flow."""
    same = [_el("Continuar")]
    page = _Page([same] * 40)
    assert checkout._await_transition(page, checkout._screen_signature(same),
                                      budget_ms=2000) is False


# ── the budget: enforced on the WALK, and only before the click ───────────────

class _Clock:
    def __init__(self, elapsed): self._e = elapsed
    @property
    def elapsed(self): return self._e
    def mark(self, name): return 0.0


def test_a_slow_walk_REFUSES_to_click(monkeypatch):
    """⛔🔴 The enforcement that did not exist, at the LAST instant it is permissible.

    The walk starts inside the budget and blows it partway through — the realistic
    shape, and the one the per-step check cannot catch because the step that goes slow
    is the step already in flight. The gate right before the click is what remains.
    """
    page, final = _reaches_the_order(monkeypatch)
    clock = _Clock(12.0)

    real_price = checkout._page_price_cents
    def _slow_by_now(pg):
        clock._e = 62.0          # the walk overran while the last screen rendered
        return real_price(pg)
    monkeypatch.setattr(checkout, "_page_price_cents", _slow_by_now)

    with pytest.raises(BudgetExceeded) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                              clock=clock, deadline_s=45.0)
    assert final.clicks == 0, "⛔ the order-creating click must NOT have happened"
    assert "REFUSING to click" in str(ei.value)
    assert "point of no return" in str(ei.value)


def test_an_already_slow_run_refuses_at_the_FIRST_step(monkeypatch):
    """The other arm: over budget before the walk even starts."""
    page, final = _reaches_the_order(monkeypatch)
    with pytest.raises(BudgetExceeded) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                              clock=_Clock(90.0), deadline_s=45.0)
    assert final.clicks == 0
    assert "step 1" in str(ei.value)


def test_a_fast_walk_still_clicks(monkeypatch):
    """⛔ The permit path. A budget only proven to REFUSE is half-tested — and a refusal
    gate that never permits is indistinguishable from a broken flow."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix",
                        lambda pg: {"pix_code": "0002" + "0" * 45, "source": "clip"})
    out = checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                                clock=_Clock(12.0), deadline_s=45.0)
    assert final.clicks == 1
    assert out["pix_code"].startswith("0002")


def test_no_deadline_means_no_enforcement(monkeypatch):
    """⚠️ A manual `buy` is attended: a human chose the moment and can judge for
    themselves. Only the unattended cron path passes a deadline."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix",
                        lambda pg: {"pix_code": "0002" + "0" * 45, "source": "clip"})
    checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                          clock=_Clock(999.0), deadline_s=None)
    assert final.clicks == 1, "no deadline given, so nothing may refuse the click"


def test_the_budget_is_NOT_enforced_after_the_click(monkeypatch):
    """⛔🔴 THE rule that makes this safe. Past the click a reservation may be live and
    the Pix code MUST be captured however long it takes. A budget that aborted there
    would abandon a real unpaid hold — worse than any overrun.

    The 47 s post-click read that blew the old 60 s total must still be allowed to run
    to completion, and the failure that follows must still be graded fail-closed."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix", lambda pg: {})
    monkeypatch.setattr(checkout, "_pix_from_orders_page", lambda pg, **kw: {})

    clock = _Clock(10.0)                      # inside the budget at the click

    class _Slow(_Clock):
        @property
        def elapsed(self):
            return 10.0 if final.clicks == 0 else 999.0   # blows it AFTER clicking
    with pytest.raises(OrderMayExistError):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                              clock=_Slow(10.0), deadline_s=45.0)
    assert final.clicks == 1, "it clicked, and the overrun after did not abort it"


def test_a_slow_walk_aborts_EARLY_not_at_the_last_screen(monkeypatch):
    """⭐ Checked before `settle`, which can wait 90 s. Starting one while already over
    budget spends the very window the guard exists to protect."""
    calls = {"settle": 0}
    real = checkout.settle

    def _counting(page, **kw):
        calls["settle"] += 1
        return real(page, **kw)
    page, _ = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "settle", _counting)
    with pytest.raises(BudgetExceeded):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200,
                              clock=_Clock(90.0), deadline_s=45.0)
    assert calls["settle"] == 0, "it must refuse before the first 90 s settle"


# ── the report must name the ENFORCED span ───────────────────────────────────

def test_the_report_shows_the_walk_separately():
    """⛔🔴 On 2026-09-10 the verdict line screamed '❌ OVER by 22.79s' about the total
    while the walk — the only span that could still be aborted — was fine. Printing one
    number kept the reader optimising against the wrong one."""
    c = timing.Clock("t")
    c.marks = [("resolve market", 1.0), ("screens", 29.0),
               ("ORDER CREATED (final click)", 1.0), ("PIX CODE EXTRACTED", 47.0)]
    out = c.report(budget_s=60.0, walk_budget_s=45.0)
    assert "report only" in out, "the total must be labelled as non-binding"
    assert "ENFORCED" in out
    assert "31.00s" in out, "the walk is resolve->click, not the total"
    assert "✅ inside" in out, "and it was inside, which is the point"


def test_the_report_says_nothing_when_the_click_never_happened():
    """⛔ A run that never reached the click has no walk. A fabricated number here would
    be indistinguishable from a measured one."""
    c = timing.Clock("t")
    c.marks = [("resolve market", 1.0), ("screens", 29.0)]
    assert c.walk_elapsed("ORDER CREATED (final click)") is None
    assert "ENFORCED" not in c.report(budget_s=60.0, walk_budget_s=45.0)


# ── and the night-level consequence ──────────────────────────────────────────

def test_a_refused_click_leaves_the_night_ARMED(tmp_path, monkeypatch):
    targets = tmp_path / "targets"; targets.mkdir()
    hist = tmp_path / "history"; hist.mkdir()
    state = tmp_path / "state"; state.mkdir()
    (targets / "n.json").write_text(json.dumps({
        "id": "n", "label": "Night N",
        "params": {"event_slug": "e", "data_millis": 1, "evento_local": "l"},
        "buy": {"enabled": True, "max_price_brl": 250.0, "quantity": 1,
                "sector": ["Gramado"], "entry_class": None}}), encoding="utf-8")
    (hist / "n.jsonl").write_text(json.dumps({
        "target_id": "n", "item": "Gramado || Inteira", "price_cents": 22000,
        "quantity": 178, "available": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "extra": {"sector": "Gramado", "entry_class": "Inteira"}}) + "\n",
        encoding="utf-8")
    (hist / "cron.log").write_text("alive\n", encoding="utf-8")
    monkeypatch.setattr(runner, "STATE_DIR", state)
    monkeypatch.setattr(runner, "STATE_PATH", state / "autobuy.json")
    monkeypatch.setattr(runner, "LOCK_PATH", state / "autobuy.lock")
    monkeypatch.setattr(runner, "now_local",
                        lambda: datetime(2026, 9, 11, 15, 0, tzinfo=runner.TZ))
    monkeypatch.setattr(buy_mod.session, "require", lambda *a, **k: tmp_path / "s.json")
    monkeypatch.setattr(buy_mod.notify, "send_text", lambda *a, **k: None)

    def _too_slow(*a, **k):
        raise BudgetExceeded("62.0s from resolve to the point of no return -- "
                             "REFUSING to click. Nothing was ordered.")
    monkeypatch.setattr(buy_mod, "_execute_buy", _too_slow)

    args = SimpleNamespace(targets=str(targets), history=str(hist),
                           fields=str(tmp_path / "f.json"), verbose=False)
    assert buy_mod.cmd_autobuy(args) == 0
    raw = json.loads((targets / "n.json").read_text(encoding="utf-8"))
    assert raw["buy"]["enabled"] is True, "refusing to click must not disarm"
    assert not runner._read_state().get("fired"), "and must not write a ledger row"
