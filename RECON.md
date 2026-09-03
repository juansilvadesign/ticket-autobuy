# buyticketbrasil checkout — recon, 2026-09-02

Method: Playwright MCP against the live site, logged **out**, plus `curl` on the RSC
endpoint. Nothing was purchased and no order was created. The walk stopped at the login
wall, which is where an unauthenticated browser runs out of flow.

> Read this before writing a single checkout selector. The previous recon on this site
> overturned three confident assumptions in a row; this one overturned two more.

---

## ✅ Verified

### 1. ⭐ `matriz_preco[...].id_ref` **is** the buy button's `c_anuncio`

The finding the whole tool rests on. Selecting *Gramado* + *Inteira* on the event page
turned the `Comprar` anchor's href into:

```
https://buyticketbrasil.com/r?event=rockinrio2026&c_anuncio=1788351098784x401080915725898750
```

and that id is byte-identical to `matriz_preco["Gramado||Inteira"].id_ref` in the RSC
payload — which **price-watcher already stores** in `Reading.extra["id_ref"]`.

**Consequence:** the buyer never operates the ticket UI. It goes straight to the listing
URL. That deletes the three most fragile steps a DOM-driven buyer would need on a night
when listings move within minutes:

- opening two dropdowns whose class names carry a build hash (below),
- racing the re-render between the first and second selection,
- parsing prices back out of concatenated labels — `"GramadoR$ 319332"` is
  name + price + qty with **no separator**.

Pinned as a regression test in `tests/test_listing.py`.

### 2. 🔴 The checkout is **not** a guest flow — it is behind `/entrar`

Navigating to the listing URL while logged out redirects to
`https://buyticketbrasil.com/entrar?...&anuncio=rock-in-rio-2026qerm&p=1`.

This **overturns the working assumption** that the described flow (`fill email` → `fill
personal info`) was a guest checkout. It is not: those steps sit *after* authentication.
A session is mandatory, and the tool must fail on its absence at startup rather than
discover it mid-race.

The login form is **email + password**, with a *"Lembrar de mim"* checkbox and a
*"Cadastrar"* link. No OTP or magic link at this gate — so it is scriptable — but the
ToS (§6.2) reserves the right to ask for 2FA, so a login path must not assume it never
will.

### 3. ⚠️ Class names carry a build hash — never select on them

```
TicketCard-module__CLLKWG__dropdownHead
                   ^^^^^^ changes on any redeploy
```

A selector pinned to `CLLKWG` works in testing and silently stops matching the first
time the site ships. On this tool that is a buy night where nothing happens and nothing
errors. Use roles, `aria-label`s and visible text; where a class is unavoidable, match
by substring (`[class*="buyBtn"]`).

Stable anchors observed: `[aria-label="Selecione o ingresso"]`, `[role="option"]`,
`aria-disabled="true"` on both the sold-out options and the inert buy button.

### 4. The buy button is inert until **both** dropdowns are set

`<a aria-disabled="true">` with **no href** until sector *and* category are chosen; the
href appears only then. So the displayed `R$ 308` on a fresh page is the event floor,
not a purchasable target — another reason the direct-URL path is the right one.

### 5. Sold-out rows are marked, and `matriz_preco` is not the whole picture

Disabled options (`aria-disabled="true"`, `~dropdownItemDisabled`) show `R$ 0` — e.g.
*Vip - Club One*, *Meia Até 21*, *Acompanhante PCD*, *Meia aposentado*.

⚠️ **These R$ 0,00 rows were NOT in `matriz_preco` in this capture.** price-watcher's
notes record them *inside* `matriz_preco` on 2026-09-01; one day later all 19 rows are
priced and available. They live in `entradas`/`tipos_ingresso`, which the dropdown reads.
The `price <= 0` guard is therefore still required and **this fixture cannot exercise
it** — `tests/test_listing.py` says so explicitly so a green suite is not misread as
evidence it was tested against real data.

### 6. ToS: no anti-automation clause — but antifraud scores *purchase behaviour*

Searched `robô` · `automatizado` · `script` · `bot` · `crawler` · `scraper` · `spider`.
**There is no prohibition on automated purchasing.** The only automation language
describes *their* systems.

But §4.2: *"Sistema antifraude automatizado, que analisa **padrões de risco e
comportamento de compra**"*, and §5.5 lists the penalty: *"cancelar a transação,
suspender o ingresso e/ou **bloquear a conta do usuário**."*

⇒ The risk is not legal, it is behavioural. Mitigations already in the design: a session
established **by hand, days earlier** (a fresh scripted login on buy night is the least
ordinary thing the tool could do at the worst moment), an honest User-Agent, and no
retry loop on a failed checkout.

