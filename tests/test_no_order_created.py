"""A click that provably created NOTHING must not disarm the night.

⛔🔴 This is what fired the disarm TWICE on 2026-09-10, and the second one ended the
11/09 event night 25 hours before anyone looked:

    10:55  83.30s total — 47.47s of it failing to read a Pix code → OrderMayExistError
    14:24  82.79s total — 47.17s of it failing to read a Pix code → OrderMayExistError
                                                     → ledger UNCONFIRMED + DISARM

Neither run created an order. `receipts/rockinrio2026-09-11/order.png` is the event
DATE-PICKER page ("Rock In Rio 2026 · 3 datas", 11/12/13 Set 2026) — not a checkout,
not an order. The app bounced there because the listing evaporated before the click
landed. Juan verified `/ingressos` → Comprados by hand after the first: nothing pending,
nothing bought.

⭐ The tool already HAD the evidence and threw it away. `_pix_from_orders_page` spent
those 45 seconds successfully reaching the Comprados tab and observing zero pending
rows — it even comments `# nothing pending at all -- no order` — and then returned the
same empty `{}` it returns when it cannot reach the page at all. "I looked and found
nothing" and "I could not look" left through one door, so evidence was graded as
ignorance.

⛔ The fix is deliberately TWO signals, never one. A false negative here is the
catastrophic direction: it would leave a real reservation out of the ledger and let the
next cron minute buy a SECOND ticket. Every single-signal case below still fails CLOSED.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import buy as buy_mod                                                 # noqa: E402
from autobuy import checkout, runner                                  # noqa: E402
from autobuy.errors import NoOrderCreated, OrderMayExistError         # noqa: E402

# `tests/` is a package (it has __init__.py), so the bare module name does not
# resolve -- reuse the existing post-click harness through the package path.
from tests.test_checkout_errors import _reaches_the_order             # noqa: E402

#: The real bounce, copied from the 2026-09-10 screenshot.
BOUNCE_URL = ("https://buyticketbrasil.com/evento/rockinrio2026"
              "?data=1789174800000&evento_local=1765323734393x441784445622288400")
CHECKOUT_URL = "https://buyticketbrasil.com/checkout?anuncio=rock-in-rio-2026mret&p=2"


def _tail(monkeypatch, *, settled_url, comprados_rendered, pending_rows):
    """Drive run_checkout to the post-click tail with a chosen probe outcome."""
    page, final = _reaches_the_order(monkeypatch)
    monkeypatch.setattr(checkout, "_extract_pix", lambda pg: {})

    def _orders(pg, **kw):
        pg.url = checkout.ORDERS_URL          # the real one navigates; so does this
        probe = kw.get("probe")
        if probe is not None:
            probe["comprados_rendered"] = comprados_rendered
            probe["pending_rows"] = pending_rows
        return {}
    monkeypatch.setattr(checkout, "_pix_from_orders_page", _orders)

    page.url = settled_url                    # what `settle` left on screen
    return page, final


# ── the two signals agreeing: no order, night stays armed ─────────────────────

def test_a_bounce_with_an_empty_comprados_is_NOT_an_order(monkeypatch):
    """⛔🔴 THE regression. Pre-patch this raised OrderMayExistError, which disarms."""
    page, final = _tail(monkeypatch, settled_url=BOUNCE_URL,
                        comprados_rendered=True, pending_rows=0)
    with pytest.raises(NoOrderCreated) as ei:
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)
    assert final.clicks == 1, "the click still happened"
    assert "NO order exists" in str(ei.value)
    assert "stays ARMED" in str(ei.value)


def test_it_is_not_an_OrderMayExistError(monkeypatch):
    """⛔ The grading is the whole point: `OrderMayExistError` writes the ledger and
    disarms. `NoOrderCreated` is a sibling, so it must not be caught as one."""
    page, _ = _tail(monkeypatch, settled_url=BOUNCE_URL,
                    comprados_rendered=True, pending_rows=0)
    with pytest.raises(NoOrderCreated):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)
    assert not issubclass(NoOrderCreated, OrderMayExistError)
    assert not issubclass(OrderMayExistError, NoOrderCreated)


# ── every single-signal case still fails CLOSED ───────────────────────────────

def test_a_bounce_ALONE_still_fails_closed(monkeypatch):
    """The page fell out of the flow, but Comprados was never read. Unknown ⇒ closed."""
    page, _ = _tail(monkeypatch, settled_url=BOUNCE_URL,
                    comprados_rendered=False, pending_rows=None)
    with pytest.raises(OrderMayExistError):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)


def test_an_empty_comprados_ALONE_still_fails_closed(monkeypatch):
    """⛔ The dangerous one. Still on /checkout means the click may well have landed;
    an order can lag behind its own publication, and grading THAT as 'no order' is how
    a real reservation goes unrecorded and the next cron minute buys a second ticket."""
    page, _ = _tail(monkeypatch, settled_url=CHECKOUT_URL,
                    comprados_rendered=True, pending_rows=0)
    with pytest.raises(OrderMayExistError):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)


def test_a_bounce_with_PENDING_rows_still_fails_closed(monkeypatch):
    """Something is pending. Whether it is ours is exactly what we cannot tell."""
    page, _ = _tail(monkeypatch, settled_url=BOUNCE_URL,
                    comprados_rendered=True, pending_rows=2)
    with pytest.raises(OrderMayExistError):
        checkout.run_checkout(page, None, dry_run=False, expect_cents=13200)


# ── the bounce predicate ──────────────────────────────────────────────────────

@pytest.mark.parametrize("url,bounced", [
    (CHECKOUT_URL, False),
    ("https://buyticketbrasil.com/checkout", False),
    ("https://buyticketbrasil.com/ingressos", False),
    (BOUNCE_URL, True),
    ("https://buyticketbrasil.com/datas/rockinrio2026", True),
    ("https://buyticketbrasil.com/", True),
    ("", False),          # unreadable is NOT evidence of a bounce
    (None, False),
])
def test_click_bounced(url, bounced):
    assert checkout._click_bounced(url) is bounced


# ── the probe now reports what it SAW ─────────────────────────────────────────

def test_the_probe_records_an_empty_comprados():
    """⭐ The 45 seconds that used to be thrown away."""
    probe: dict = {}
    calls = {"n": 0}

    class _P:
        url = checkout.ORDERS_URL

        def click(self):
            pass
    tab = _P()

    import autobuy.checkout as c
    orig_goto, orig_wait = c._goto, c._wait_for
    try:
        c._goto = lambda pg, url: None

        def _wait(pg, fn, timeout_ms=0):
            calls["n"] += 1
            return tab if calls["n"] == 1 else []      # tab found, zero pending rows
        c._wait_for = _wait
        assert c._pix_from_orders_page(_P(), probe=probe) == {}
    finally:
        c._goto, c._wait_for = orig_goto, orig_wait

    assert probe["comprados_rendered"] is True, "it reached the tab"
    assert probe["pending_rows"] == 0, "and saw nothing pending -- positive evidence"


def test_the_probe_records_a_tab_it_never_reached():
    """⛔ The other cause of an empty return, and it must stay distinguishable."""
    probe: dict = {}
    import autobuy.checkout as c
    orig_goto, orig_wait = c._goto, c._wait_for
    try:
        c._goto = lambda pg, url: None
        c._wait_for = lambda pg, fn, timeout_ms=0: None    # tab never appears
        assert c._pix_from_orders_page(object(), probe=probe) == {}
    finally:
        c._goto, c._wait_for = orig_goto, orig_wait
    assert probe["comprados_rendered"] is False
    assert probe["pending_rows"] is None


# ── and the consequence the night actually cares about ────────────────────────

def test_the_night_stays_ARMED_and_the_ledger_stays_EMPTY(tmp_path, monkeypatch):
    """⛔🔴 The end-to-end fact. On 2026-09-10 14:24 this wrote an UNCONFIRMED ledger
    row and flipped `buy.enabled: false`, and the 11/09 night never fired again."""
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

    def _no_order(*a, **k):
        raise NoOrderCreated("the listing was gone before the click landed -- "
                             "NO order exists. The night stays ARMED.")
    monkeypatch.setattr(buy_mod, "_execute_buy", _no_order)

    args = SimpleNamespace(targets=str(targets), history=str(hist),
                           fields=str(tmp_path / "f.json"), verbose=False)
    assert buy_mod.cmd_autobuy(args) == 0, "an ordinary no-op, not a failure"

    raw = json.loads((targets / "n.json").read_text(encoding="utf-8"))
    assert raw["buy"]["enabled"] is True, "the night must STAY ARMED"
    assert not runner._read_state().get("fired"), "and the ledger must stay EMPTY"


# ── "could not look" must never be reported as "nothing is there" ─────────────

class _FakePW:
    """The thinnest playwright that reaches cmd_orders' decision."""
    def __init__(self, page): self._page = page
    def __enter__(self): return self
    def __exit__(self, *a): return False

    @property
    def chromium(self): return self

    def launch(self, **kw): return self
    def new_context(self, **kw): return self
    def new_page(self): return self._page
    def close(self): pass


