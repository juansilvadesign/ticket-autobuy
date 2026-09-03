"""Drive the BuyTicket checkout in a real browser.

## What is mapped, and what is not

Recon 2026-09-02 (RECON.md) walked the flow as far as an unauthenticated browser can go
and stopped at the login wall. Everything up to and including "navigate to the listing"
is VERIFIED against the live site. Everything after login is NOT: it is behind an
account, and guessing selectors for a flow nobody has looked at is how you write a
buyer that clicks a confidently-named button that does not exist.

    ✅ resolve listing id -> direct URL          verified
    ✅ /r?... redirects to /entrar when logged out   verified
    ✅ login form: email + password + "Lembrar de mim"   verified
    🔲 Comprar agora -> Continuar -> Pix -> ...   NOT MAPPED

`map_checkout()` is the tool that closes that gap: with a session it walks the flow and
dumps each step's interactive elements to disk, so the remaining selectors get WRITTEN
from an observation instead of assumed. `run_checkout()` refuses to guess until then.

## Why the selectors that ARE here avoid class names

The site ships CSS-module class names with a build hash in them --
`TicketCard-module__CLLKWG__dropdownHead`. `CLLKWG` changes on any redeploy. A selector
pinned to it works in testing and silently stops matching the first time the site ships,
which on this tool means a buy night where nothing happens and nothing errors. Roles,
`aria-label`s and visible text are used instead; where a class is unavoidable it is
matched by substring (`[class*="buyBtn"]`).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CheckoutError, SessionError
from . import session as session_mod

# ⚠️ buyticketbrasil takes ~12s to reach domcontentloaded from this machine, measured
# 2026-09-02, and headed with a cold profile it is slower still. 30s looked generous and
# was not: it left roughly one slow render of margin. 90s costs nothing on a fast night
# and is the difference between a retry and a lost login on a slow one.
NAV_TIMEOUT_MS = 90_000
STEP_TIMEOUT_MS = 15_000

#: The one definition of "an interactive control", shared by `settle` (which decides the
#: page has rendered) and `_PROBE_JS` (which dumps what is there).
#: ⛔ They must not drift apart. When `settle` counted ALL matching elements while the
#: probe kept only VISIBLE ones, `settle` returned "rendered" on hidden nodes and the
#: dump then wrote 0 controls -- a gate that passes while the thing it gates is empty.
_CONTROL_SELECTOR = ('button, a, input, select, textarea, [role="button"], '
                     '[role="option"], [role="radio"], [role="tab"]')
_VISIBLE_JS = ("el => !!(el.offsetWidth || el.offsetHeight || "
               "el.getClientRects().length)")

#: ⛔ Never wait for "load". It waits on every third-party resource, and the recon found
#: seven analytics/ad origins on this site (GA, Google Ads, Datadog RUM, MS Clarity,
#: LinkedIn, Spotify, Twitter). One hanging tracker would hold a navigation that has
#: been usable for ten seconds.
WAIT_UNTIL = "domcontentloaded"


def _goto(page, url: str, *, attempts: int = 2):
    """Navigate, tolerating one slow first paint.

    Retried because the failure it guards is asymmetric: a retried navigation costs a
    few seconds, while an un-retried one on the login path costs a session the human
    already established by hand. ⛔ Retrying navigation is safe precisely because it is
    a READ -- nothing in this module retries an action that could create an order.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until=WAIT_UNTIL, timeout=NAV_TIMEOUT_MS)
            return
        except Exception as e:                             # noqa: BLE001
            last = e
            print(f"  ⚠️  navigation to {url} timed out (attempt {attempt}/{attempts})")
    raise CheckoutError(
        f"could not load {url} after {attempts} attempts: {last}\n"
        f"The site was measured at ~12s to first render; if this persists it is the "
        f"network or the site, not a selector.")