### 7. 🔴 The refund window for the 04/09 night has **already closed**

§5.1.4: *"O direito de arrependimento só poderá ser exercido se houver, no momento da
solicitação, **mais de 48 horas de antecedência** em relação à data do evento"* — and
*"Caso o ingresso já tenha sido transferido ou acessado… o comprador abriu mão do
direito de cancelamento."*

Event 04/09 14h00 → the window shut **02/09 14h00**. Anything paid for on buy night is
non-refundable.

⇒ This is a constraint on **paying**, not on the bot: the tool only ever *reserves*, and
the Pix hold (⏰ ~**10** minutes, measured — not the 30 assumed here originally) is the
human gate. The worst a bug can do is create a
reservation that lapses by itself. It is recorded here because it is the reason the tool
must never be given the ability to pay.

### 8. `robots.txt` unchanged — and irrelevant to this path

`Allow: /` with `Disallow: /api/` + `/_next/`. The resolver reads `/evento/…` (allowed).
The checkout is a human-initiated purchase in a real browser on the user's own account,
which is not crawling.

### 9. Timing and auth signals — measured 2026-09-02 after the first `login` failure

| | |
|---|---|
| `/entrar` → `domcontentloaded` | **~13 s** (12.9 / 13.6 s across runs) |
| `/entrar` → `load` | ~11.5 s — *not* the bottleneck |
| homepage → `domcontentloaded` | ~3 s warm |
| `/minhas-compras`, `/conta`, `/perfil` | **404** |

⛔ **`/minhas-compras` does not exist.** It was inferred from the site's own Terms
(*"ir até 'Minhas Compras'"*) and used as the post-login probe. A real, successful login
was therefore reported as a failure and the session discarded. **A path read out of
prose is a guess** — probe a URL you have watched respond. Verification now uses the
homepage, which is confirmed 200.

✅ **The real paths, read off the logged-in nav 2026-09-02** (not guessed):

| path | what it is |
|---|---|
| `/minha-conta` | profile + seller balance. **200** — note the singular, and no `s` |
| `/ingressos` | tabs: *Meus anúncios* · **Comprados** · *Vendidos* |
| `/ingressos?pagina=comprado&ID=<bubble-id>` | ⭐ one order: status, countdown, `Copiar código` |

⭐ **`Comprados` is where an order can be confirmed to exist.** It is the only view that
answers "did my checkout actually reserve something", and it settled exactly that
question on the first real purchase.

⚠️ 30 s was not a generous navigation timeout on a page that needs 13 s cold; headed,
with a cold profile, it left about one slow render of margin. Now 90 s with one retry,
and `wait_until="domcontentloaded"` — never `"load"`, which waits on all seven
analytics/ad origins.

⭐ **Cookie count is not an authentication signal.** A logged-**out** context measured
**0 cookies immediately after `goto` and 20 a few seconds later**, once the trackers
fired. Anything gating on a cookie count is measuring Google Analytics. The signal that
actually works is `a[href*="/entrar"]`: **2–3 when logged out, 0 when logged in**. Both
are reported separately by `session.auth_signals()` so a failure can name which one
fired, rather than collapsing "session expired" and "page had not rendered" into one
message.

---

## ✅ The checkout flow — MAPPED end to end, 2026-09-02

Walked live with a real session, headless, stopping on the final button.

| screen | URL | controls | advance |
|---|---|---|---|
| 1 | `/checkout/<slug>?…&p=1` | `Comprar agora por R$316,80` | click it — **opens** the checkout |
| 2 | `/checkout?…&p=2` | `Código do Cupom`, ⦿`PIX` ○`Cartão`, `Continuar` | select PIX → Continuar |
| 3 | `…p=2` | `E-mail do recebedor` | fill → Continuar |
| 4 | `…p=2` | 9 personal fields | fill → Continuar |
| 5 | `…p=2` | **`Comprar agora`** | ⛔ **creates the order** |

⚠️ **Screens 2–5 share one URL** and accumulate — it is progressive disclosure, not
pages. Drive by which controls are *visible*, never by a page counter.

⛔ **"Comprar agora" is the label of BOTH screen 1 and screen 5.** Text cannot separate
the harmless opener from the button that spends. Position does: click it on screen 1,
never again. `_next_control` and `run_checkout` both enforce this.

### The checkout is a **Bubble** app, not Next.js

`class="clickable-element bubble-element Button"`. The event page is Next.js; the
checkout is the Bubble backend's own UI — a different stack, which explains the 37.8 s
(headless) / 57.4 s (headed) first paint and the sparse DOM. Inputs carry no
human-written `name`, only generated record ids (`1788385170268x12065`), so **placeholder
text is the only stable field label** — hence `FIELD_HINTS`.

