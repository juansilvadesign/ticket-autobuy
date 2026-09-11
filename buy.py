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
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: The human benchmark this tool has to beat. Juan does the whole manual flow -- see the
#: alert, open the listing, fill the checkout, copy the Pix -- in about 60 s, and on a hot
#: listing the row is gone inside that. Anything slower is a tool that reliably produces a
#: correct answer about a ticket somebody else already bought.
BUDGET_S = 60.0

#: The span that is actually ENFORCED: resolve -> the order-creating click.
#: ⛔🔴 `BUDGET_S` above is a REPORT, and it always was -- nothing ever read it but the
#: printed verdict line. It also measures the wrong span to enforce. Both 2026-09-10
#: failures stood at ~30 s when they clicked and ~83 s when they finished, the entire
#: overrun sitting in the 47 s post-click Pix read. Enforcing 60 s TOTAL would have
#: fired only after the click -- where aborting abandons a live unpaid hold -- and never
#: once during the window it was meant to protect.
#: ⭐ 45 s against a measured ~30 s walk: wide enough that a healthy run never trips it,
#: tight enough to stop the pathological ones the comments already record (a 102 s
#: screen 1; a 117 s run that "lost the listing outright"). Those are precisely the runs
#: that click into a listing that has already been sold.
WALK_BUDGET_S = 45.0
sys.path.insert(0, str(HERE))

from autobuy import (checkout, config, listing, notify, runner,        # noqa: E402
                     session, timing)
from autobuy.errors import (AutobuyError, BudgetExceeded, CheckoutError,  # noqa: E402
                            ConfigError, NoMatch, NoOrderCreated,
                            OrderMayExistError, ResolveError, SessionError)


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
                 clock=None, deadline_s: float | None = None):
    """The one purchase path. `buy` and `autobuy` MUST share it -- two copies would
    drift, and the copy that drifts is the one that spends money unattended.

    ⚠️ `deadline_s` defaults to None -- no enforcement. Only `autobuy` passes it. A
    manual `buy` is attended: a human chose the moment, is watching the browser, and can
    judge for themselves whether a slow run is still worth finishing. The budget exists
    to stop an UNATTENDED click landing on a listing that sold while nobody was looking.
    """
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
                out_dir=HERE / "receipts" / cfg.target_id, clock=clock,
                deadline_s=deadline_s)
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


def _report_rejected(state: dict, cfg, readings: list[dict]) -> None:
    """Say out loud that something was under the ceiling and still not bought.

    ⛔🔴 The gap this closes cost two real windows. See `runner.rejected_under_ceiling`:
    on 2026-09-06 and 2026-09-07 a R$198,00 listing sat under a R$200,00 ceiling across
    7 and 10 polls, price-watcher alerted `CRITICAL -- under R$ 200,00` both times, and
    this tool wrote NOTHING anywhere, because `min_available: 20` refused the row before
    the first print statement. The ceiling was the number everyone was watching, so a
    second filter silently holding the buy was invisible for three days.

    ⭐ The log line is unconditional and the Telegram is throttled, in that order. The
    log is what makes a run diagnosable afterwards; the message is what makes it noticed
    at the time -- and only the second one can train a reader to mute the channel.
    ⛔ Its OWN throttle key, per target: a chatty night must not eat the slot another
    night needs, and `session_dead` must not eat this one.
    ⚠️ A delivery failure is swallowed here, unlike the Pix code's. This rides on top of
    a log line that has already landed, so the carve-out CLAUDE.md grants the QR image
    applies -- an advisory must not become exit 3 and abort the remaining nights.
    """
    rejected = runner.rejected_under_ceiling(readings, cfg)
    if not rejected:
        return

    def money(r: dict) -> str:
        p = r.get("price_cents")
        return _fmt_brl(p) if isinstance(p, int) and p > 0 else "?"

    for r, reason in rejected:
        print(f"⚠ {cfg.label}: {r.get('item')} at {money(r)} is AT OR UNDER the "
              f"ceiling {_fmt_brl(cfg.max_price_cents)} and was NOT bought -- {reason}",
              flush=True)

    if not runner.should_alert(state, f"rejected:{cfg.target_id}"):
        return
    runner._write_state(state)
    body = "\n".join(f"• {r.get('item')} at {money(r)} -- {why}"
                     for r, why in rejected)
    try:
        notify.send_text(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            f"⚠ ticket-autobuy did NOT buy a ticket that was under your ceiling\n\n"
            f"{cfg.label}\nceiling {_fmt_brl(cfg.max_price_cents)}\n\n{body}\n\n"
            f"The session and the poller are fine -- a buy FILTER refused it. Change it "
            f"in the target's `buy` block, or accept the miss.")
    except Exception as e:                                            # noqa: BLE001
        print(f"   (could not deliver the rejected-row alert: "
              f"{type(e).__name__}: {e})", flush=True)