@dataclass
class Person:
    """The buyer's details, read from a file -- never hardcoded in the repo.

    CPF, phone and address are personal data. They live in a gitignored `fields.json`
    for the same reason `subscribers.json` does in price-watcher: the keys are worth
    documenting, the values are not worth committing.
    """
    full_name: str
    email: str
    phone_number: str
    cpf: str
    cep: str
    uf: str
    bairro: str
    municipio: str
    address: str
    number: str

    @classmethod
    def load(cls, path: str | Path) -> "Person":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise CheckoutError(f"{path}: no such file. Copy fields.example.json and fill it.") from e
        except json.JSONDecodeError as e:
            raise CheckoutError(f"{path}: invalid JSON -- {e}") from e
        missing = [f for f in cls.__dataclass_fields__ if not raw.get(f)]
        if missing:
            # All of them, in one message. Reporting the first missing field means
            # finding the second one on the next run, and the third on the one after.
            raise CheckoutError(f"{path}: missing or empty {', '.join(missing)}")
        return cls(**{f: str(raw[f]) for f in cls.__dataclass_fields__})


def _actionable(el: dict) -> bool:
    """Whether a probed element is something a human could actually act on.

    ⛔ The reason this predicate exists: the checkout's first paint puts five
    `<a class="iconify svg-loading">` spinners on the page. They are real, visible
    elements, so a gate that waits for "any visible control" fires on them and dumps a
    step whose only contents are loading placeholders -- while the real
    "Comprar agora por R$316,80" button is still seconds away.

    Deliberately a shape rule, not a class-name blocklist: `svg-loading` is this site's
    spelling of "spinner" today, and matching on it would silently stop working the day
    it is renamed. Carrying text, or being a field a human types into, is what makes a
    control actionable anywhere.
    """
    if el.get("tag") in ("input", "select", "textarea"):
        return True
    return bool((el.get("text") or "").strip()
                or (el.get("aria") or "").strip()
                or (el.get("placeholder") or "").strip())