### ⛔ Three traps that each produced a confident, wrong result

1. **Loading spinners are visible controls.** First paint puts five
   `<a class="iconify svg-loading">` on the page. A gate waiting for "any visible
   control" fires on them and dumps a step whose only contents are placeholders, while
   the real button is seconds away. Readiness must mean *actionable* — carries text, or
   is a field you type into.
2. **`get_by_text(...).first` is not "the visible one".** The checkout leaves the
   previous screen's controls in the tree, hidden. `.first` resolved to a stale
   invisible `Continuar`, `is_visible()` said False, and the mapper concluded there was
   no way forward while a working button sat on screen. Scan matches; take the first
   actually visible.
3. **`preco_min` is the FEE-INCLUSIVE total.** The summary reads
   `Ingresso R$ 288,00 · Taxa de serviço (10%) R$ 28,80 · Valor total R$ 316,80`, and
   `matriz_preco` carries **316,80**. A price check reading "the first R$ on the page"
   takes the pre-fee 288,00 and reports a 28,80 move that never happened — aborting real
   purchases while looking like a working safeguard. Anchor to **`Valor total`**, and
   fail **closed** if that label is absent.

### Field mapping (all 9 matched by placeholder)

`Nome completo` · `Telefone celular` · `CPF/CNPJ` · `CEP (Código postal)` ·
`Estado (UF)` · `Bairro` · `Município` · `Endereço` · `N° do endereço` ·
`Complemento (opcional)` *(left empty)*

The site **auto-masks** plain digits — send digits only, unformatted:
`11987654321` → `(11) 9 8765-4321`, `00000000000` → `000.000.000-00`,
`01310100` → `01310-100`.

⛔ Example values above are deliberately fake. Real buyer data lives only in
`fields.json`, which is gitignored — never quote it into a document that ships.

## ✅ The Pix leg — VERIFIED 2026-09-02, and all three guesses were wrong

The first real purchase (order **#7707X57Q**, Gramado ‖ Outras Meias, R$ 275,00) closed
this gap and **overturned the premise of `_extract_pix` completely**.

It had been written blind against three plausible shapes — a readonly input, a textarea,
and page text matching a `0002…` EMV payload. **The code is in none of them.** It is not
in the DOM at all. The order screen renders the words *"Código Pix"* beside a
`Copiar código` button, and the payload exists only on the **clipboard** that button's
handler writes.

⭐ The working path, and the two traps around it:

| | |
|---|---|
| the button | `<button id="btn_copy">Copiar código</button>` — a stable Bubble id (its `class` carries a hash; ⛔ don't select on that) |
| trap 1 | A `<div class="greyout …">` overlays it. Playwright's `.click()` reports the button *"visible, enabled and stable"* and then times out after 30 s. `document.querySelector(sel).click()` via `evaluate` dispatches the handler directly and is not subject to pointer interception. |
| trap 2 | The context needs `clipboard-read`, or `navigator.clipboard.readText()` rejects and returns nothing — indistinguishable from "there was no code". Granted at context creation in `open_listing`. |

⚠️ **What failed on the night was the CAPTURE, not the reservation.** The order was
created correctly and the price guard passed exactly (`Valor total` R$ 275,00 ==
resolved R$ 275,00). `_extract_pix` ran while the page still showed an *"Aguarde…"*
spinner and found nothing, so the tool raised its loudest error over a purchase that had
in fact worked.

⛔ **And its recovery advice pointed nowhere useful:** it named the *checkout* URL, but
reopening that starts a FRESH checkout showing no order. Following it produced a
confident "no order exists" while #7707X57Q sat in Comprados counting down. Fixed — the
error now names `/ingressos` → **Comprados**.

Pinned as regression tests in `tests/test_listing.py`, including a fake page whose
`.click()` raises, so a return to the intercepted path fails loudly.

## Headless: ✅ verified, and faster

| | first paint |
|---|---|
| headless | **37.8 s** |
| headed | 57.4 s |

Identical rendering, identical auth signals (`entrar_links=0`), same URL. Headless is the
better default for an unattended buy.

## Market snapshot (2026-09-02 ~17:35 BRT, 04/09 night)

19 combos, all available. Cheapest overall **R$ 308,00**; cheapest *Gramado*
**R$ 308,00** (Meia PCD, qty 1), then Meia Professor R$ 319,00 (6), Meia Estudante
R$ 330,00 (96), Inteira R$ 341,00 (225).

⚠️ Prices rose since the 09-01 recon (Gramado Inteira R$ 286,00 → R$ 341,00), and
quantities moved *during* this session (Meia Estudante 97 → 96). The ceiling of
R$ 220,00 is far out of the money; nothing would arm today.
