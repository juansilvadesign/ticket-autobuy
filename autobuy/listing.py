"""Resolve a buy config to ONE listing URL -- the step that skips the entire ticket UI.

⭐ The finding this module exists for (recon 2026-09-02, see RECON.md):

    matriz_preco["Gramado||Inteira"].id_ref  ==  the `c_anuncio` query parameter
    on the event page's "Comprar" button.

So a purchase does not have to operate the two dropdowns at all. Given an `id_ref`
already present in every price-watcher `Reading.extra`, the buyer can navigate
straight to:

    https://buyticketbrasil.com/r?event=<slug>&c_anuncio=<id_ref>

That deletes the three most fragile things a DOM-driven buyer would have had to do on
a night when listings move within minutes: opening two CSS-module-hashed dropdowns
whose class names (`TicketCard-module__CLLKWG__dropdownHead`) carry a build hash that
changes on any redeploy; racing the re-render between the two selections; and parsing
prices back out of concatenated option labels ("GramadoR$ 319332" is name+price+qty
with no separator).

Money is integer centavos here, exactly as in price-watcher. `preco_min` is served in
centavos already and a float near a threshold is how you buy the wrong ticket.
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import NoMatch, ResolveError

BASE_EVENT = "https://buyticketbrasil.com/evento/"
BASE_LISTING = "https://buyticketbrasil.com/r"

# Honest and identifiable, matching price-watcher's adapter rather than spoofing Chrome.
# The recon confirmed the RSC endpoint answers a request with no User-Agent at all, so
# there is nothing here to work around.
USER_AGENT = "ticket-autobuy/0.1 (personal ticket buyer; contact: repo owner)"

TIMEOUT_S = 30


@dataclass(frozen=True)
class Candidate:
    """One `sector||class` listing, priced and locatable."""

    sector: str
    entry_class: str
    price_cents: int
    quantity: int
    id_ref: str

    @property
    def item(self) -> str:
        return f"{self.sector} || {self.entry_class}"

    def url(self, event_slug: str) -> str:
        """The direct listing URL. Both values are quoted: `id_ref` is third-party text
        and an unescaped one would silently retarget the purchase."""
        query = urllib.parse.urlencode({"event": event_slug, "c_anuncio": self.id_ref})
        return f"{BASE_LISTING}?{query}"


def _grab(text: str, key: str):
    """Pull one top-level JSON value out of the RSC stream by key name.

    Lifted deliberately from price-watcher's adapter, including the whitespace
    handling: `JSONDecoder.raw_decode` raises on leading whitespace rather than
    skipping it, so matching the separator with a regex is what stops a future
    pretty-printing change upstream from reading as "the payload shape is gone".
    """
    m = re.search(rf'"{re.escape(key)}"\s*:\s*', text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[m.end():])
    except json.JSONDecodeError:
        return None
    return value


def build_event_url(event_slug: str, data_millis, evento_local: str,
                    cidade: str | None = None) -> str:
    query = {"data": data_millis, "evento_local": evento_local}
    if cidade:
        query["cidade"] = cidade
    return BASE_EVENT + urllib.parse.quote(event_slug) + "?" + urllib.parse.urlencode(query)


def fetch_rsc(url: str) -> str:
    """The event page over the RSC endpoint.

    `RSC: 1` returns the same data at roughly half the bytes and -- decisively -- with
    the embedded JSON UNESCAPED. The plain HTML response escapes it
    (`\\"preco_min\\":27500`), which would force an unescaping pass before any of the
    parsing below could work.
    """
    req = urllib.request.Request(url, headers={
        "RSC": "1",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise ResolveError(f"fetch failed for {url}: {e}") from e


def parse_candidates(body: str, url: str = "") -> list[Candidate]:
    """Every purchasable `sector||class` in the payload.

    An absent `matriz_preco` RAISES -- the payload shape is gone and this resolver is
    blind. Returning `[]` there would be indistinguishable from a sold-out event and
    would make every subsequent run report "nothing under your price".
    """
    matriz = _grab(body, "matriz_preco")
    if matriz is None:
        raise ResolveError(
            f"'matriz_preco' not found in the RSC payload ({len(body)} bytes"
            f"{' from ' + url if url else ''}). The site's payload shape likely "
            f"changed -- re-run the recon before trusting this resolver."
        )
    if not isinstance(matriz, dict):
        raise ResolveError(f"'matriz_preco' is {type(matriz).__name__}, expected object")

    out: list[Candidate] = []
    for key, row in matriz.items():
        sector, _, entry_class = key.partition("||")
        try:
            price_cents = int(row["preco_min"])
            quantity = int(row["disponivel"])
        except (KeyError, TypeError, ValueError):
            continue                      # one malformed row is skipped; a missing matriz raises
        id_ref = row.get("id_ref")

        # Placeholder rows: the site carries e.g. "Meia aposentado" at preco_min 0 with
        # 0 available. Left in, a zero wins every min() and would target a purchase at
        # a listing that cannot be bought.
        if price_cents <= 0 or quantity <= 0:
            continue
        # No id_ref means no direct URL. Dropping it is safe (it can still be watched);
        # keeping it would produce a buy target that 404s at the moment it is used.
        if not id_ref:
            continue

        out.append(Candidate(sector.strip(), entry_class.strip(),
                             price_cents, quantity, str(id_ref)))
    return out


def choose(candidates: list[Candidate], *, max_price_cents: int,
           quantity: int = 1,
           sectors: list[str] | None = None,
           entry_classes: list[str] | None = None,
           min_price_cents: int = 0,
           min_available: int = 0) -> Candidate:
    """The cheapest candidate satisfying the buy config. Raises `NoMatch` if none is.

    `min_price_cents` is the anomaly floor and it is OPT-IN, defaulting to off. In
    price-watcher the floor is mandatory because a mispriced row poisons `lowest_ever`
    permanently and silently. Here the consequence is the opposite: the bot only ever
    RESERVES, and a human pays the Pix. So an anomalous R$ 66,00 row is a decision to
    put in front of you, not a trap to filter out -- you look at it and either pay or
    let the 30-minute hold lapse. Set it if you would rather not be woken for one.

    ⭐ `min_available` is a DEPTH floor, and it is the difference between a tool that
    buys and a lottery. Measured 2026-09-04 across three live runs on event day:

        Gramado || Inteira        qty 168  -> ORDER CREATED (#6999ZUKS)
        Gramado || Meia Professor qty   4  -> no order
        Gramado || Meia Idoso     qty   1  -> no order

    The checkout takes ~50-80 s from the live re-resolve to the order-creating click,
    and a 1-4 unit row on a hot event day is simply GONE inside that window: the click
    lands on a listing that no longer exists, the app bounces back to the event page,
    and nothing is reserved. Depth was already the TIE-BREAK below; the measurement
    says it has to be a FILTER, because the cheapest row is usually the thinnest one.
    """
    pool = [c for c in candidates if c.price_cents <= max_price_cents]
    if min_price_cents:
        pool = [c for c in pool if c.price_cents >= min_price_cents]
    if sectors:
        want = {s.casefold() for s in sectors}
        pool = [c for c in pool if c.sector.casefold() in want]
    if entry_classes:
        want = {e.casefold() for e in entry_classes}
        pool = [c for c in pool if c.entry_class.casefold() in want]
    pool = [c for c in pool if c.quantity >= max(quantity, min_available)]

    if not pool:
        raise NoMatch(
            f"no listing at or under {max_price_cents} centavos"
            + (f" in {sectors}" if sectors else "")
            + (f" of class {entry_classes}" if entry_classes else "")
            + f" with at least {max(quantity, min_available)} available"
        )
    # Cheapest first; ties broken by the deeper stock, which is the one less likely to
    # be gone by the time the browser reaches it.
    return min(pool, key=lambda c: (c.price_cents, -c.quantity))