def cmd_orders(args) -> int:
    """Read `/ingressos` -> Comprados and say what is actually there.

    ⛔🔴 The step `CLAUDE.md` and every `OrderMayExistError` message DEMAND -- *"Verify
    at /ingressos -> Comprados before re-arming"* -- and which had no command. So it was
    done by hand, or not at all: the 2026-09-10 **14:24** UNCONFIRMED ledger row was
    never checked by anyone, and it sat there blocking the 11/09 night for 25 hours.
    A verification step with no instrument is a verification step that gets skipped.

    ⛔ READ-ONLY, and structurally so: it navigates, clicks the Comprados tab, and reads.
    It never opens an order's payment control and cannot reach a checkout, so there is
    no path from here to a reservation.
    """
    from playwright.sync_api import sync_playwright                   # noqa: PLC0415

    state_path = session.require()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        try:
            context = browser.new_context(storage_state=str(state_path))
            page = context.new_page()
            checkout._goto(page, checkout.ORDERS_URL)
            page.wait_for_timeout(3000)
            tab = checkout._wait_for(
                page, lambda pg: checkout._first_visible(pg, "Comprados"),
                timeout_ms=30_000)
            if tab is None:
                # ⛔ Reported as UNKNOWN, never as "nothing there". This is the exact
                # conflation that made `_pix_from_orders_page` unusable as evidence.
                print("⚠ could not reach the 'Comprados' tab -- this is NOT evidence "
                      "that no order exists. Re-run, or look by hand.")
                return 3
            tab.click()
            page.wait_for_timeout(3000)
            rows = checkout._pending_rows(page)
            ids = checkout.pending_order_ids(page)
            shot = Path(args.out) if args.out else None
            if shot:
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=True)
            print(f"Comprados rendered at {page.url}")
            print(f"  pending ('Aguardando pagamento') rows: {len(rows)}")
            print(f"  order ids visible: {sorted(ids) if ids else '(none)'}")
            if shot:
                print(f"  screenshot: {shot}")
            if not rows:
                print("\n✅ NOTHING PENDING. No unpaid hold exists on this account.")
            else:
                print("\n⚠ A pending hold exists -- open it by hand before re-arming.")
            return 0
        finally:
            browser.close()


