"""Tests for the resolver, against a REAL captured RSC payload.

The fixture is `curl -H 'RSC: 1'` output from 2026-09-02, byte-for-byte -- never a
hand-written approximation of what the site "probably" returns. price-watcher makes the
same call, and it is what caught the zero-priced placeholder rows: nobody inventing a
fixture would have thought to put a R$ 0,00 "Meia aposentado" in it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autobuy import config, listing                                   # noqa: E402
from autobuy.errors import ConfigError, NoMatch, ResolveError         # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "rockinrio2026-09-04.rsc.txt"
BODY = FIXTURE.read_text(encoding="utf-8")


# ------------------------------------------------------------------ parse

def test_parses_every_purchasable_row():
    cands = listing.parse_candidates(BODY)
    # All 19 matriz_preco rows in this capture are purchasable.
    assert len(cands) == 19
    assert all(c.price_cents > 0 and c.quantity > 0 for c in cands)


def test_this_capture_has_no_placeholder_rows():
    """Pins a fact about the FIXTURE, so the next test cannot quietly become vacuous.

    ⚠️ price-watcher's notes record zero-priced placeholder rows ("Meia aposentado" at
    0/0) inside matriz_preco on 2026-09-01. This capture, one day later, has none --
    the R$ 0,00 entries visible in the page's category dropdown come from `entradas`
    /`tipos_ingresso`, which are a different structure. The guard below is therefore
    still required and this fixture CANNOT exercise it; saying so here is what stops
    someone reading a green suite as evidence that it was tested against real data.
    """
    matriz = listing._grab(BODY, "matriz_preco")
    assert len(matriz) == 19
    assert not [k for k, v in matriz.items() if (v.get("preco_min") or 0) <= 0]


def test_drops_zero_priced_placeholder_rows():
    """A R$ 0,00 row wins every min(). Left in, it targets a listing nobody can buy.

    Synthetic input, not a capture -- see the test above for why the real fixture
    cannot cover this branch today.
    """
    body = ('{"matriz_preco":{'
            '"A||Inteira":{"preco_min":0,"disponivel":0,"id_ref":"x1"},'
            '"A||Meia aposentado":{"preco_min":0,"disponivel":3,"id_ref":"x2"},'
            '"A||Boa":{"preco_min":30800,"disponivel":5,"id_ref":"x3"}}}')
    cands = listing.parse_candidates(body)
    assert [c.entry_class for c in cands] == ["Boa"]


def test_drops_rows_with_no_id_ref():
    """No id_ref means no direct URL; keeping one would 404 at the moment it is used."""
    body = ('{"matriz_preco":{'
            '"A||NoRef":{"preco_min":30800,"disponivel":5},'
            '"A||Ok":{"preco_min":30900,"disponivel":5,"id_ref":"x9"}}}')
    assert [c.entry_class for c in listing.parse_candidates(body)] == ["Ok"]


def test_id_ref_is_the_c_anuncio_from_the_buy_button():
    """⭐ The finding the whole tool rests on, pinned as a regression test.

    Observed live 2026-09-02: selecting Gramado + Inteira gave the Comprar button
    href `/r?event=rockinrio2026&c_anuncio=1788351098784x401080915725898750`.
    """
    c = next(c for c in listing.parse_candidates(BODY)
             if c.sector == "Gramado" and c.entry_class == "Inteira")
    assert c.id_ref == "1788351098784x401080915725898750"
    assert c.url("rockinrio2026") == (
        "https://buyticketbrasil.com/r"
        "?event=rockinrio2026&c_anuncio=1788351098784x401080915725898750")


def test_missing_matriz_raises_rather_than_returning_empty():
    """Blind must never look like sold out -- the invariant price-watcher is built on."""
    with pytest.raises(ResolveError, match="matriz_preco"):
        listing.parse_candidates('{"something_else": 1}')


def test_empty_matriz_is_data_not_blindness():
    """A real sold-out event returns []. It must NOT raise."""
    assert listing.parse_candidates('{"matriz_preco":{}}') == []


# ------------------------------------------------------------------ choose

def _cands():
    return listing.parse_candidates(BODY)


def test_chooses_the_cheapest_matching_listing():
    best = listing.choose(_cands(), max_price_cents=100_000, sectors=["Gramado"])
    assert best.item == "Gramado || Meia PCD"      # R$ 308,00, the cheapest Gramado
    assert best.price_cents == 30800


def test_ceiling_is_inclusive_and_excludes_one_centavo_over():
    """Off-by-one at the threshold is the bug that quietly never fires."""
    cheapest = min(c.price_cents for c in _cands())
    assert listing.choose(_cands(), max_price_cents=cheapest).price_cents == cheapest
    with pytest.raises(NoMatch):
        listing.choose(_cands(), max_price_cents=cheapest - 1)


def test_quantity_filters_out_thin_listings():
    """qty=1 rows must not be offered when 2 are wanted."""
    best = listing.choose(_cands(), max_price_cents=100_000, sectors=["Gramado"], quantity=2)
    assert best.quantity >= 2
    assert best.item != "Gramado || Meia PCD"      # that one has qty=1


def test_no_match_raises_rather_than_returning_none():
    with pytest.raises(NoMatch):
        listing.choose(_cands(), max_price_cents=1_00)


def test_sector_filter_is_case_insensitive():
    assert listing.choose(_cands(), max_price_cents=100_000, sectors=["gRaMaDo"]).sector == "Gramado"


def test_anomaly_floor_excludes_below_it_when_set():
    cheapest = min(c.price_cents for c in _cands())
    with pytest.raises(NoMatch):
        listing.choose(_cands(), max_price_cents=100_000, min_price_cents=10_000_00)
    assert listing.choose(_cands(), max_price_cents=100_000,
                          min_price_cents=cheapest).price_cents == cheapest


# ------------------------------------------------------------------ config

def _target(tmp_path, buy, params=None):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "id": "t", "label": "T",
        "params": params if params is not None else {
            "event_slug": "rockinrio2026", "data_millis": 1788570000000,
            "evento_local": "1765323377313x720803947984191500"},
        "buy": buy,
    }), encoding="utf-8")
    return p


def test_brl_to_cents_is_exact_where_float_is_not():
    """int(220.30 * 100) is 22029 on this machine. One centavo low never fires."""
    assert config.brl_to_cents(220.30, "x") == 22030
    assert config.brl_to_cents("220.00", "x") == 22000
    assert config.brl_to_cents(220, "x") == 22000


def test_enabled_without_a_ceiling_refuses_to_load(tmp_path):
    """Same shape as price-watcher's 'a rule enabled without its parameter must refuse'.
    An armed buy with no ceiling would take any price at all."""
    with pytest.raises(ConfigError, match="max_price_brl"):
        config.load(_target(tmp_path, {"enabled": True}))


def test_disabled_buy_refuses_to_run(tmp_path):
    with pytest.raises(ConfigError, match="enabled"):
        config.load(_target(tmp_path, {"enabled": False, "max_price_brl": 220}))


def test_missing_buy_block_refuses(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"id": "t", "params": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="no 'buy' block"):
        config.load(p)


def test_min_above_max_refuses(tmp_path):
    with pytest.raises(ConfigError, match="exceeds"):
        config.load(_target(tmp_path, {"enabled": True, "max_price_brl": 200,
                                       "min_price_brl": 300}))


def test_bare_string_sector_is_accepted_not_treated_as_characters(tmp_path):
    cfg = config.load(_target(tmp_path, {"enabled": True, "max_price_brl": 220,
                                         "sector": "Gramado"}))
    assert cfg.sectors == ["Gramado"]


def test_invalid_json_is_blindness_not_absence(tmp_path):
    p = tmp_path / "t.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        config.load(p)


def test_quantity_must_be_a_positive_int(tmp_path):
    for bad in (0, -1, "2", True):
        with pytest.raises(ConfigError, match="quantity"):
            config.load(_target(tmp_path, {"enabled": True, "max_price_brl": 220,
                                           "quantity": bad}))


# ------------------------------------------------------------------ checkout helpers

from autobuy import checkout                                          # noqa: E402


class _FakePage:
    """Minimal stand-in: the price reader only ever calls `inner_text('body')`."""
    def __init__(self, text): self._t = text
    def inner_text(self, _sel): return self._t


# The real summary, copied from the live checkout on 2026-09-02.
_SUMMARY = ("Resumo da compra\nIngresso\nR$ 288,00\n"
            "Taxa de serviço (10%)\nR$ 28,80\nValor total\nR$ 316,80\n"
            "Método de pagamento\nPix")


def test_price_reader_takes_valor_total_not_the_first_price():
    """⛔ The bug this pins: reading the first R$ returns the PRE-FEE 288,00 and reports
    a 28,80 price move that never happened -- aborting real purchases while looking
    like a working safeguard."""
    assert checkout._page_price_cents(_FakePage(_SUMMARY)) == 31680


def test_price_reader_returns_none_when_the_label_is_absent():
    """None means 'could not verify', and the caller must fail closed on it."""
    assert checkout._page_price_cents(_FakePage("Ingresso R$ 288,00")) is None


def test_price_reader_handles_thousands_separator():
    assert checkout._page_price_cents(
        _FakePage("Valor total\nR$ 3.300,00")) == 330000


def test_actionable_rejects_the_loading_spinners():
    """The checkout's first paint is five <a class='iconify svg-loading'> with no text.
    A readiness gate that accepts them dumps a step containing only spinners."""
    spinner = {"tag": "a", "text": "", "aria": None, "placeholder": None}
    assert not checkout._actionable(spinner)


def test_actionable_accepts_a_real_button_and_a_bare_field():
    assert checkout._actionable(
        {"tag": "button", "text": "Comprar agora por R$316,80",
         "aria": None, "placeholder": None})
    # A field with no text at all is still actionable -- it is typed into.
    assert checkout._actionable(
        {"tag": "input", "text": "", "aria": None, "placeholder": None})


def test_field_hints_cover_every_person_field():
    """Every attribute the buyer must supply has at least one placeholder hint, so a
    new Person field cannot be added and silently never filled."""
    mapped = {attr for _, attr in checkout.FIELD_HINTS}
    required = set(checkout.Person.__dataclass_fields__)
    assert required - mapped == set(), f"unmapped Person fields: {required - mapped}"


# ------------------------------------------------------------------ pix extraction

#: A structurally real BTG/Pix EMV payload: the tag layout, the `br.gov.bcb.pix` GUI,
#: currency 986, country BR and the CRC are exactly what the live order screen returned
#: on 2026-09-02. ⛔ The account UUID is substituted -- the captured original is a live
#: payment instruction against a real order, and a fixture is not the place for one.
_REAL_PIX = ("00020101021226910014br.gov.bcb.pix2569api.developer.btgpactual.com"
             "/pc/p/v2/00000000000000000000000000000000"
             "5204000053039865802BR5925BUYTICKET DESENVOLVIMENTO6008Brasilia"
             "62070503***63045FC7")


class _FakeLocator:
    def __init__(self, values=(), *, present=True):
        self._v, self._present = list(values), present
    def count(self): return len(self._v) if self._v else (1 if self._present else 0)
    def nth(self, i): return self
    def input_value(self): return self._v[0] if self._v else ""
    def click(self, *a, **k):
        raise AssertionError("a real .click() would hit the greyout overlay and time out")


class _FakeOrderPage:
    """The REAL order screen of 2026-09-02: the Pix code is in NO DOM shape at all --
    it exists only on the clipboard, written by the `#btn_copy` handler."""
    def __init__(self, *, clipboard="", has_button=True, dom_values=()):
        self.clipboard, self.has_button, self.dom_values = clipboard, has_button, dom_values
        self.js_clicked = False
    def inner_text(self, _sel):
        return "Minha compra Aguardando pagamento do PIX Código Pix Copiar código 04:15"
    def locator(self, sel):
        if sel in ("#btn_copy", "button:has-text('Copiar')"):
            return _FakeLocator(present=self.has_button)
        return _FakeLocator(self.dom_values, present=False)
    def wait_for_timeout(self, _ms): pass
    def evaluate(self, js, arg=None):
        if "readText" in js:
            return self.clipboard
        if "click" in js:
            self.js_clicked = True
            return None
        return None


def test_pix_comes_off_the_clipboard_when_no_dom_shape_carries_it():
    """⭐ THE regression for 2026-09-02. `_extract_pix` was written blind against three
    shapes -- readonly input, textarea, page text -- and the live order screen has the
    code in none of them. A real reservation (#7707X57Q) was created with its code
    uncaptured. This pins the path that actually works."""
    page = _FakeOrderPage(clipboard=_REAL_PIX)
    got = checkout._extract_pix(page)
    assert got["pix_code"] == _REAL_PIX
    assert "clipboard" in got["source"]


def test_the_copy_button_is_clicked_via_js_not_playwright_click():
    """⛔ A `<div class='greyout'>` intercepts pointer events, so Playwright's `.click()`
    times out after 30 s on a button it just called visible, enabled and stable. The
    fake raises if `.click()` is ever used, so a regression to it fails loudly."""
    page = _FakeOrderPage(clipboard=_REAL_PIX)
    checkout._extract_pix(page)
    assert page.js_clicked, "must dispatch the handler via evaluate(), not .click()"


def test_a_dom_shape_still_wins_when_present():
    """The three blind shapes are kept, not deleted: they cost nothing and a redesign
    may expose one. If the DOM ever carries the code, it is used without a click."""
    page = _FakeOrderPage(clipboard="", dom_values=[_REAL_PIX])
    page.locator = lambda sel: (_FakeLocator(present=False)
                                if sel in ("#btn_copy", "button:has-text('Copiar')")
                                else _FakeLocator([_REAL_PIX]))
    got = checkout._extract_pix(page)
    assert got["pix_code"] == _REAL_PIX and "clipboard" not in got["source"]


def test_no_code_anywhere_returns_empty_so_the_caller_raises():
    """⛔ Must stay empty-not-exception here: `run_checkout` turns this into the loud
    'AN ORDER MAY EXIST' error, which is the only thing standing between a live
    reservation and nobody knowing it exists."""
    assert checkout._extract_pix(_FakeOrderPage(clipboard="", has_button=False)) == {}


def test_a_bubble_record_id_is_not_mistaken_for_a_pix_code():
    """The order page is full of long ids like 1788398349011x824669513165275000. Only a
    `0002`-prefixed EMV payload counts."""
    assert not checkout._looks_like_pix("1788398349011x824669513165275000")
    assert not checkout._looks_like_pix("0002")            # right prefix, far too short
    assert checkout._looks_like_pix(_REAL_PIX)


def test_recovery_message_points_at_the_orders_page_not_the_checkout_url():
    """⛔ Reopening the checkout URL starts a FRESH checkout showing no order -- it read
    as 'nothing was reserved' while #7707X57Q was live. The error must name the place
    the order can actually be found."""
    src = (ROOT / "autobuy" / "checkout.py").read_text(encoding="utf-8")
    assert "ORDERS_URL" in src and "/ingressos" in checkout.ORDERS_URL
    assert "Comprados" in src and "~10 MINUTES" in src


class _FakeCheckoutThenOrders:
    """Models the REAL post-click behaviour: the checkout page keeps its URL, shows an
    "Aguarde…" spinner and NEVER renders a code; the code is on the order page."""
    def __init__(self):
        self.url = "https://buyticketbrasil.com/checkout?anuncio=x&p=2"
        self.clicked, self.on_orders = [], False
    # -- page API used by the fallback --
    def goto(self, url, **k): self.url = url; self.on_orders = True
    def wait_for_timeout(self, _ms): pass
    def inner_text(self, _sel):
        return ("Aguarde..." if not self.on_orders
                else "Comprados Aguardando pagamento Código Pix Copiar código")
    def locator(self, sel):
        if sel == "#btn_copy":
            return _FakeLocator(present=self.on_orders)
        return _FakeLocator(present=False)
    def evaluate(self, js, arg=None):
        if "readText" in js: return _REAL_PIX if self.on_orders else ""
        return None
    def get_by_text(self, text, **k):
        page = self
        class _M:
            def __init__(self, t): self.t = t
            def is_visible(self): return page.on_orders or self.t == "Comprados"
            def click(self): page.clicked.append(self.t); page.on_orders = True
        class _L:
            def all(self): return [_M(text)]
        return _L()


def test_the_code_is_fetched_from_the_orders_page_not_the_checkout(monkeypatch):
    """⭐ THE correction from two real orders (#7707X57Q, #1280BPGN). Clicking the final
    'Comprar agora' does NOT turn the checkout into a Pix screen -- it keeps its URL and
    sits on 'Aguarde…'. Waiting longer never helps: the code was never coming to that
    page. It is on /ingressos -> Comprados -> the pending order."""
    monkeypatch.setattr(checkout, "_goto", lambda pg, url, **k: pg.goto(url))
    monkeypatch.setattr(checkout, "_first_visible",
                        lambda pg, text, limit=12: next(
                            (m for m in pg.get_by_text(text).all() if m.is_visible()), None))
    page = _FakeCheckoutThenOrders()
    assert checkout._extract_pix(page) == {}, "the checkout page carries no code"
    got = checkout._pix_from_orders_page(page)
    assert got["pix_code"] == _REAL_PIX
    assert "Comprados" in page.clicked
    assert got["order_url"].endswith("/ingressos")


def test_wait_for_returns_none_rather_than_hanging():
    """A predicate that never fires must time out and return None, so the caller reports
    'no code' loudly instead of blocking past the 10-minute hold."""
    calls = {"n": 0}
    class _P:
        def wait_for_timeout(self, _ms): calls["n"] += 1
    assert checkout._wait_for(_P(), lambda pg: False, timeout_ms=300, poll_ms=50) is None
    assert calls["n"] > 0
