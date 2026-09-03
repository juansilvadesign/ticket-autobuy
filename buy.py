#!/usr/bin/env python3
"""ticket-autobuy CLI.

    python buy.py login                          establish the browser session (headed)
    python buy.py resolve --target <file>        read the market, print what would be bought
    python buy.py map     --target <file>        walk the checkout, dump selectors, NEVER buy
    python buy.py buy     --target <file>        reserve + send the Pix code

Exit codes, matching price-watcher's grammar:
    0  ok / nothing matched
    1  usage or config error
    2  the site could not be read (blind)
    3  something happened that YOU were not told about
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: The human benchmark this tool has to beat. Juan does the whole manual flow -- see the
#: alert, open the listing, fill the checkout, copy the Pix -- in about 60 s, and on a hot
#: listing the row is gone inside that. Anything slower is a tool that reliably produces a
#: correct answer about a ticket somebody else already bought.
BUDGET_S = 60.0
sys.path.insert(0, str(HERE))

from autobuy import checkout, config, listing, notify, session, timing  # noqa: E402
from autobuy.errors import (AutobuyError, CheckoutError, ConfigError,   # noqa: E402
                            NoMatch, ResolveError, SessionError)


def _fmt_brl(cents: int) -> str:
    """Same rendering as price-watcher's `fmt_brl`, so the two tools never disagree
    about what a price 'is' in a message you read at 3am."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}R$ {whole:,.0f}".replace(",", ".") + f",{frac:02d}"