def cmd_status(args) -> int:
    """Answer 'is this actually going to buy tonight?' in one command.

    ⛔🔴 The instrument whose absence cost the 11/09 night. Answering that question
    used to mean reading three files by hand -- the target's `buy.enabled`, the ledger
    in `state/autobuy.json`, and the tail of `autobuy.log` -- and the one state that
    mattered (fired + disarmed) was precisely the state that wrote NOTHING to the third.
    A tool whose armed/disarmed state can only be inferred is a tool that will be found
    disarmed 25 hours late.

    Read-only by construction: it opens no browser, touches no network, takes no lock
    and writes no state. Safe to run at any time, including mid-buy.
    """
    from datetime import datetime, timezone

    state = runner._read_state()
    now = datetime.now(timezone.utc)
    targets = sorted(Path(args.targets).glob("*.json"))
    if not targets:
        print(f"no target files in {args.targets}")
        return 0

    print(f"ticket-autobuy status  ·  {runner.now_local():%Y-%m-%d %H:%M} "
          f"{runner.now_local().tzname()}  ·  buy window "
          f"{runner.WINDOW_START:%H:%M}-{runner.WINDOW_END:%H:%M}  ·  "
          f"{'INSIDE' if runner.within_window(runner.now_local()) else 'OUTSIDE'}")
    print()
    any_armed = False
    for tpath in targets:
        tid = runner.target_id_of(tpath) or tpath.stem
        label = _label_of(tpath, tid)
        entry = runner.fired_entry(state, tid)
        try:
            raw = json.loads(tpath.read_text(encoding="utf-8"))
            blk = raw.get("buy") if isinstance(raw.get("buy"), dict) else {}
        except (OSError, json.JSONDecodeError):
            blk = {}
        enabled = blk.get("enabled") is True
        ceiling = blk.get("max_price_brl")

        # ⭐ THREE outcomes, never two. "CLOSED" and "PARKED" are both `enabled: false`
        # and they mean opposite things -- one is a failure nobody saw, the other is a
        # deliberate choice. The ledger row is the only thing that tells them apart.
        if entry is not None:
            kind = ("🔴 CLOSED (UNCONFIRMED order)" if runner.is_unconfirmed(entry)
                    else "✅ CLOSED (bought)")
        elif enabled:
            kind = "🟢 ARMED"; any_armed = True
        else:
            kind = "⚪ PARKED (disarmed by hand)"

        print(f"  {label}")
        print(f"     {kind}"
              + (f"   ceiling R$ {ceiling:,.2f}".replace(",", ".") if ceiling else ""))
        if entry is not None:
            print(f"     ledger: {entry.get('item')} at "
                  f"{_fmt_brl(entry.get('price_cents') or 0)} on {entry.get('at')}")
            print(f"     order:  {entry.get('order_url')}")

        hist = Path(args.history) / f"{tid}.jsonl"
        try:
            readings = runner.latest_readings(hist, now=now)
            cheapest = min((r for r in readings
                            if isinstance(r.get("price_cents"), int)),
                           key=lambda r: r["price_cents"], default=None)
            if cheapest:
                age = (now - datetime.fromisoformat(
                    cheapest["captured_at"])).total_seconds()
                mark = ""
                if ceiling and cheapest["price_cents"] <= int(round(ceiling * 100)):
                    # ⛔ The line that would have shouted on 11/09: 67 in-window polls
                    # sat under the ceiling while the night was closed.
                    mark = ("  ← AT OR UNDER THE CEILING"
                            + ("" if enabled and entry is None
                               else " and NOTHING WILL BE BOUGHT"))
                print(f"     market: {_fmt_brl(cheapest['price_cents'])} "
                      f"({cheapest.get('item')}, qty {cheapest.get('quantity')}), "
                      f"polled {age/60:.0f} min ago{mark}")
        except AutobuyError as e:
            print(f"     market: ⚠ cannot see this night -- {e}")
        print()

    if not any_armed:
        print("⛔ NOTHING IS ARMED. No dip on any night will be bought.")
    return 0