def _orders_world(monkeypatch, tmp_path, *, tab_found, rows):
    import playwright.sync_api as pw

    class _Page:
        url = checkout.ORDERS_URL
        def wait_for_timeout(self, ms): pass
        def screenshot(self, **kw): Path(kw["path"]).write_bytes(b"x")

    page = _Page()
    monkeypatch.setattr(pw, "sync_playwright", lambda: _FakePW(page))
    monkeypatch.setattr(buy_mod.session, "require", lambda *a, **k: tmp_path / "s.json")
    monkeypatch.setattr(checkout, "_goto", lambda pg, url: None)

    class _Tab:
        def click(self): pass
    monkeypatch.setattr(checkout, "_wait_for",
                        lambda pg, fn, timeout_ms=0: _Tab() if tab_found else None)
    monkeypatch.setattr(checkout, "_pending_rows", lambda pg: rows)
    monkeypatch.setattr(checkout, "pending_order_ids", lambda pg: set())
    return SimpleNamespace(headed=False, out=str(tmp_path / "shot.png"))


def test_orders_reports_UNKNOWN_when_it_cannot_reach_the_tab(
        monkeypatch, tmp_path, capsys):
    """⛔🔴 The dangerous branch. If an unreachable Comprados tab printed 'nothing
    pending', a human would clear a ledger row guarding a REAL hold. Exit 3, and the
    words must refuse the inference explicitly."""
    args = _orders_world(monkeypatch, tmp_path, tab_found=False, rows=[])
    assert buy_mod.cmd_orders(args) == 3, "unknown is a failure, not a clean bill"
    out = capsys.readouterr().out
    assert "NOT evidence" in out
    assert "NOTHING PENDING" not in out, "it must not claim the account is clear"


def test_orders_reports_a_clean_account_only_when_it_actually_looked(
        monkeypatch, tmp_path, capsys):
    args = _orders_world(monkeypatch, tmp_path, tab_found=True, rows=[])
    assert buy_mod.cmd_orders(args) == 0
    assert "NOTHING PENDING" in capsys.readouterr().out


def test_orders_flags_a_pending_hold(monkeypatch, tmp_path, capsys):
    """⛔ A pending row means a human must look before re-arming -- never auto-cleared."""
    args = _orders_world(monkeypatch, tmp_path, tab_found=True, rows=[object()])
    assert buy_mod.cmd_orders(args) == 0
    out = capsys.readouterr().out
    assert "pending hold exists" in out
    assert "NOTHING PENDING" not in out
