#!/usr/bin/env python3
"""ticket-autobuy CLI.

    python buy.py login                          establish the browser session (headed)
    python buy.py resolve --target <file>        read the market, print what would be bought
    python buy.py map     --target <file>        walk the checkout, dump selectors, NEVER buy
    python buy.py buy     --target <file>        reserve + send the Pix code
    python buy.py autobuy                        cron: buy once per night on a dip

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

from autobuy import (checkout, config, listing, notify, runner,        # noqa: E402
                     session, timing)
from autobuy.errors import (AutobuyError, CheckoutError, ConfigError,   # noqa: E402
                            NoMatch, OrderMayExistError, ResolveError,
                            SessionError)


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
        min_available=cfg.min_available,
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
                              min_price_cents=cfg.min_price_cents,
                              min_available=cfg.min_available)
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


def _execute_buy(cfg, *, fields, headed: bool = False, dry_run: bool = False,
                 clock=None):
    """The one purchase path. `buy` and `autobuy` MUST share it -- two copies would
    drift, and the copy that drifts is the one that spends money unattended."""
    from playwright.sync_api import sync_playwright
    person = checkout.Person.load(fields)
    session.require()                  # fail at startup, not at the payment screen
    if clock is not None:
        clock.mark("config + session")
    best, _ = _resolve(cfg)            # ⚠️ LIVE re-resolve: the authority, not history
    if clock is not None:
        clock.mark("resolve market")
    url = best.url(cfg.event_slug)
    print(f"\u2192 {best.item} at {_fmt_brl(best.price_cents)}\n  {url}", flush=True)

    with sync_playwright() as p:
        browser, _, page = checkout.open_listing(p, url, headless=not headed)
        if clock is not None:
            clock.mark("browser + listing")
        try:
            result = checkout.run_checkout(
                page, person, dry_run=dry_run, expect_cents=best.price_cents,
                out_dir=HERE / "receipts" / cfg.target_id, clock=clock)
        finally:
            browser.close()
    return best, result


def cmd_buy(args) -> int:
    # \u23f1 The clock starts on the FIRST line of work, not at the browser. The question
    # this tool exists to answer is "alert -> payable code"; config parsing and the RSC
    # read are inside that budget whether or not they are the interesting part.
    clock = timing.Clock(f"buy {Path(args.target).stem}")
    try:
        cfg = config.load(args.target)
        best, result = _execute_buy(cfg, fields=args.fields, headed=args.headed,
                                    dry_run=args.dry_run, clock=clock)
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


def _alert_session_dead(state: dict, cfg, hit: dict) -> None:
    """Telegram the failure that DELETES this feature instead of degrading it.

    ⛔ Called from BOTH places a dead session can surface, because the obvious one is
    not the one that fires. `session.require()` is a FILE check: it passes happily on a
    session that exists, carries cookies, and is thoroughly logged out. The real probe
    is `checkout.open_listing`, behind the browser -- and its `SessionError` used to
    travel straight past this alert into exit 3.

    Measured 2026-09-04, on a session 36 h old against an observed lifetime under 4 h:
    the homepage showed 3 `Entrar` links, `state/autobuy.json` did not exist, and
    `state/autobuy.log` held ZERO "not authenticated" lines. The guard CLAUDE.md calls
    load-bearing had never once fired -- it was a declaration nothing asserted.

    ⚠️ Throttled via `should_alert`: a 1-minute cron would otherwise send 1,440
    identical messages a day and train the reader to mute the channel the Pix code
    arrives on.
    """
    if runner.should_alert(state, "session_dead"):
        runner._write_state(state)
        notify.send_text(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            f"\U0001f534 ticket-autobuy is BLIND\n\n{cfg.label} dipped to "
            f"{_fmt_brl(hit['price_cents'])} and it could NOT buy: the "
            f"BuyTicket session is dead.\n\nRun:  python buy.py login\n\n"
            f"Nothing is being bought on any night until you do.")


def cmd_autobuy(args) -> int:
    """Cron entry point. At most ONE reservation per invocation, one per night ever."""
    from datetime import datetime, timezone

    lock = runner.acquire_lock()
    if lock is None:
        # A previous invocation is still mid-purchase. ~40s of work on a 1-minute cron
        # WILL overlap; two runs seeing one dip is how one ticket becomes two.
        if args.verbose:
            print("another autobuy run holds the lock; skipping")
        return 0
    try:
        now = runner.now_local()
        if not runner.within_window(now):
            if args.verbose:
                print(f"outside the buy window at {now:%H:%M} {now.tzname()}; skipping")
            return 0

        # ⛔ ONCE per run, from the poller itself -- never from a history file's age.
        # Those are change logs: a stable market writes nothing, so an age check over
        # them would call a steady R$150 dip "blind" and skip the very thing we want.
        runner.assert_poller_alive(Path(args.history))

        state = runner._read_state()
        blind: list[str] = []
        for tpath in sorted(Path(args.targets).glob("*.json")):
            try:
                cfg = config.load(tpath)          # armed targets only
            except ConfigError:
                continue                          # disarmed or no buy block: not an error
            if runner.already_fired(state, cfg.target_id):
                continue
            hist = Path(args.history) / f"{cfg.target_id}.jsonl"
            try:
                readings = runner.latest_readings(
                    hist, now=datetime.now(timezone.utc))
            except AutobuyError as e:
                # A missing or unreadable file for an ARMED target. Not "no dip" --
                # we cannot see this night at all, and that must be said out loud.
                blind.append(str(e))
                continue
            hit = runner.candidate_under_ceiling(readings, cfg)
            if hit is None:
                continue

            # ⛔ A dead session is the failure that silently DELETES this feature: the
            # session observed on 2026-09-02 lasted under 4 hours, and without an alert
            # the cron would go on running, finding dips, and failing to buy any of them
            # into a log nobody reads. Checked here, before the browser, so the message
            # says "re-login" rather than arriving as a checkout stack trace.
            try:
                session.require()
            except SessionError:
                _alert_session_dead(state, cfg, hit)
                raise

            print(f"\u2193 {cfg.label}: {hit['item']} at "
                  f"{_fmt_brl(hit['price_cents'])} <= ceiling "
                  f"{_fmt_brl(cfg.max_price_cents)} -- buying", flush=True)
            clock = timing.Clock(f"autobuy {cfg.target_id}")
            try:
                best, result = _execute_buy(cfg, fields=args.fields, clock=clock)
            except NoMatch:
                # The dip evaporated between price-watcher's read and ours. The single
                # most likely outcome on a fast market, and an ordinary no-op.
                print("   gone before we got there; nothing ordered", flush=True)
                continue
            except ResolveError as e:
                # ⛔🔴 Observed 2026-09-05: this used to escape the loop to `main()`
                # (exit 2) and kill the WHOLE run at the first blind night. Targets
                # iterate in sorted() order, so 04/09 -- left armed at a temporary
                # R$1.000 ceiling and blind since its event ended -- matched at R$297
                # every minute and aborted 468 consecutive runs before 05/09..13/09
                # were ever evaluated. `autobuy.log` held ZERO lines for any of them
                # while a 40-minute R$198,00 dip came and went on 2026-09-04.
                # Collected here exactly like a blind HISTORY read: the `if blind:`
                # raise below still makes the run shout, and the LATER nights still
                # get looked at.
                # ⭐ No bookkeeping is owed and the night stays armed -- `_resolve`
                # runs BEFORE the browser, so this is strictly pre-click and no
                # reservation can exist.
                blind.append(f"{cfg.target_id}: {e}")
                continue
            except SessionError:
                # ⭐ THE path that actually fires. `session.require()` above already
                # passed -- it is a file check -- and the live probe inside
                # `open_listing` is where a logged-out session is really discovered.
                _alert_session_dead(state, cfg, hit)
                raise
            except OrderMayExistError:
                # ⛔ Raised AFTER the order-creating click ("AN ORDER MAY EXIST but no
                # Pix code could be read"), so a live reservation may exist. The ledger
                # and the disarm therefore belong HERE too, not only on the happy path.
                # Recording only on success is exactly how the next cron minute buys a
                # SECOND ticket for a night that already has one -- observed live
                # 2026-09-04, when this branch left the target armed on a 1-minute cron.
                # ⚠️ Deliberately fails CLOSED: if it turns out no order was created,
                # clearing one ledger key by hand is cheap; a duplicate reservation is
                # not.
                state = runner.record_fired(
                    state, cfg.target_id, price_cents=hit["price_cents"],
                    item=hit["item"],
                    order_url="UNCONFIRMED -- the checkout raised after the final "
                              "click. Verify at /ingressos -> Comprados before re-arming.")
                runner._write_state(state)
                runner.disarm_target(tpath)
                raise
            finally:
                print("\n" + clock.report(budget_s=BUDGET_S), flush=True)

            # ⛔ Record BEFORE notifying. A delivery failure must never leave a real
            # reservation absent from the ledger -- that is how the next cron minute
            # buys a second ticket for the same night.
            state = runner.record_fired(
                state, cfg.target_id, price_cents=best.price_cents,
                item=best.item, order_url=result.get("order_url"))
            runner._write_state(state)
            runner.disarm_target(tpath)

            notify.send_pix(
                os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                os.environ.get("TELEGRAM_CHAT_ID", ""),
                label=cfg.label, item=best.item,
                price_brl=_fmt_brl(best.price_cents),
                pix_code=result["pix_code"], qr_png=result.get("qr_png"),
            )
            return 0                              # one reservation per invocation

        if blind:
            raise AutobuyError("; ".join(blind))
        return 0
    finally:
        lock.close()


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

    PW = HERE.parent / "price-watcher"
    ab = sub.add_parser("autobuy",
                        help="cron entry point: buy once per night when it dips")
    ab.set_defaults(fn=cmd_autobuy)
    ab.add_argument("--targets", default=str(PW / "targets"),
                    help="price-watcher targets directory")
    ab.add_argument("--history", default=str(PW / "history"),
                    help="price-watcher history directory -- the TRIGGER source. Using "
                         "it costs zero extra requests to the site; a second poller "
                         "would nearly double the request budget.")
    ab.add_argument("--fields", default=str(HERE / "fields.json"),
                    help="buyer personal data (gitignored)")
    ab.add_argument("--verbose", "-v", action="store_true",
                    help="say why nothing happened -- without it a skipped run is silent, "
                         "which is correct for cron and useless when you are debugging")

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