def settle(page, *, timeout_ms: int = 90_000, poll_ms: int = 500) -> list[dict]:
    """Wait until the app has rendered, and return THE CONTROLS IT SAW.

    ⛔ Not a fixed sleep. The first version waited 1200 ms and dumped 0 controls from a
    checkout page that was still blank -- a recon file that read "this step has no
    buttons" when it meant "we looked too early".

    ⛔ And not a count, either. Returning a count meant the caller re-probed afterwards,
    and this is a Bubble app that re-renders: `settle` saw controls, the DOM swapped
    during the settle delay, and the caller's separate probe wrote 0 for a page it had
    just been told was ready. Handing back the observation itself removes the window in
    which the two can disagree -- there is only one measurement now, so nothing can
    drift between the gate and the thing it gates.

    ⚠️ The budget is 90 s because the checkout page was MEASURED at 37.8 s headless and
    57.4 s headed on 2026-09-02. A 45 s cap would pass headless and fail headed on the
    same page -- the worst shape of flaky, since it would look like a headless-only bug.

    An empty list after the full budget is the honest, distinct outcome "nothing
    rendered": a page that truly has no controls and a page that never painted look
    identical in a dump, and only the elapsed time separates them.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last: list[dict] = []
    while time.monotonic() < deadline:
        try:
            els = page.evaluate(_PROBE_JS)
        except Exception:                                  # noqa: BLE001
            els = []                                       # mid-navigation; keep waiting
        if any(_actionable(e) for e in els):
            page.wait_for_timeout(1500)                    # let the rest of the tree land
            try:
                # Re-read after settling, and keep whichever view is richer. A Bubble
                # re-render mid-settle must never turn a populated step into an empty one.
                later = page.evaluate(_PROBE_JS)
                return later if len(later) >= len(els) else els
            except Exception:                              # noqa: BLE001
                return els
        last = els                                         # spinners only: keep waiting
        page.wait_for_timeout(poll_ms)
    # Timed out. Return whatever was last seen so the dump records the stuck state
    # (usually the spinners) rather than an empty file that says nothing about why.
    return last


@dataclass
class StepDump:
    """One observed checkout step, for the recon that has not happened yet."""
    step: int
    url: str
    title: str
    elements: list[dict] = field(default_factory=list)


_PROBE_JS = """() => {
  const sel = '""" + _CONTROL_SELECTOR + """';
  const vis = """ + _VISIBLE_JS + """;
  return [...document.querySelectorAll(sel)].filter(vis).map(el => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    name: el.getAttribute('name'),
    id: el.id || null,
    role: el.getAttribute('role'),
    aria: el.getAttribute('aria-label'),
    placeholder: el.getAttribute('placeholder'),
    disabled: el.getAttribute('aria-disabled') === 'true' || el.disabled === true,
    href: el.getAttribute('href'),
    cls: String(el.className || '').replace(/-module__\\w+__/g, '-module__~__'),
    text: (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
    visible: true
  }));
}"""


def login(playwright, *, headless: bool = False, session_path: Path | None = None) -> Path:
    """Open a real browser, let a HUMAN log in, save the session.

    Headed and interactive by design. Automating the password would mean storing it, and
    a fresh scripted login on buy night is also the least ordinary-looking thing this
    tool could do at the moment it most wants to look ordinary -- the ToS describes an
    antifraud system that scores "comportamento de compra". A session established days
    earlier, by hand, with "Lembrar de mim" ticked, is the boring path.

    ⭐ The session is SAVED BEFORE it is verified, and a failed verification warns
    instead of discarding it. The two mistakes are not symmetric: keeping a dead session
    costs one clear error at the next command, which already re-checks; discarding a
    live one costs the human the login they just performed. An earlier version got this
    backwards and threw away a real, successful login because its probe URL 404'd.
    """
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context()
    page = context.new_page()
    try:
        _goto(page, session_mod.LOGIN_URL)
        print("A browser is open. Log in (tick 'Lembrar de mim'), then return here.")
        input("Press Enter once you are logged in and can see your account... ")

        # Verify where the human actually ended up -- no navigation to a guessed path.
        # If they are still on /entrar, one nudge to a URL known to exist settles it.
        try:
            page.wait_for_load_state(WAIT_UNTIL, timeout=STEP_TIMEOUT_MS)
        except Exception:                                  # noqa: BLE001
            pass
        if session_mod.auth_signals(page)["on_login_wall"]:
            _goto(page, session_mod.HOME_URL)
            page.wait_for_timeout(2500)

        saved = session_mod.save(context, session_path)
        sig = session_mod.auth_signals(page)
        if session_mod.is_logged_in(page):
            print(f"✅ session saved to {saved}  (cookies={sig['cookies']})")
        else:
            # Saved anyway. Say plainly what was seen and what it means.
            print(f"⚠️  session saved to {saved}, but it does NOT look logged in: "
                  f"{session_mod.describe(sig)}.")
            print(f"   Signals: {sig}")
            print(f"   Check with:  python buy.py map --target <file>   "
                  f"(it re-verifies and never buys). Re-run login if it refuses.")
        return saved
    finally:
        browser.close()


def open_listing(playwright, listing_url: str, *, headless: bool = True,
                 session_path: Path | None = None):
    """Browser + context + page, already authenticated, sitting on the listing.

    Returns `(browser, context, page)`; the caller closes them. Raises `SessionError`
    rather than proceeding if the session turned out to be dead -- which is checked
    here, at the top of the flow, and not left to be discovered at the payment step.
    """
    state = session_mod.require(session_path)
    browser = playwright.chromium.launch(headless=headless)
    # ⚠️ `clipboard-read` is REQUIRED, not a nicety: the Pix payload is only ever
    # written to the clipboard (see `_pix_from_clipboard`). Granted here, at context
    # creation, because a permission discovered to be missing at the payment step is
    # discovered on the one screen where the order already exists.
    context = browser.new_context(storage_state=str(state),
                                  permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    _goto(page, listing_url)
    page.wait_for_timeout(2000)                    # the app re-renders after navigation
    if not session_mod.is_logged_in(page):
        sig = session_mod.auth_signals(page)
        browser.close()
        raise SessionError(
            f"not authenticated at {page.url} -- {session_mod.describe(sig)}.\n"
            f"Signals: {sig}\n"
            f"Run:  python buy.py login")
    return browser, context, page


#: Text that only appears once an order EXISTS. These are the true point of no return;
#: ⛔ an earlier version also treated the word "pix" as terminal, which halted mapping on
#: the payment-method SELECTION screen -- a page where nothing has been reserved and
#: three screens still separate you from the button that reserves.
POST_ORDER_MARKERS = ("copia e cola", "copia-e-cola", "pix copia",
                      "qr code", "qrcode", "pagamento pendente", "expira em")


def _select_pix(page) -> bool:
    """Tick the PIX payment radio if it is present and not already chosen."""
    for sel in ('input[value="PIX" i]', 'input[type="radio"]'):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                val = (el.get_attribute("value") or "").casefold()
                if "pix" in val:
                    if el.is_checked():
                        return False
                    el.check(force=True, timeout=5000)
                    return True
        except Exception:                                  # noqa: BLE001
            continue
    try:
        lbl = page.get_by_text("PIX", exact=False).first
        if lbl.count() and lbl.is_visible():
            lbl.click(timeout=5000)
            return True
    except Exception:                                      # noqa: BLE001
        pass
    return False


def map_checkout(page, out_dir: Path, *, max_steps: int = 8,
                 allow_final: bool = False,
                 person: "Person | None" = None) -> list[StepDump]:
    """Walk the checkout, dumping every step's interactive elements. ⛔ Never buys.

    This is the recon instrument, not the buyer. It advances only through controls whose
    visible text matches the flow Juan described, and it HARD-STOPS before the final
    "Comprar agora" -- the one control that creates a real reservation. The stop is a
    positive match on the terminal step, not a step counter: a counter would sail past
    the point of no return the first time the site inserts a screen.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dumps: list[StepDump] = []
    for step in range(1, max_steps + 1):
        try:
            page.wait_for_load_state(WAIT_UNTIL, timeout=STEP_TIMEOUT_MS)
        except Exception:                                  # noqa: BLE001
            pass                                           # settle() is the real gate
        elements = settle(page)
        if not any(_actionable(e) for e in elements):
            print(f"  ⚠️  step {step}: no actionable control after 90s "
                  f"({len(elements)} decorative element(s) only) — dumping for the "
                  f"record, then stopping.")
        dump = StepDump(step=step, url=page.url, title=page.title(), elements=elements)
        dumps.append(dump)
        (out_dir / f"step-{step:02d}.json").write_text(
            json.dumps(dump.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")
        page.screenshot(path=str(out_dir / f"step-{step:02d}.png"), full_page=True)
        print(f"  step {step}: {page.title()[:60]!r} — {len(elements)} controls "
              f"→ step-{step:02d}.json")
        if not any(_actionable(e) for e in elements):
            break

        body = (page.inner_text("body") or "").casefold()
        if any(m in body for m in POST_ORDER_MARKERS):
            print("  ⛔ stopping: this page shows an ORDER THAT ALREADY EXISTS.")
            break

        # Choosing a payment method changes no state on the server and is what reveals
        # the control that advances. Safe, and required to see the next screen at all.
        if _select_pix(page):
            print("     selected PIX")
            page.wait_for_timeout(1500)

        done = fill_known_fields(page, person)
        if done:
            print(f"     filled {len(done)}: {', '.join(done)}")
            page.wait_for_timeout(1200)

        nxt, label = _next_control(page, step=step, allow_final=allow_final)
        if nxt is None:
            print("  (no control found that is safe to click — flow ends here)")
            break
        print(f"     clicking {label!r}")
        nxt.click()

    return dumps


def _first_visible(page, text: str, limit: int = 12):
    """The first VISIBLE element matching `text`, or None.

    ⛔ Not `.first`. `get_by_text(...).first` returns the first element in DOM order and
    says nothing about whether it can be seen -- and this checkout leaves the previous
    screen's controls in the tree, hidden. So `.first` resolved to a stale, invisible
    "Continuar", `is_visible()` said False, and the mapper concluded there was no way
    forward while a perfectly good button sat on screen. Scan the matches; take the
    first that is actually visible.
    """
    try:
        loc = page.get_by_text(text, exact=False)
        n = min(loc.count(), limit)
    except Exception:                                      # noqa: BLE001
        return None
    for i in range(n):
        try:
            el = loc.nth(i)
            if el.is_visible():
                return el
        except Exception:                                  # noqa: BLE001
            continue
    return None


#: placeholder keyword -> Person attribute. Matched case-insensitively on a substring of
#: the field's placeholder, because that is the only stable label this Bubble form
#: exposes -- the inputs carry no `name` a human wrote, only generated record ids.
FIELD_HINTS = (
    ("e-mail", "email"), ("email", "email"),
    ("nome", "full_name"),
    ("cpf", "cpf"),
    ("telefone", "phone_number"), ("celular", "phone_number"), ("whatsapp", "phone_number"),
    ("cep", "cep"),
    ("bairro", "bairro"),
    ("cidade", "municipio"), ("munic", "municipio"),
    ("estado", "uf"), ("uf", "uf"),
    ("n\u00famero", "number"), ("numero", "number"), ("n\u00b0", "number"),
    ("endere", "address"), ("rua", "address"), ("logradouro", "address"),
)


def fill_known_fields(page, person: "Person | None") -> list[str]:
    """Fill every visible, empty text input whose placeholder we recognise.

    Returns the names of the fields filled. ⛔ Never touches a field that already has a
    value: the account may prefill its own details, and overwriting them would be both
    pointless and the least ordinary-looking thing to do on a form that antifraud
    watches. ⛔ Never touches the coupon box -- "Código do Cupom" matches no hint.
    """
    if person is None:
        return []
    filled: list[str] = []
    try:
        inputs = page.locator("input:not([type=radio]):not([type=checkbox]), textarea")
        n = min(inputs.count(), 40)
    except Exception:                                      # noqa: BLE001
        return filled
    for i in range(n):
        try:
            el = inputs.nth(i)
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
            ph = ((el.get_attribute("placeholder") or "")
                  + " " + (el.get_attribute("aria-label") or "")).casefold()
            if not ph.strip():
                continue
            attr = next((a for kw, a in FIELD_HINTS if kw in ph), None)
            if attr is None:
                continue
            el.fill(getattr(person, attr), timeout=5000)
            filled.append(f"{attr}<-{ph.strip()[:24]}")
        except Exception:                                  # noqa: BLE001
            continue
    return filled


def _next_control(page, *, step: int, allow_final: bool = False):
    """The control that advances the flow, or `(None, "")`.

    Text-based, because class names carry a build hash.

    ⛔ THE SAFETY RULE. "Comprar agora" is the label of BOTH the opening control (screen
    p=1, which merely opens the checkout -- verified 2026-09-02) and the final,
    order-creating one. They cannot be told apart by text, so they are told apart by
    POSITION: a "Comprar" control is clicked on step 1 and never again. Past that, only
    "Continuar" advances, and reaching a screen whose sole control is "Comprar agora"
    means the mapper has arrived at the button that reserves -- so it stops there and
    says so, which is precisely the outcome wanted from a tool that must not buy.
    """
    cand = _first_visible(page, "Continuar")
    if cand is not None:
        return cand, "Continuar"

    for label in ("Comprar agora", "Comprar"):
        cand = _first_visible(page, label)
        if cand is None:
            continue
        if step == 1 or allow_final:
            return cand, label
        print(f"  ⛔ STOP: the only way forward is {label!r} on screen {step}. "
              f"That is the control that CREATES THE ORDER — not clicking it.")
        return None, ""
    return None, ""


PIX_CODE_RE = re.compile(r"0002[0-9A-Za-z._\-*+/:%\s]{40,}")


#: The checkout's own summary, verified 2026-09-02:
#:     Ingresso              R$ 288,00
#:     Taxa de serviço (10%) R$  28,80
#:     Valor total           R$ 316,80   <- equals matriz_preco.preco_min
#: ⛔ So `preco_min` is the FEE-INCLUSIVE total, and a ceiling compares against the real
#: amount charged. Reading "the first R$ on the page" grabs the pre-fee 288,00 and
#: reports a 28,80 price move that never happened -- which is worse than no check at
#: all, because it aborts real purchases while looking like a working safeguard.
TOTAL_RE = re.compile(r"Valor\s+total\s*R?\$?\s*([\d.]+),(\d{2})", re.I)


def _page_price_cents(page) -> int | None:
    """The checkout's **Valor total** in centavos, or None if that label is absent.

    Anchored to the label, never positional. None means "could not verify" and the
    caller must fail closed on it -- an unreadable total is not a matching total.
    """
    try:
        body = re.sub(r"\s+", " ", page.inner_text("body") or "")
    except Exception:                                      # noqa: BLE001
        return None
    m = TOTAL_RE.search(body)
    if not m:
        return None
    return int(m.group(1).replace(".", "")) * 100 + int(m.group(2))


#: The order screen's copy button, verified live on a real order (#7707X57Q) 2026-09-02.
PIX_COPY_BUTTON = "#btn_copy"

#: Where an order can be found AFTER the fact. ⛔ Not the checkout URL: reopening that
#: starts a FRESH checkout showing no order, which reads as "nothing was reserved" when
#: a real reservation exists. That mistake was made, live, on the night this was written.
ORDERS_URL = "https://buyticketbrasil.com/ingressos"


def _looks_like_pix(val: str) -> bool:
    """A Pix EMV payload opens with the payload-format tag `0002`. Length is checked too
    because the page carries long Bubble record ids that also begin with digits."""
    return bool(val) and val.strip().startswith("0002") and len(val.strip()) > 40


def _pix_from_clipboard(page) -> dict:
    """Click "Copiar código" and read what it wrote to the clipboard.

    ⚠️ Two traps, both hit live on 2026-09-02 and neither guessable from the DOM:

    1. A `<div class="greyout ...">` sits above the button, so Playwright's `.click()`
       waits for actionability and times out after 30 s against a button it has just
       reported "visible, enabled and stable". `HTMLElement.click()` via `evaluate`
       dispatches the handler directly and is not subject to pointer interception.
    2. The context must hold `clipboard-read` or `readText()` rejects -- returning
       nothing, indistinguishably from "there was no code". `open_listing` grants it.
    """
    for sel in (PIX_COPY_BUTTON, "button:has-text('Copiar')"):
        try:
            if not page.locator(sel).count():
                continue
            page.evaluate("sel => document.querySelector(sel)?.click()", sel)
            page.wait_for_timeout(600)
            val = (page.evaluate("navigator.clipboard.readText()") or "").strip()
            if _looks_like_pix(val):
                return {"pix_code": val, "source": f"clipboard via {sel}"}
        except Exception:                                  # noqa: BLE001
            continue
    return {}


def _wait_for(page, predicate, *, timeout_ms: int = 45_000, poll_ms: int = 250):
    """Poll until `predicate(page)` returns truthy, or the deadline passes.

    ⛔ Not a fixed sleep, and deliberately not `settle()`. `settle` returns as soon as
    ANY actionable control exists, and the post-order page is full of them (Voltar, the
    help links) while the Pix section is still rendering. A gate that generic is why the
    first two real orders were read too early and reported as failures.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            got = predicate(page)
            if got:
                return got
        except Exception:                                  # noqa: BLE001
            pass                                           # mid-navigation; keep polling
        page.wait_for_timeout(poll_ms)
    return None


def _pending_rows(page) -> list:
    """Every visible "Aguardando pagamento" row in Comprados, newest first."""
    out = []
    try:
        for el in page.get_by_text("Aguardando pagamento", exact=False).all():
            if el.is_visible():
                out.append(el)
    except Exception:                                      # noqa: BLE001
        return []
    return out


def _pix_from_orders_page(page, *, timeout_ms: int = 45_000,
                          max_orders: int = 3) -> dict:
    """Navigate to the ORDER and read its code.

    ⭐ THE correction from three real runs. Clicking the final "Comprar agora" does NOT
    turn the checkout into a Pix screen: the page keeps its `/checkout?...&p=2` URL,
    shows "Aguarde…", and never renders a code. Waiting longer never helps -- the code
    was never coming to that page. It is on the order, at `/ingressos` -> **Comprados**,
    where `#btn_copy` appears within ~0.04 s of the row being opened.

    ⛔ And the first pending row is NOT necessarily this run's order. A hold from an
    earlier run that has not lapsed yet still reads "Aguardando pagamento" and sorts
    ahead of an order the site has not finished publishing -- which is exactly what
    happened on the third run: the fallback opened a stale order, found no copy button,
    and reported "no Pix code" over a checkout that had gone through.

    So it does not guess which row is ours: it tries each pending order and returns the
    first that actually yields a payload. A lapsed or unpayable hold has no `#btn_copy`
    and is skipped. The caller reports `order_url` so a human confirms WHICH order was
    read, because "the one that had a code" is a heuristic, not an identity.

    ⛔ Read-only: it navigates and clicks account UI. It cannot create an order.
    """
    for idx in range(max_orders):
        try:
            _goto(page, ORDERS_URL)
            tab = _wait_for(page, lambda pg: _first_visible(pg, "Comprados"),
                            timeout_ms=timeout_ms)
            if tab is None:
                return {}
            tab.click()
            rows = _wait_for(page, _pending_rows, timeout_ms=timeout_ms)
            if not rows:
                return {}                      # nothing pending at all -- no order
            if idx >= len(rows):
                return {}                      # exhausted the pending orders
            rows[idx].click()
            # A shorter budget per candidate: a lapsed order will never grow the button,
            # and spending the full 45 s on each would blow the 10-minute hold.
            if _wait_for(page, lambda pg: pg.locator(PIX_COPY_BUTTON).count(),
                         timeout_ms=12_000) is None:
                continue
            got = _extract_pix(page)
            if got:
                got["order_url"] = page.url
                return got
        except Exception:                                  # noqa: BLE001
            continue
    return {}


def _extract_pix(page) -> dict:
    """The Pix copia-e-cola and, if present, the QR image.

    ⭐ VERIFIED 2026-09-02 against a real order, which overturned this function's whole
    premise. It was written blind against three plausible shapes -- a readonly input, a
    textarea, and the page text -- and **all three were wrong**. The payload is not in
    the DOM at any point: the order screen renders the literal words "Código Pix" beside
    a `Copiar código` button, and the code exists only on the clipboard that button
    writes. The three DOM shapes are kept below because they cost nothing and a redesign
    may yet expose one, but the clipboard is the path that works.

    The caller still RAISES when every path comes back empty, because a reservation that
    exists while its code was not captured is the worst state this tool can leave behind.
    """
    for sel in ("input[readonly]", "textarea", "input[type=text]"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 10)):
                val = (loc.nth(i).input_value() or "").strip()
                if _looks_like_pix(val):
                    return {"pix_code": val, "source": f"{sel}[{i}]"}
        except Exception:                                  # noqa: BLE001
            continue
    try:
        m = PIX_CODE_RE.search(page.inner_text("body") or "")
        if m and _looks_like_pix(m.group(0)):
            return {"pix_code": m.group(0).strip(), "source": "body-text"}
    except Exception:                                      # noqa: BLE001
        pass
    return _pix_from_clipboard(page)


def run_checkout(page, person: Person, *, dry_run: bool = True,
                 expect_cents: int | None = None,
                 out_dir: Path | None = None, clock=None) -> dict:
    """Drive the checkout to a Pix payload. ⛔ Reserves; never pays.

    Written from the map of 2026-09-02, which walked all five screens live:

        p=1  "Comprar agora"                        -> opens the checkout
        p=2  select PIX, "Continuar"
        p=2  "E-mail do recebedor", "Continuar"     (progressive disclosure, same URL)
        p=2  9 personal fields, "Continuar"
        p=2  "Comprar agora"                        <- CREATES THE ORDER

    The screens share one URL and accumulate, so the flow is driven by which controls
    are visible, never by a page counter.
    """
    out_dir = Path(out_dir) if out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for step in range(1, 9):
        elements = settle(page)
        if clock is not None:
            clock.mark(f"screen {step} rendered")
        if not any(_actionable(e) for e in elements):
            raise CheckoutError(f"step {step}: nothing rendered after 90s at {page.url}")

        if _select_pix(page):
            page.wait_for_timeout(1500)
        if fill_known_fields(page, person):
            page.wait_for_timeout(1200)

        cont = _first_visible(page, "Continuar")
        if cont is not None:
            cont.click()
            page.wait_for_timeout(2000)
            continue

        final = _first_visible(page, "Comprar agora") or _first_visible(page, "Comprar")
        if final is None:
            raise CheckoutError(f"step {step}: no 'Continuar' and no 'Comprar agora' at "
                                f"{page.url} -- the flow changed; re-run `map`.")

        # ⛔ Screen 1's only control is ALSO labelled "Comprar agora", and it merely
        # OPENS the checkout -- verified 2026-09-02. Text cannot separate it from the
        # order-creating button of the last screen, so position does, exactly as
        # `_next_control` does it. Without this the dry run "succeeded" on screen 1
        # having exercised nothing, which is a pass that proves the flow works when it
        # never ran.
        if step == 1:
            final.click()
            page.wait_for_timeout(2500)
            continue

        # ── the point of no return ────────────────────────────────────────────────
        # Re-read the price off the CHECKOUT itself. The listing was chosen from a
        # payload fetched seconds ago and this is a market that moves within minutes;
        # committing to a number nobody re-checked is how you buy at a price you never
        # approved. A drift ABORTS -- it never silently accepts the new price.
        shown = _page_price_cents(page)
        if expect_cents is not None:
            if shown is None:
                # Fail CLOSED. An unverifiable total is not a verified one, and this is
                # the last moment before money is committed.
                raise CheckoutError(
                    f"could not read 'Valor total' at {page.url} -- refusing to commit "
                    f"a purchase whose price could not be verified. Nothing was "
                    f"ordered. Re-run `map` if the summary layout changed.")
            if shown != expect_cents:
                raise CheckoutError(
                    f"price moved before the final click: expected {expect_cents} "
                    f"centavos, the checkout's Valor total is {shown}. Nothing was "
                    f"ordered.")
        if out_dir:
            page.screenshot(path=str(out_dir / "pre-order.png"), full_page=True)
        if dry_run:
            return {"dry_run": True, "price_cents": shown, "url": page.url,
                    "note": "stopped one click short of 'Comprar agora'"}

        final.click()
        break
    else:
        raise CheckoutError("checkout did not reach a final control in 8 screens")

    # ── an order now exists; capturing its code is the only thing that matters ──────
    if clock is not None:
        clock.mark("ORDER CREATED (final click)")
    page.wait_for_timeout(4000)
    settle(page, timeout_ms=120_000)
    if clock is not None:
        clock.mark("order screen settled")
    if out_dir:
        page.screenshot(path=str(out_dir / "order.png"), full_page=True)

    pix = _extract_pix(page)
    if not pix:
        # The checkout page never becomes the Pix screen -- go to the order itself.
        pix = _pix_from_orders_page(page)
    if clock is not None:
        clock.mark("PIX CODE EXTRACTED")
    if not pix:
        # ⛔ Never retried and never swallowed. A retry would risk a SECOND reservation,
        # and a swallow would leave a real, paid-for-able order with no code to pay it.
        raise CheckoutError(
            f"AN ORDER MAY EXIST but no Pix code could be read.\n"
            f"⚠️ Find it at {ORDERS_URL} -> the 'Comprados' tab, open the order and "
            f"press 'Copiar código'.\n"
            f"⛔ NOT at {page.url} -- reopening a checkout URL starts a FRESH checkout "
            f"and shows no order, which reads as 'nothing was reserved' while a real "
            f"reservation is running down its clock.\n"
            f"⏰ The hold is ~10 MINUTES from the order, not 30.\n"
            f"A screenshot is in {out_dir or '(no out_dir given)'}.\n"
            f"⛔ Do not re-run this command; it would reserve a second ticket.")

    qr = None
    if out_dir:
        try:
            img = page.locator("img[src^='data:image'], canvas").first
            if img.count():
                qr = out_dir / "pix-qr.png"
                img.screenshot(path=str(qr))
        except Exception:                                  # noqa: BLE001
            qr = None
    return {"dry_run": False, "pix_code": pix["pix_code"], "qr_png": qr,
            "source": pix.get("source"),
            "order_url": pix.get("order_url") or page.url, "url": page.url}