def _label_of(path: Path, target_id: str) -> str:
    """The target's human label, read WITHOUT the buy block.

    ⚠️ `cfg.label` is unavailable on exactly the targets this matters for -- a disarmed
    one never survives `config.load`. Falls back to the id, which is always enough to
    name the night.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        lbl = raw.get("label")
        return lbl if isinstance(lbl, str) and lbl else target_id
    except (OSError, json.JSONDecodeError):
        return target_id


def _report_inert(state: dict, cfg_label: str, target_id: str,
                  entry: dict) -> None:
    """Say that this night is CLOSED, instead of skipping it in silence.

    ⛔🔴 The 25 silent hours this exists to prevent. On 2026-09-10 14:24 BRT an
    `OrderMayExistError` disarmed the 11/09 night (fail-closed, working as designed).
    From that minute until 2026-09-11 15:39 the cron fired ~1,500 more times and
    `state/autobuy.log` gained **0 bytes**, because a fired/disarmed target is skipped
    by a bare `continue`. The session was alive (keepalive ✅ every 20 min), the poller
    was healthy, and **67 in-window polls on 11/09 sat at or under the R$250 ceiling** --
    four straight minutes at R$220,00 with qty 178 among them. A night that is switched
    OFF looked exactly like a night where nothing dipped.
    → the same shape as `_report_rejected`, one gate higher up.

    ⭐ Log unconditional, Telegram throttled -- the order `_report_rejected` established.
    ⛔ Only an UNCONFIRMED row nags. A night that really bought is FINISHED, and a
    reminder about a job well done is exactly the traffic that teaches a reader to mute
    the channel the Pix code arrives on.
    """
    unconfirmed = runner.is_unconfirmed(entry)
    print(f"— {cfg_label}: night CLOSED, nothing will be bought -- ledger says "
          f"{'an order MAY exist (UNCONFIRMED)' if unconfirmed else 'bought'} "
          f"at {_fmt_brl(entry.get('price_cents') or 0)} on {entry.get('at')}. "
          f"Clear it from state/autobuy.json and re-arm buy.enabled to buy again.",
          flush=True)
    if not unconfirmed:
        return                       # a finished night is allowed to be quiet
    if not runner.should_alert(state, f"inert:{target_id}",
                               every_s=runner.INERT_ALERT_EVERY_S):
        return
    runner._write_state(state)
    try:
        notify.send_text(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            f"⚠ ticket-autobuy is DISARMED and buying NOTHING\n\n{cfg_label}\n\n"
            f"The ledger holds an UNCONFIRMED order from {entry.get('at')} at "
            f"{_fmt_brl(entry.get('price_cents') or 0)} -- the checkout raised after the "
            f"order-creating click, so the night was disarmed fail-closed.\n\n"
            f"Check https://buyticketbrasil.com/ingressos -> 'Comprados'.\n"
            f"Then EITHER pay it, OR clear the entry from state/autobuy.json and set "
            f"buy.enabled: true to resume.\n\n"
            f"Until you do, every dip is being skipped.")
    except Exception as e:                                            # noqa: BLE001
        print(f"   (could not deliver the inert-night alert: "
              f"{type(e).__name__}: {e})", flush=True)


def _alert_disarmed(cfg, hit: dict) -> None:
    """Telegram the DISARM at the moment it happens. Never throttled.

    ⛔ This is the message whose absence cost the 11/09 night. The fail-closed disarm
    is correct and must stay; what was missing is that it happened on a channel nobody
    was watching -- a stderr line in a log file, at 14:24, on a day the reader had
    already checkpointed at 12:00. By construction it can fire at most once per night
    (the disarm makes it unrepeatable), so it needs no throttle and must not take one:
    a throttle slot shared with anything else could swallow the only warning there is.

    ⚠️ Delivery failure is swallowed with a printed note. The ledger write and the
    disarm have ALREADY happened when this runs, and the caller still has to re-raise
    `OrderMayExistError` -- an undelivered advisory must not rewrite that outcome.
    """
    try:
        notify.send_text(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            f"🔴 ticket-autobuy DISARMED {cfg.label}\n\n"
            f"It clicked to order {hit.get('item')} at "
            f"{_fmt_brl(hit.get('price_cents') or 0)} and then could NOT read the Pix "
            f"code. An order MAY be live and running down a ~10 minute hold.\n\n"
            f"→ https://buyticketbrasil.com/ingressos -> 'Comprados'\n\n"
            f"⛔ This night is now DISARMED: nothing further will be bought on it, "
            f"however far the price falls, until you clear the entry from "
            f"state/autobuy.json AND set buy.enabled: true.")
    except Exception as e:                                            # noqa: BLE001
        print(f"   (could not deliver the DISARMED alert: "
              f"{type(e).__name__}: {e})", flush=True)


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
            # ⛔🔴 The ledger is read FIRST, and that order is the fix. `disarm_target`
            # and `record_fired` fire together, so from the next minute `config.load`
            # raises ConfigError (disabled) and the `already_fired` check below it was
            # unreachable -- a disarmed night fell through a bare `continue` into total
            # silence for 25 hours on 2026-09-10/11. Checking the ledger before the buy
            # block is validated is the only order in which a CLOSED night can still say
            # so. ⚠ A target with no ledger row stays silent exactly as before: the six
            # parked nights are deliberately off and must not become 6 daily messages.
            tid = runner.target_id_of(tpath)
            entry = runner.fired_entry(state, tid) if tid else None
            if entry is not None:
                _report_inert(state, _label_of(tpath, tid), tid, entry)
                continue
            try:
                cfg = config.load(tpath)          # armed targets only
            except ConfigError:
                continue                          # disarmed or no buy block: not an error
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
                # ⛔ NOT a plain `continue`. "Nothing matched" and "something matched
                # your ceiling and a filter refused it" used to be the same silence.
                _report_rejected(state, cfg, readings)
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
                best, result = _execute_buy(cfg, fields=args.fields, clock=clock,
                                            deadline_s=WALK_BUDGET_S)
            except NoMatch:
                # The dip evaporated between price-watcher's read and ours. The single
                # most likely outcome on a fast market, and an ordinary no-op.
                print("   gone before we got there; nothing ordered", flush=True)
                continue
            except BudgetExceeded as e:
                # ⭐ Pre-click by construction, so it is graded exactly like NoMatch:
                # no ledger row, no disarm, the night stays ARMED and the next minute
                # tries again on a fresher read. ⛔ NOT an OrderMayExistError -- refusing
                # to click is the opposite of having clicked.
                print(f"   {e}", flush=True)
                continue
            except NoOrderCreated as e:
                # ⭐🔴 The SAME outcome as NoMatch, discovered one click later. Both
                # 2026-09-10 runs ended here and were graded `OrderMayExistError`
                # instead; the second one disarmed the 11/09 night for good, 25 hours
                # before anyone noticed. ⛔ No bookkeeping is owed: the two-signal test
                # inside `NoOrderCreated` has already established that no reservation
                # exists, so a ledger row or a disarm would be recording a purchase that
                # provably did not happen. The night stays ARMED and the next dip is
                # still eligible -- which is the entire point.
                print(f"   {e}", flush=True)
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
                # ⛔ AFTER the ledger write and the disarm, never before: an alert that
                # beat the bookkeeping could be the only trace of a reservation the
                # ledger then failed to record.
                _alert_disarmed(cfg, hit)
                raise
            finally:
                # ⭐ The autobuy path is the one that ENFORCES the walk, so it is the one
                # that must print it. The manual `buy` path above keeps the total-only
                # report -- labelling a budget "ENFORCED" where nothing enforces it is
                # exactly the kind of decorative declaration that hid the last bug.
                print("\n" + clock.report(budget_s=BUDGET_S,
                                          walk_budget_s=WALK_BUDGET_S), flush=True)

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
    od = sub.add_parser("orders",
                       help="read /ingressos -> Comprados; never buys")
    od.set_defaults(fn=cmd_orders)
    od.add_argument("--headed", action="store_true", help="show the browser")
    od.add_argument("--out", default=str(HERE / "receipts" / "orders-check.png"),
                    help="screenshot path -- the evidence a ledger decision rests on")

    st = sub.add_parser("status",
                       help="is anything actually armed? reads only -- never buys")
    st.set_defaults(fn=cmd_status)
    st.add_argument("--targets", default=str(PW / "targets"))
    st.add_argument("--history", default=str(PW / "history"))

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
    except NoOrderCreated as e:
        # ⛔ BEFORE the CheckoutError arm below, which would grade it exit 3. Nothing was
        # reserved, so this is an ordinary no-op, not a failure needing a human.
        print(f"nothing ordered: {e}");           return 0
    except BudgetExceeded as e:
        print(f"too slow, nothing ordered: {e}"); return 0
    except (SessionError, CheckoutError) as e:
        print(f"stopped: {e}", file=sys.stderr);  return 3
    except notify.NotifyError as e:
        print(f"NOT DELIVERED: {e}", file=sys.stderr); return 3
    except AutobuyError as e:
        print(f"error: {e}", file=sys.stderr);    return 3


if __name__ == "__main__":
    raise SystemExit(main())
