"""Read a price-watcher target file and extract the `buy` block.

The target JSON is price-watcher's, unchanged -- this tool is a READER of it and adds
one optional key. That is deliberate: the night you buy is the night you least want two
files disagreeing about which sector you meant.

⛔ price-watcher itself never reads `buy` and never will; an unknown key is inert to it.
The boundary in its CLAUDE.md ("it never buys") stays literally true -- the watcher does
not gain a buy path, this tool gains a config parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import ConfigError


def brl_to_cents(value, field: str) -> int:
    """'220.00' | 220 | 220.0 -> 22000.

    Via `Decimal(str(...))`, never `int(value * 100)`. The float path turns 220.30 into
    22029 on some values, and a threshold that is one centavo low simply never fires --
    silently, and only on the prices unlucky enough to land on a binary-inexact value.
    """
    try:
        cents = Decimal(str(value)) * 100
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ConfigError(f"{field}: {value!r} is not a number") from e
    if cents != cents.to_integral_value():
        raise ConfigError(f"{field}: {value!r} is finer than one centavo")
    cents = int(cents)
    if cents <= 0:
        raise ConfigError(f"{field}: must be positive, got {value!r}")
    return cents


def _as_list(value, field: str) -> list[str] | None:
    """`null` means "no constraint"; a list means "one of these". A bare string is
    accepted and wrapped, because writing `"sector": "Gramado"` is the obvious typo and
    silently treating it as a 7-character filter would match nothing, forever."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value or None
    raise ConfigError(f"{field}: expected a string, a list of strings, or null")


@dataclass(frozen=True)
class BuyConfig:
    target_id: str
    label: str
    event_slug: str
    data_millis: object
    evento_local: str
    cidade: str | None
    max_price_cents: int
    quantity: int
    sectors: list[str] | None
    entry_classes: list[str] | None
    min_price_cents: int


def load(path: str | Path, *, require_armed: bool = True) -> BuyConfig:
    """Parse one target file. Raises `ConfigError` unless it is fully usable.

    `require_armed=False` is for the read-only `resolve` command. Inspecting what WOULD
    be bought must not require arming the target first -- the alternative is that the
    only way to sanity-check a ceiling is to switch on the thing that spends, which
    turns the safe command into a reason to leave targets armed.

    Everything is validated HERE, at load, including values only the last step would
    touch. A buy config that is enabled but missing `max_price_brl` must refuse to
    load -- the same call price-watcher's registry makes for a rule enabled without its
    parameter, and for the same reason: a config discovered to be broken at the instant
    it was meant to fire has already cost you the thing it existed to get.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path}: no such target file") from e
    except json.JSONDecodeError as e:
        # Never treated as "no buy configured". An unparseable file means we do not know
        # what you asked for, which is not the same as you not having asked.
        raise ConfigError(f"{path}: invalid JSON -- {e}") from e

    buy = raw.get("buy")
    if buy is None:
        raise ConfigError(f"{path}: no 'buy' block. Add one, or use a target that has it.")
    if not isinstance(buy, dict):
        raise ConfigError(f"{path}: 'buy' must be an object")
    if require_armed and not buy.get("enabled", False):
        raise ConfigError(f"{path}: 'buy.enabled' is false -- refusing to run on a "
                          f"target that is not armed. (`resolve` works without arming.)")

    if "max_price_brl" not in buy:
        raise ConfigError(f"{path}: 'buy' is enabled but has no 'max_price_brl'. "
                          f"An armed buy with no ceiling would take any price.")
    max_cents = brl_to_cents(buy["max_price_brl"], f"{path}: buy.max_price_brl")
    min_cents = brl_to_cents(buy["min_price_brl"], f"{path}: buy.min_price_brl") \
        if buy.get("min_price_brl") is not None else 0
    if min_cents and min_cents > max_cents:
        raise ConfigError(f"{path}: buy.min_price_brl exceeds buy.max_price_brl -- "
                          f"nothing can ever match.")

    quantity = buy.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ConfigError(f"{path}: buy.quantity must be an integer >= 1, got {quantity!r}")

    params = raw.get("params") or {}
    for key in ("event_slug", "data_millis", "evento_local"):
        if not params.get(key):
            raise ConfigError(f"{path}: params.{key} is required to build the event URL")

    return BuyConfig(
        target_id=raw.get("id") or path.stem,
        label=raw.get("label") or raw.get("id") or path.stem,
        event_slug=params["event_slug"],
        data_millis=params["data_millis"],
        evento_local=params["evento_local"],
        cidade=params.get("cidade"),
        max_price_cents=max_cents,
        quantity=quantity,
        sectors=_as_list(buy.get("sector"), f"{path}: buy.sector"),
        entry_classes=_as_list(buy.get("entry_class"), f"{path}: buy.entry_class"),
        min_price_cents=min_cents,
    )