def _load_env(path: Path) -> None:
    """Parse `.env`. ⛔ Parsed, never sourced -- a value with a backtick or a `$(` in it
    becomes shell execution the moment it is sourced, and this file holds a bot token."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _resolve(cfg) -> tuple[listing.Candidate, str]:
    url = listing.build_event_url(cfg.event_slug, cfg.data_millis,
                                  cfg.evento_local, cfg.cidade)
    candidates = listing.parse_candidates(listing.fetch_rsc(url), url)
    best = listing.choose(
        candidates,
        max_price_cents=cfg.max_price_cents,
        quantity=cfg.quantity,
        sectors=cfg.sectors,
        entry_classes=cfg.entry_classes,
        min_price_cents=cfg.min_price_cents,
    )
    return best, url


def cmd_resolve(args) -> int:
    # Read-only: works on a disarmed target by design.
    cfg = config.load(args.target, require_armed=False)
    url = listing.build_event_url(cfg.event_slug, cfg.data_millis, cfg.evento_local, cfg.cidade)
    candidates = listing.parse_candidates(listing.fetch_rsc(url), url)

    print(f"{cfg.label}")
    print(f"  ceiling {_fmt_brl(cfg.max_price_cents)} · qty {cfg.quantity}"
          f" · sector {cfg.sectors or 'any'} · class {cfg.entry_classes or 'any'}\n")
    for c in sorted(candidates, key=lambda c: c.price_cents):
        mark = "  " if c.price_cents > cfg.max_price_cents else "→ "
        print(f"  {mark}{c.item:<42} {_fmt_brl(c.price_cents):>13}  qty={c.quantity:>4}")

    try:
        best = listing.choose(candidates, max_price_cents=cfg.max_price_cents,
                              quantity=cfg.quantity, sectors=cfg.sectors,
                              entry_classes=cfg.entry_classes,
                              min_price_cents=cfg.min_price_cents)
    except NoMatch as e:
        print(f"\nnothing armed: {e}")
        return 0                       # data, not failure -- the ordinary outcome
    print(f"\n✅ would buy: {best.item} at {_fmt_brl(best.price_cents)}")
    print(f"   {best.url(cfg.event_slug)}")
    return 0


def cmd_login(args) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        checkout.login(p, headless=False)
    return 0


def cmd_map(args) -> int:
    from playwright.sync_api import sync_playwright
    # `map` never buys, so it must not require arming. Making the SAFE command demand
    # `enabled: true` is a reason to leave targets armed between runs, which is exactly
    # backwards -- the disarmed state should be the comfortable one to work in.
    cfg = config.load(args.target, require_armed=False)
    best, _ = _resolve(cfg)
    url = best.url(cfg.event_slug)
    person = None
    if args.fill:
        person = checkout.Person.load(args.fields)
        print("filling forms from", args.fields)
    out = HERE / "recon" / cfg.target_id
    print(f"mapping from {best.item} at {_fmt_brl(best.price_cents)}\n  {url}\n")
    with sync_playwright() as p:
        browser, _, page = checkout.open_listing(p, url, headless=args.headless)
        try:
            checkout.map_checkout(page, out, max_steps=args.max_steps,
                                  allow_final=False, person=person)
        finally:
            browser.close()
    print(f"\n✅ dumps in {out}")
    return 0


def cmd_buy(args) -> int:
    from playwright.sync_api import sync_playwright
    # ⏱ The clock starts on the FIRST line of work, not at the browser. The question
    # this tool exists to answer is "alert -> payable code"; config parsing and the RSC
    # read are inside that budget whether or not they are the interesting part.
    clock = timing.Clock(f"buy {Path(args.target).stem}")
    try:
        cfg = config.load(args.target)
        person = checkout.Person.load(args.fields)
        session.require()              # fail at startup, not at the payment screen
        clock.mark("config + session")
        best, _ = _resolve(cfg)
        clock.mark("resolve market")
        url = best.url(cfg.event_slug)
        print(f"\u2192 {best.item} at {_fmt_brl(best.price_cents)}\n  {url}")

        with sync_playwright() as p:
            browser, _, page = checkout.open_listing(p, url, headless=not args.headed)
            clock.mark("browser + listing")
            try:
                result = checkout.run_checkout(
                    page, person, dry_run=args.dry_run,
                    expect_cents=best.price_cents,
                    out_dir=HERE / "receipts" / cfg.target_id, clock=clock)
            finally:
                browser.close()

        if result.get("dry_run"):
            print(f"\n\u2705 dry run: stopped one click short of 'Comprar agora'. "
                  f"Nothing was ordered.\n   {result}")
            return 0

        notify.send_pix(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            label=cfg.label, item=best.item,
            price_brl=_fmt_brl(best.price_cents),
            pix_code=result["pix_code"], qr_png=result.get("qr_png"),
        )
        return 0
    finally:
        # \u26d4 In `finally`: an abort is exactly when the breakdown matters most, and a
        # report that prints only on success cannot explain a run that ran out of time.
        print("\n" + clock.report(budget_s=BUDGET_S), flush=True)


def main(argv=None) -> int:
    _load_env(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="open a browser and save the session").set_defaults(fn=cmd_login)

    for name, fn, helptext in (
        ("resolve", cmd_resolve, "read the market and print what would be bought"),
        ("map", cmd_map, "walk the checkout and dump selectors (never buys)"),
        ("buy", cmd_buy, "reserve and send the Pix code"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--target", required=True, help="a price-watcher target JSON")
        sp.set_defaults(fn=fn)

    sub.choices["map"].add_argument(
        "--fill", action="store_true",
        help="fill recognised form fields from --fields while mapping, so the flow can "
             "advance past the data screens. Still never clicks the final button.")
    sub.choices["map"].add_argument(
        "--fields", default=str(HERE / "fields.json"), help="buyer personal data")
    sub.choices["map"].add_argument(
        "--max-steps", type=int, default=8,
        help="how many screens to advance through. Use a small number to walk the flow "
             "one click at a time when you are unsure what the next control does.")
    sub.choices["map"].add_argument(
        "--headless", action="store_true",
        help="map with no visible window -- use it to prove the headless path works "
             "before relying on it for an unattended buy")

    buy_p = sub.choices["buy"]
    buy_p.add_argument("--fields", default=str(HERE / "fields.json"),
                       help="buyer personal data (gitignored)")
    buy_p.add_argument("--headed", action="store_true", help="show the browser")
    buy_p.add_argument("--dry-run", action="store_true", default=False,
                       help="stop before the order-creating click")

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr);   return 1
    except ResolveError as e:
        print(f"blind: {e}", file=sys.stderr);    return 2
    except NoMatch as e:
        print(f"no match: {e}");                  return 0
    except (SessionError, CheckoutError) as e:
        print(f"stopped: {e}", file=sys.stderr);  return 3
    except notify.NotifyError as e:
        print(f"NOT DELIVERED: {e}", file=sys.stderr); return 3
    except AutobuyError as e:
        print(f"error: {e}", file=sys.stderr);    return 3


if __name__ == "__main__":
    raise SystemExit(main())
