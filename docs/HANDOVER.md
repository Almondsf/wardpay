# Handover: where the project stands and how to pick it up

For a new Claude Code session (or anyone) taking over WardPay. Read in this order:

1. `CLAUDE.md`: the rules of the project (loaded automatically).
2. **This file**: what's built since the original roadmap, the owner's decisions, what's next, and the
   quirks of this machine.
3. `docs/SYSTEM_OVERVIEW.md`: how everything works (the developer reference; kept current).
4. `OWNER_TODO.md`: everything waiting on the owner (accounts, keys, decisions).
5. `PLAN.md`: the original product plan and roadmap (chunks 0 to 10, all done).

Last updated: 2026-10-22, with the parent portal redesign (branch `feature/parent-redesign`, then `main`).

## Where things stand

The roadmap (PLAN.md chunks 0 to 10) is finished. Since then, at the owner's request:

| When | What | Where to look |
|---|---|---|
| 19 Oct | Payment plans (pay a bill in instalments) | `fees/plans.py` |
| 19 Oct | Extra fees for whole classes mid-term, pupil exemptions, a price list, one-off charges per pupil | `fees/charges.py` |
| 22 Oct | Product named **WardPay** (was ParentPay; shown name is one setting: `APP_NAME` / `VITE_APP_NAME`; code and repo use `wardpay`) | `core/config.py`, `frontend/src/lib/brand.ts` |
| 19 Oct | "School at a glance" (pupils, classes, records needing attention), now under the money figures on the dashboard | `students/summary.py`, `console/Dashboard.tsx` |
| 19 Oct | **Family payment link**: one link per parent, no sign-in, pay for all children with the amount chosen per child | `payments/family.py`, `family_links.py`, `public/FamilyPay.tsx` |
| 19 Oct | **Phone calls** for parents who don't read messages (English or Pidgin; overdue reminders and payment confirmations; Africa's Talking) | `notifications/voice.py` |
| 19 Oct | Pay for all children by bank transfer, card or USSD on the gateway's checkout, also from the portal ("Pay for all your children") | `payments/family.py`, `parent/ParentApp.tsx` |
| 20 Oct | **Fee status by pupil** download (every fee paid / part paid / unpaid, all classes or one; Excel with a sheet per class, PDF) | `reports/fee_status.py`, `reports/exports.py` |
| 20 Oct | Payment provider **switchable at runtime** (Platform → Providers); **Bachs** adapter built but not in use | `payments/providers.py`, `gateways/bachs.py` |
| 20 Oct | **Paystack chosen** as the provider; its sandbox works locally (real checkout page) | `CLAUDE.md`, `docs/DEPLOYMENT.md` |
| 20 Oct | **School admins can do everything a bursar can** (with two-factor sign-in); only the proprietor manages staff and the bank account | `accounts/permissions.py` |
| 20 Oct | **Fee breakdowns** wherever a balance shows (staff pupil page, portal, payment page) | `fee_status.fee_breakdowns`, `lib/FeeBreakdown.tsx` |
| 20 Oct | Fees added to the fee grid after billing can be added to the bills already sent (Fees page offers it) | `charges.sync_bills` |
| 20 Oct | **Pay for particular fees** (tick WAEC on the payment page or the cash form); earmarked money goes to those fees first, even before older debts | `payments/earmarks.py`, `PaymentEarmark` |
| 20 Oct | **Deployed**: API, worker, Postgres and Redis on Render (https://schoolpay-a97t.onrender.com), web app on Netlify (https://wardpay.netlify.app, renamed from parentpay.netlify.app on 22 Oct), which forwards `/api` to Render | `docs/DEPLOYMENT.md` ("On Render and Netlify"), `frontend/public/_redirects` |
| 21 Oct | Production logs leave out database query values; startup refuses a malformed Redis address and never echoes a rejected setting's value | `core/db.py`, `core/config.py` |
| 21 Oct | Fee status download per class: each class lists only its own fees, part-paid fees show paid and left, the PDF has a page per class | `reports/exports.py` (`fee_status_pdf`) |
| 21 Oct | **Redesign**: design tokens + Plus Jakarta Sans + lucide icons, a fixed sidebar (Setup always open), a one-screen dashboard with a tabbed panel, the pupil page as a profile header with tabs; shared `Tabs`, `Stat`, `Menu`, `BackLink` in `lib/ui.tsx` | `frontend/src/styles.css`, `console/ConsoleApp.tsx` |
| 21 Oct | **Classes from the usual list**: Creche to SSS 3 ticked for a new school, untick/add own, one batch; missing ones slot into the promotion order later | `schools/academics_service.py` (`STANDARD_CLASSES`, `add_class_levels`), `console/setup/ClassSetup.tsx` |
| 21 Oct | **School year filled in** (usual Nigerian term dates from the Lagos date, today's term made current) | `schools/academics_service.py` (`suggest_session`), `console/setup/Calendar.tsx` |
| 21 Oct | **Printable pay cards** (one pupil or a class, four to an A4 page: account number + this term's fees with what's left, total to pay; no QR code); Lagos time in one place (`core/clock.py`) | `payments/paycards.py`, `console/payments/PayCards.tsx` |
| 21 Oct | **Tuition first** for money not marked for a fee; **mark a payment for fees afterwards** (once, audited) from the Payments page | `reports/fee_status.py` (`_fill`), `payments/earmarks.py` (`mark_payment`) |
| 22 Oct | **Parent portal redesign**: one page (total at the top, each child with owed, Pay online, account number and fees; pay-all at the bottom), history and settings a tap away; demo code-fill button on laptops | `parent/ParentApp.tsx`, `parent/ParentLogin.tsx` |
| 22 Oct | **Fees in reminders**: `{fees}` in new-bill notices and reminders (fees with money left, biggest first, max four then "N more"); WhatsApp templates get it as their last parameter | `notifications/service.py` (`fees_phrase`), `templates.py` |
| 22 Oct | **SMS alphabet**: every text is converted to GSM-7 (NGN for ₦, plain quotes/dashes, emoji dropped) so it isn't billed as Unicode (7 pages became 3); wording preview shows SMS pages | `notifications/sms.py` (`to_gsm`, `sms_pages`) |
| 26 Oct | **Load test, 10,000 pupils** (`python -m app.loadtest`): books add up; fixed the outbox holding two connections under bursts, per-pupil queries summing the whole school (500 → ~100 ms a payment), the session pack crashing over 5,000 debtors, billing timing out (now a background billing run), stale statistics after bulk imports; webhook limit 3,000/min. Hosting sizes in DEPLOYMENT.md | `app/loadtest.py`, `fees/billing_runs.py`, `core/outbox.py` |
| 25 Oct | **WardPay's standard plan charge** (owner): ₦300 per payment + ₦200 per plan, a floor schools can only raise; both on the pricing plan. Fixed: saving a pricing plan with a taken name while making it the default gave a server error | `plan_offers.rates`, `platform/plans.py` |
| 25 Oct | **Staff put families on school plans** (owner: most parents won't use the website): pupil's page → Instalment plan beside the parent applies it to all their children and messages them | `plan_offers.apply_for_family`, `console/students/FamilyPlan.tsx` |
| 25 Oct | **School instalment plans**: schools set up their own plans, parents choose them on the link or in the portal (a plan charge goes on the bill), staff can apply them; WardPay charges the school ₦200 per plan made (per pricing plan) on its next bill | `fees/plan_offers.py`, `console/fees/PlanTemplates.tsx`, `public/PlanChoices.tsx` |
| 24 Oct | **Background exports**: the journal and end-of-session pack are built by the worker (no browser timeouts for big schools), kept a day, downloadable only by whoever asked | `accounting/export_jobs.py`, `console/reports/useBackgroundExport.ts` |
| 24 Oct | **Live smoke check** (`python -m app.smoke <site>`): 13 read-only checks of a deployment; all pass on the live site. A real-payment checklist is in OWNER_TODO.md | `app/smoke.py` |
| 24 Oct | **Unusual activity** (proprietor): reversals, late/after-close cash, short counts, bill cuts and voids, write-offs, rejected banking, duplicate spending | `reports/signals.py`, `console/reports/Unusual.tsx` |
| 24 Oct | **Writing off debt**: staff ask, only the proprietor approves (owner's rule); a write_off line on the bill, Dr bad debts in the journal | `fees/writeoffs.py`, `console/payments/WriteOffs.tsx` |
| 24 Oct | **Paid into the bank** (Paystack settlement reconciliation): daily fetch of payouts, matched to payments by reference; unsettled and unknown listed | `payments/settlements.py`, `console/payments/Payouts.tsx` |
| 24 Oct | **Closing the books**: close an ended term so nothing is backdated into it; the proprietor re-opens with a reason (`books.reopen`) | `accounting/periods.py` |
| 24 Oct | **End-of-session pack** (Reports → For your accountant): one workbook per school year | `accounting/session_pack.py` |
| 24 Oct | **General journal for the accountant** (Reports → For your accountant): balanced double entries for any period as Excel or CSV, the school's own account codes | `accounting/journal.py`, `console/reports/Accountant.tsx` |
| 24 Oct | **Collection curve and cash forecast**: the dashboard's "Pace and forecast" tab compares this term with last at the same day and shows what falls due in 30/60/90 days (plans count only their instalments) | `reports/insights.py`, `console/Dashboard.tsx` |
| 22 Oct | **Set-up wizard** (`/console/get-started`): details, school year, classes, fees once per section, pupils, bills; from the dashboard's getting-started card | `console/GetStarted.tsx` |
| 22 Oct | **Billing and Payments decluttered**: term picker in the header, one strip of figures, lighter lists; a count on Awaiting approval; payment actions in a ⋯ menu | `console/billing/Billing.tsx`, `console/payments/Payments.tsx` |
| 22 Oct | **Suggested admission numbers** when adding a pupil (latest pattern's highest + 1; initials/year for a new school; editable) | `students/service.py` (`next_admission_number`) |
| 21 Oct | Real `favicon.ico` + PNG icons (Apple touch, 192/512 in `site.webmanifest`): Netlify's app fallback was answering `/favicon.ico` with the page, so Safari and some tabs showed no icon | `frontend/public/` |

Also made: a pitch deck for school proprietors (a claude.ai artifact, private to the owner:
https://claude.ai/artifact/U8F5XpU55wtrACtihHp2oH; the dashboard figures and contact details are
placeholders), the Word overview `docs/product/WardPay - what it does.docx` and the user guide `docs/product/WardPay - user guide.docx`.

## How the owner wants work done

These are standing rules from the owner. CLAUDE.md has the project rules; these sit on top.

- **Branch, CI, merge.** Branch off `main`, commit, push, wait for **both** GitHub Actions workflows
  (Backend, Frontend) to pass, then fast-forward `main` and delete the branch. One commit per piece of
  work; describe the feature, never the word "chunk"; **no AI attribution** of any kind. The owner
  sometimes merges a PR on GitHub himself: fetch first and build on what's there.
- **Never a database call inside a loop.** Batch with `IN (...)`, joins, bulk inserts, or one locked
  `SELECT … FOR UPDATE`. Services prove it with "fixed number of statements" tests (`QueryCounter`).
- **Docs are part of every change**: `docs/SYSTEM_OVERVIEW.md`, `docs/USER_STORIES.md`, and
  `SECURITY.md` / `PRIVACY.md` / `DEPLOYMENT.md` / `.env.example` when relevant.
- **Word documents** (two; owner wants both always current): when a feature ships or changes, update
  `docs/product/overview.cjs` (rows, the dated CHANGES line, UPDATED) and `docs/product/guide.cjs` (the
  user guide: the feature's who / what it's for / how to use it with the real button names, a CHANGES
  line, UPDATED), then rebuild both with `cd docs/product && npm run build`. If the owner has the .docx open
  the build fails with EBUSY: ask them to close it. To look at the layout, export to PDF through Word
  (PowerShell `Word.Application`, SaveAs format 17) and stop any **windowless** WINWORD process
  afterwards (never one with a window: that's the owner's).
- **`OWNER_TODO.md`**: anything only the owner can do goes there in the same piece of work, marked
  _(new)_, and is mentioned in the summary.
- **Tests**: run only the new and affected test files, never the full suites (slow on this machine).
  Check frontend tests under Node 22 as CI does:
  `npx -y -p node@22 node node_modules/vitest/vitest.mjs run <paths>`. Run the affected Playwright
  specs with `PW_CHANNEL=msedge npx playwright test <names>`.
- **Frontend formatting**: there is no Prettier config. Never run bare `npx prettier --write`. Match the
  existing style (single quotes, 2 spaces, lines up to ~140).
- **Decisions that change security or money rules**: ask the owner first (a short multiple-choice
  question works well), and record the answer in the code comment and the docs.
- **Talking to the owner**: plain language, what changed and what they can now do, what's waiting on
  them. They're not a developer and often test in the browser themselves.
- **Comments**: the owner asked for unnecessary comments to be removed, then paused it (see "Next").
  Meanwhile don't add new `# --- Section ---` divider lines; keep comments to the non-obvious why.

## Owner decisions (with dates)

- 2026-10-18 Pricing model: set-up fee + yearly licence + per pupil per term, free for parents (prices in
  the platform plan are defaults: ₦50,000 / ₦100,000 / ₦500).
- 2026-10-23 How one family amount is shared between children is a per-school setting (default "old debts
  first, then clear as many children as possible"). Built for the family payment link (one tap pays
  everything; a smaller amount follows the rule); family account numbers are on hold (owner: "stick
  with the payment link for now").
- 2026-10-22 Platform admins may send a school's staff their invite / a reset link and reset their two-factor
  sign-in (for locked-out proprietors); not change their email or password.
- 2026-10-22 Pricing: a set-up fee and a pupil fee each term; **no yearly licence**. 2026-10-23: the licence
  is removed from the product entirely (plans, billing, screens). The website explains the fees without figures.
- 2026-10-22 Home page redesigned (ideas from schoolpaynigeria.com and parentpaygroup.com): photos of
  Black pupils, product mock-ups, short copy, "Built for Nigerian schools" (not "private").
- 2026-10-19 Product name "ParentPay" for now (trademark to be checked: it's a UK company's brand).
- 2026-10-22 Product renamed **WardPay** everywhere: the app, the docs, the code (`wardpay`) and the GitHub
  repository. Two fixed hashing labels in the code still say `schoolpay` (`core/crypto.py` blind index,
  `parents/auth_service.py` sign-in codes): changing them would break every stored phone/email lookup.
- 2026-10-19 Email through Zoho Mail; WhatsApp and voice via Zoho if it can (otherwise Termii / Africa's Talking).
- 2026-10-19 A family payment can be split however the parent likes; nothing is split automatically.
- 2026-10-20 Parents keep signing in with a texted code (the owner considered dropping it; kept for
  children's data protection).
- 2026-10-20 Paystack is the payment provider; Bachs stays built but unconfigured (it can't give each
  pupil an account number).
- 2026-10-20 School admins get everything a bursar has, with two-factor sign-in.
- 2026-10-20 Money a parent marks for a fee goes to that fee even if older debts are owed.
- 2026-10-21 Pay cards list the term's fees and what's left to pay; no QR code.
- 2026-10-21 A fee added to a class in the fee grid goes onto every bill in that class already sent, as soon as it's saved (no confirmation).
- 2026-10-21 Money not marked for particular fees covers tuition first, then the bill's other fees in order.
- 2026-10-22 Text messages write NGN for the naira sign (not N).
- 2026-10-22 The parent portal stays one page for paying (no tapping into each child), for parents who aren't smartphone-savvy.

## Next: what's open, roughly in order

**On hold (owner, 2026-10-22): announcements and messaging costs.** Designed but not built until the
owner decides: (1) who pays for announcement messages (prepaid credits per school, or an allowance in
each plan); (2) the price per SMS page; (3) whether urgent announcements reach parents who turned off
reminders. The plan: write, choose who (school/classes/owing/pupils, one per family), SMS by default
(cheaper than WhatsApp marketing at about ₦77 a message), 160-character counter and cost estimate before
sending, a free copy in the parent portal, delivery report. Also discussed: optional browser push notifications (an idea from diplace); the SMS-provider
switch from the same talk is built (22 Oct). Costs research: Termii
about ₦6–9 a page, WhatsApp utility about $0.0067 and marketing about $0.0516 a message.

**Ideas waiting on the owner (2026-10-22), not started:**

- **School groups with several branches.** Today each branch would sign up as its own school: a staff
  account belongs to one school and an email is unique, so a proprietor needs one email per branch;
  there's no combined view; a parent with children in two branches picks a branch at sign-in; each
  branch is billed separately. Proposed: keep each branch a full school (own bills, receipt numbers,
  fees, staff, bank account; tenant isolation unchanged) and add a **group** above them. Group staff
  (proprietor, head-office bursar) sign in once, switch branches and see combined dashboard/reports;
  branch staff see only their branch; parents see all their children across the group on one page; the
  platform can bill the group as one customer. Stages: (1) groups, group staff, branch switcher,
  combined reports; (2) parent portal across branches; (3) group billing. One payment covering two
  branches only works if they share a bank account (a Paystack split goes to one subaccount). The
  alternative, branches as a campus label inside one school, was rejected: it fits only groups run as
  one school, and limiting staff to a campus would need a weaker second isolation layer. **Open
  questions** (in `OWNER_TODO.md`): shared or separate bank accounts per branch; can one bursar cover
  several branches; a group price. This changes who sees which school's data, so confirm before
  building.
- **Giving a school all its records** (for leaving WardPay or a wind-down; on hold, owner 2026-10-24:
  "leave it till later"). Discussed: the owner first wanted a platform button sending each school (or all)
  one password-protected file by email. Recommended instead, and agreed as the shape when it's built: a
  proprietor-only "Download all our records" (one Excel file with everything, MFA sign-in, audited, nothing
  emailed), with closed schools keeping read-only sign-in for about 90 days; the owner emails proprietors
  to download. Emailing everything was set aside: it puts every parent's phone in a mailbox the school
  controls (and the school can change that address), and big files exceed attachment limits. If an
  emailed file is ever wanted: one encrypted .xlsx (msoffcrypto-tool, AES), built per school by an outbox
  job, password by SMS to the school's phone. Ties in with the open "close a school" decision.
- **Demo reset button: dropped** (owner, 2026-10-25: "leave this out"). If it comes back: never seed the demo
  on the live site as it is (known passwords incl. a platform admin, real texts to made-up numbers, real
  Paystack calls). The safe options were a separate demo deployment (fake gateway, its own free DB) or a
  locked-down `is_demo` school with random credentials.
- **No second, cheaper virtual-account provider** (owner, 2026-10-25): Fincra, Figo and the like need
  BVN verification for each virtual account, which WardPay doesn't want to collect. Stay on Paystack's
  Dedicated NUBAN for pupils' account numbers; don't propose these providers again.
- **One virtual account per family** (on hold, owner 2026-10-23; the split rule below is built and used by
  the family payment link, so family numbers would reuse it). Needs a provider that issues virtual
  accounts (Paystack Dedicated NUBAN; the cheaper providers were ruled out, see above). A plain transfer says nothing about which child it's for, so it's
  split by a **rule each school chooses** (owner, 2026-10-23), a school setting:
  - **old debts first, then clear as many children as possible** (the default): earlier terms oldest
    first across the children, then this term smallest balance first, tuition first within a child;
  - in proportion to what each child owes;
  - eldest child first, or youngest first;
  - hold it as family credit until the bursar shares it out.
  In every case anything left over becomes family credit, the parent gets one text saying how it was
  split, a child's name in the transfer narration overrides the rule, and the bursar can re-mark it.
  The family payment link (parent chooses the amounts) is unchanged and needs no rule.

0. **Bookkeeping roadmap** (owner, 2026-10-24: "make the school ledger really great"), in order:
   ~~cash book and banking~~ (done 24 Oct: `payments/cashbook.py`); ~~expenses and income & expenditure~~ (done
   24 Oct: `expenses/`; receipt photos not stored yet, no file storage on the free plan);
   ~~collection curve and cash forecast~~ (done 24 Oct: `reports/insights.py`); ~~accountant export~~ (done 24 Oct: `accounting/`;
   built in the background since 24 Oct, `accounting/export_jobs.py`); ~~end-of-session pack~~ (done 24 Oct: `accounting/session_pack.py`); ~~closing a term~~ (done 24 Oct:
   `accounting/periods.py`); ~~Paystack settlement reconciliation~~ (done 24 Oct:
   `payments/settlements.py`; field names to confirm on the first live payout); ~~write-offs~~ (done 24 Oct: `fees/writeoffs.py`); ~~unusual-activity signals~~ (done 24 Oct:
   `reports/signals.py`). The bookkeeping roadmap is complete.
1. ~~Card payment of school subscriptions~~ (done 22 Oct: `platform/card_payments.py`).
   ~~SMS provider switch~~ (done 22 Oct: Termii or Africa's Talking, `notifications/providers.py`).
   ~~Owner tools~~ (done 22–23 Oct): Problems page, helping locked-out staff, 12-month trends, bill discounts
   and due dates (`platform/problems.py`, `staff_support.py`, `trends.py`, `billing.add_discount`).
   Bachs re-checked 23 Oct: it does have virtual accounts, but **one per account** (so one per school as a
   connected account, not one per pupil); a deposit carries the sender's account number and narration and
   must be matched by us. Fees: deposit 1% capped at ₦300, card 2%, checkout transfer 1.5% capped at ₦2,000.
2. **Parent consent in the portal** (step 4): a parent confirms consent themselves on first sign-in
   (`ConsentMethod.PORTAL` already exists).
3. **The "later" list in PLAN.md** (step 5): only when the owner says so.
4. From the low-literacy discussion, still open: a short "number-first" text style for messages
   (the printable pay card is done).
5. **Comment cleanup**: the owner asked to remove unnecessary comments and trim long docstrings
   (choice: "noise + trim docstrings"), then said "abort" after section dividers were removed. That
   pass is saved in `git stash` ("comment cleanup pass 1 … aborted 2026-10-19"); it predates a lot of
   later work, so redo it fresh rather than applying the stash. **Ask before resuming.**
6. Yoruba, Hausa and Igbo voice calls need recorded voices (owner to arrange).

Waiting on the owner (details in `OWNER_TODO.md`): Paystack "Dedicated NUBAN" approval (per-pupil
account numbers; until then parents pay through checkout), live keys, WhatsApp/SMS/voice accounts,
actual prices, legal documents, a pilot school.

## Running it on this machine

The scratch Postgres is on **5433** (`pg_ctl -D /c/Users/HP/.schoolpay-scratch/pgdata -o "-p 5433" …`
start; see CLAUDE.md). API on **8002**, Vite on **5175**. Never use the owner's own database on 5432.

```bash
cd backend && .venv/Scripts/python -m app.core.migrate && .venv/Scripts/uvicorn app.main:app --port 8002 --reload
cd frontend && npx vite --port 5175 --strictPort
cd backend && .venv/Scripts/python -m app.cli seed-demo --reset     # rebuild the demo school (scratch DB only)
```

Local `backend/.env` (never committed) has, beyond `.env.example`: `PAYMENT_GATEWAYS=paystack,fake`, the
owner's **Paystack test key** (`PAYSTACK_SECRET_KEY=sk_test_…`), `ENFORCE_MFA=false` (so the school admin
signs in without a code locally; production refuses it) and `SMS_BACKEND=memory`.

**Demo accounts** (password `demo-password-123`, authenticator setup key `JBSWY3DPEHPK3PXP`):
`admin@demo.wardpay.ng` (Ada Obi, school admin; no code locally), `bursar@…` and `proprietor@…`
(code required), `platform@…` (platform admin). Demo parent: phone **0803 500 0000**; on the code step,
**"Fill in the code (demo on this computer)"** reads the texted code from the local inbox (the owner demos
this way until Termii is set up; the button exists only in the Vite dev server; local `.env` sets
`OTP_RATE_PER_PHONE=30/10minutes` so repeated demo sign-ins aren't blocked), or open
`http://localhost:8002/api/v1/dev/sms/latest?to=08035000000`. Paystack test card:
4084 0840 8408 4081, any future expiry, CVV 408, PIN 0000, OTP 123456.

**After every `seed-demo --reset`**, reconnect the demo school to the Paystack sandbox. Paystack's test
mode allows only 3 bank lookups a day, so link the test subaccount already created (scratch DB only):

```python
# backend/.venv/Scripts/python - <<'EOF'
import uuid
from sqlalchemy import select
from app.core.db import SessionLocal
from app.core.tenancy import scope_platform_wide, scope_to_school
from app.demo import DEMO_SLUG
from app.modules.schools.models import School
from app.modules.payments.models import SchoolPaymentAccount
from app.modules.payments.gateways import GatewayCode
with SessionLocal() as s:
    scope_platform_wide(s); school_id = s.scalar(select(School.id).where(School.slug == DEMO_SLUG))
with SessionLocal() as s:
    scope_to_school(s, school_id)
    s.add(SchoolPaymentAccount(id=uuid.uuid4(), gateway=GatewayCode.PAYSTACK, subaccount_code="ACCT_jrloeq5ijkp1lqc",
        bank_code="057", bank_name="Zenith Bank", account_number="0000000000", account_number_last4="0000",
        account_name="Test", platform_fee_bp=100))
    s.commit()
# EOF
```

## Quirks learned the hard way

- **Restart the servers after code changes.** Uvicorn's `--reload` and Vite's hot reload miss changes on
  this Windows machine fairly often. A stopped Vite can leave an orphan `node …vite.js` holding port 5175:
  check its command line (`Get-CimInstance Win32_Process`) before stopping it.
- **CI path filters**: the Backend workflow also runs when `frontend/openapi.json` changes, and both can be
  started by hand. After any API change, regenerate the types
  (`python -m app.openapi > ../frontend/openapi.json && npm run gen:api`) or the Backend job fails.
  `openapi.json` shows as different locally only because of CRLF line endings.
- **Playwright selectors**: match menu links and headings with `exact: true`; the dashboard grows and
  substrings collide ("Students" vs "60 students owe", "By class" vs "Pupils by class").
- **Tests run inside one transaction**, so `now()` is the same for every row: order by something else
  (receipt numbers, ids) when a test needs a stable order.
- **Docker Desktop doesn't work here**; Dockerfiles are checked by CI only.
- **Bare `python`** on PATH belongs to another project; use `py -3.12` or `backend/.venv/Scripts/python`.
- **Shell heredocs with apostrophes** sometimes break in this Git Bash; write edit scripts to a file in
  the scratchpad and run them.
- **Paystack test mode**: subaccounts work with Zenith (057) + `0000000000`; "Dedicated NUBAN" isn't
  enabled on the owner's business yet; checkout pages sit behind a Cloudflare check that blocks automated
  browsers (fine for people).
