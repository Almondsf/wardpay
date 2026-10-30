# Owner to-do list

Things only you (Oluwafemi) can do: decisions, accounts, paperwork and people.
The build carries on without them, but each blocks something real. Kept up to
date as the build goes on; newest additions are marked _(new)_.

Last updated: 2026-10-24 (one real payment, end to end)

## _(new)_ WardPay's 1% is gone (2026-10-27)

- [ ] After the next deploy, run **`python -m app.cli clear-platform-fee`** once on the server (Render: the
      API service's Shell tab). It tells Paystack to give WardPay 0% on every school's subaccount made
      before the change, and prints how many it did. Safe to run again. New schools are already at 0%.
- [ ] You can delete the `PLATFORM_FEE_BP` variable from Render; it isn't used any more.

## _(new)_ Hosting for real schools (2026-10-26, from the 10,000-pupil load test)

- [ ] Before the first school relies on WardPay: a **paid Postgres** (the free one is deleted after 30 days,
      around 19 Nov), an **API that doesn't sleep**, and a **paid background worker** started with
      `--concurrency 4` (about 1 GB of memory). That records ~6 payments a second: 10,000 arriving at once
      clear in under half an hour, and nothing is lost meanwhile. Details: DEPLOYMENT.md, "Sizing for big
      schools". Today's free setup does ~1.7 a second, and only while awake.

## _(new)_ One real payment, end to end (2026-10-24)

The automatic check of the live site passes (13 of 13: `python -m app.smoke https://wardpay.netlify.app`,
read-only, run it after each deploy). What it can't do is pay. Do this once, in this order, and note
anything that doesn't happen as described:

- [ ] **Paystack webhook address.** Paystack dashboard → Settings → API Keys & Webhooks → Webhook URL:
      `https://wardpay.netlify.app/api/v1/webhooks/paystack` (for whichever mode, test or live, the
      site's `PAYSTACK_SECRET_KEY` is in). Save.
- [ ] **Open the site first** (the free plan sleeps; a sleeping site answers Paystack late, though
      Paystack retries).
- [ ] **A pupil to pay for.** Sign in as the proprietor. Use a test pupil in a test school (or the demo
      school), with **your own phone number** as the parent, and a bill on it.
- [ ] **Pay by card.** Pupil's page → Payments → **Card payment link** → ₦100 (or a small amount) →
      **Create link**. Open the link on your phone and pay. In test mode use Paystack's test card
      4084 0840 8408 4081, any future expiry, CVV 408, PIN 0000, OTP 123456.
- [ ] **What you should see, within a minute:**
      1. the payment page says it went through;
      2. Payments → Received shows it, with a receipt number, and the bill's balance went down;
      3. a receipt message on your phone (WhatsApp or text, if those are set up; else nothing yet);
      4. the receipt PDF opens, and its QR code opens the check page showing "valid".
- [ ] **The family link.** On the same pupil, **Payment link** → **Copy link**, open it on your phone,
      and pay a small amount: the same four things should happen.
- [ ] **The next morning** (live mode only: test mode never pays out): Payments → **Paid into the
      bank** → **Check now**, and compare with the Paystack dashboard (see the next section).
- [ ] **Tell me** what happened at each step, especially anything late or missing.

## _(new)_ Check the first real Paystack payout (2026-10-24)

- [ ] When the first school receives a real payout, open Payments → **Paid into the bank** and compare
      it with that day's settlement on the Paystack dashboard: "Parents paid", "Charges" and "Into the
      bank" should match. Test mode has no payouts, so the field names came from Paystack's
      documentation; tell me if any figure differs and I'll adjust `paystack.list_settlements`.

## _(new)_ The live site (2026-10-22)

- [ ] **Change the database password on Render, now.** It was pasted into a chat. Render →
      Postgres → reset the password, then put the new **Internal** URL in the API's
      `DATABASE_URL` (and your local copy of the external URL, if you keep one).
- [ ] **Change the Redis Cloud password** for the same reason (Redis Cloud → database →
      Security), then update `REDIS_URL` and `RATE_LIMIT_STORAGE_URL` on Render.
- [ ] **Render's free Postgres is deleted 30 days after it was made** (made about 20 Oct, so
      around **19 Nov 2026**). Before then, or before any school relies on the site, move to
      the paid database and API plans and a separate background worker (`docs/DEPLOYMENT.md`).
      On the free plan the API sleeps after 15 idle minutes, so reminders pause.
- [x] The API runs migrations, the worker and the API itself from one Docker Command:
      `sh bin/start-all-in-one.sh` (2026-10-22).
- [ ] **Demos:** until Termii is live, demo from your laptop (parent sign-in has a "Fill in
      the code" button there). See `docs/HANDOVER.md` → Running it on this machine.

## Payment providers (2026-10-20)

- [ ] **Paystack "Dedicated NUBAN"** (each pupil's own account number) is not enabled on your
      Paystack business yet: email support@paystack.com to request it. Until then, parents pay
      by card or bank transfer on Paystack's checkout page.
      _(new)_ When asking, also ask them to **raise the 1,000-account limit**: Paystack allows 1,000
      virtual accounts per business by default, and every school's pupils count against the
      platform's one Paystack business. Ask too what the fee per transfer into these accounts is
      (their help pages say both 1% capped at ₦300 and 1.5% + ₦100) and whether parents need
      identity checks.
      _(new, 2026-10-27)_ Ask them too whether **schools may add the checkout charge for parents**
      (schools can now choose that under Payment settings; Paystack's own help page says charges can be
      passed to customers, but confirm it for schools before telling a school to switch it on).
- [ ] _(new, 2026-10-30)_ **Paystack answered** (30 Oct): "the details of parents or guardians can be used to
      create these DVAs". Unclear whether that means the parent's **BVN/NIN**. Reply asking: is the parent's BVN
      (or NIN) required, or are name, phone and email enough; can one parent's details back several accounts
      (one per child); what name shows to the payer (the child's or the parent's); the fee per transfer; and
      the 1,000-account limit. If BVN is required, decide whether WardPay asks parents for it (see HANDOVER).
      _(new, 2026-10-30)_ Owner: parents would give their **NIN** but are wary of BVN. Ask Paystack whether
      the parent's **NIN alone** is accepted for these accounts (their API docs show only BVN + bank account).
- [x] **Provider decided: Paystack** (2026-10-20). Bachs is built in and can be switched on
      later from Platform → Providers (it needs its keys), but isn't configured. Reason: Bachs
      can't give each pupil their own account number.
- [ ] **Before going live:** generate fresh Paystack keys (the test key was shared in a chat)
      and set the live key and webhook URL as in `docs/DEPLOYMENT.md`.

## Decisions

- [x] _(new)_ **School groups with several branches** (decided 2026-10-27: the group chooses shared or
      separate bank accounts; owner + central bursar work across branches; each branch billed as now;
      head office may see each branch's data). Step 1 is built: set a group up under Platform → School
      groups. When a real group signs on, tell us whether its branches share one bank account.
- [x] _(new)_ **How one family payment is shared between children** (decided and built 2026-10-23):
      each school's proprietor chooses the rule under Payment settings (default: old debts first,
      then clear as many children as possible). It's used by the family payment link: one tap pays
      everything, a smaller amount follows the rule. Family account numbers are on hold.

- [ ] _(new, on hold)_ **Announcements to parents** (2026-10-22): decide who pays for the messages
      (prepaid credits per school, or an allowance in each plan), your price per SMS page, and whether
      urgent announcements reach parents who turned off reminders. Talk to a few schools first. Ask
      Termii for their current rate card (generic and DND routes).

- [ ] _(new)_ **Set the actual prices.** The model: a **set-up fee** and a **pupil fee each
      term** (plus the ₦200 per instalment plan), free for parents. In the platform console (Plans),
      put in your real set-up and per-pupil amounts.
      The website shows no figures, only what the two fees are.
- [ ] _(new)_ **Try paying a bill by card** (2026-10-22): sign in as a school (e.g. the demo
      school's proprietor), Subscription → **Pay by card**, and pay on Paystack's test page with
      their test card (4084 0840 8408 4081, any future date, CVV 408, PIN 0000, OTP 123456). The
      bill should show Paid when you come back. Paystack's page has a bot check, so this one needs a person.
- [ ] _(new)_ **Home page photos** (2026-10-22): four free stock photos (Ben White on
      Unsplash; Mickael Ange Konan, Roman Odintsov and Emmanuel Abiodun on Pexels), credited
      in the footer. The three iStock previews you found are paid: buy them before using them.
      Best of all later: photos from a pilot school, with the parents' written consent.
- [ ] _(new)_ **Product renamed to WardPay** (2026-10-22). Still to do:
      - check nobody else trades as WardPay (a quick search and the Nigerian trademark register),
        and get the domain (e.g. wardpay.ng / wardpay.com.ng);
      - _(new)_ on **Render**, set `APP_NAME=WardPay` (the website header and the names in emails,
        texts and receipts come from it) and `MFA_ISSUER=WardPay`; set `FRONTEND_URL` to
        `https://wardpay.netlify.app` and `CORS_ORIGINS` to the same address (links in emails and
        texts are built from it); `EMAIL_FROM` / `TERMII_SENDER_ID` when those are set up; then
        Manual Deploy;
      - on **Netlify**, set `VITE_APP_NAME=WardPay` if it's set there. The site is renamed to
        wardpay.netlify.app (done 22 Oct); receipts printed before that carry a QR code to the
        old address, which no longer works;
      - the Render service keeps its address (schoolpay-a97t.onrender.com) unless you make a new
        service; nobody sees it, Netlify forwards to it.

## Accounts to open

| Account | What to do | Blocks |
|---|---|---|
| **Paystack** (business) | Open the account, get test keys, then ask Paystack what approval/KYC **Dedicated Virtual Accounts** need for a platform with subaccounts (PLAN.md §3.2). Send me the answer. | Per-student account numbers on real money |
| **Flutterwave** | Optional second gateway. Permanent virtual accounts need each customer's BVN, so we use it for card payments only unless they offer another route. | Card payments via Flutterwave |
| **Meta WhatsApp Business** | _(new)_ You plan to use **Zoho** for WhatsApp: Zoho Voice can send WhatsApp messages (approved templates) for you, but **not WhatsApp calls**, and Meta reportedly doesn't allow business-started WhatsApp calls in Nigeria. Either way: create a WhatsApp Business account and phone number in Meta Business Manager, a permanent access token, and submit the three message templates in `docs/DEPLOYMENT.md` ("Messages to parents") for approval. _(new)_ The bill and reminder templates' last two parameters are now the parent's family payment link and what the money is for (e.g. ": Tuition ₦130,000, PTA levy ₦5,000"); see `docs/DEPLOYMENT.md`. | WhatsApp receipts and reminders (SMS is used until then) |
| **Termii** (SMS) | _(new)_ **Account opened** (2026-10-22, ₦30 balance; KYC documents still to come). Still to do: request a **Sender ID** (up to 11 characters, e.g. `WardPay`; Termii approves it), ask Termii to **switch on the DND route** (sign-in codes must reach numbers on Do-Not-Disturb), **top up**, then on Render set `SMS_BACKEND=termii`, `TERMII_API_KEY` and `TERMII_SENDER_ID` (exactly as approved). | Parent sign-in codes and SMS messages on a live system |
| _(new)_ **Africa's Talking texts** (optional second SMS provider) | **Built** (2026-10-22): texts can go through Africa's Talking instead of Termii, switched in the console (Platform → Providers). It's the same account as the voice calls. To use it: request a **sender ID** from Africa's Talking for Nigeria, then on Render set `AFRICASTALKING_USERNAME`, `AFRICASTALKING_API_KEY` and `AFRICASTALKING_SMS_SENDER`. Compare their price per text with Termii's. | A backup if Termii is down or dearer |
| **Email sending** | _(new)_ **Zoho Mail chosen** (2026-10-19). Create the mailbox on your domain, then set `EMAIL_BACKEND=smtp`, `SMTP_HOST=smtp.zoho.com` (or `smtppro.zoho.com` for a paid domain plan), `SMTP_PORT=587`, `SMTP_USERNAME`, `SMTP_PASSWORD` (an app password) and `EMAIL_FROM`. Nothing to build. | Staff invites and password resets on a live system |
| _(new)_ **Voice calls** | **Built** with Africa's Talking (calls in English and Pidgin, read by text-to-speech). To go live: an Africa's Talking account and voice number, the env settings and callback URL in `docs/DEPLOYMENT.md`. If you'd rather use Zoho Voice, confirm its API can call Nigerian numbers and read out text, and it can be added as another provider. **Decide:** who pays for calls (included in the per-pupil fee, or charged to the school per call?); whether to add Yoruba, Hausa and Igbo, which need voices recorded by native speakers (text-to-speech in those languages isn't good enough yet). Listen to a test call in Pidgin before launch: it's read by an English voice. | Phone calls to parents |
| _(new)_ **Platform bank account** | A business account for WardPay itself (not a school's). Set `PLATFORM_BANK_NAME`, `PLATFORM_ACCOUNT_NUMBER` and `PLATFORM_ACCOUNT_NAME` so subscription invoices tell schools where to pay. | Schools paying their subscription |
| _(new)_ **Backups** | Off-site storage (any S3-compatible bucket or similar) and an [age](https://age-encryption.org) key pair. The private key and the `FIELD_ENCRYPTION_KEYS` are kept by you, off the server, apart from each other. See `docs/DEPLOYMENT.md` → Backups. | Recovering from a lost server |
| _(new)_ **Error monitoring** (optional) | A Sentry project; set `SENTRY_DSN`. | Hearing about errors before schools do |
| _(new)_ **Support email** | A mailbox schools and parents can write to; set `SUPPORT_EMAIL` (shown on the website and privacy notice). | Parents' data requests |
| **Hosting and domain** | _(new)_ Hosting chosen: the API, worker, database and Redis on **Render**, the web app on **Netlify** (https://wardpay.netlify.app, renamed from parentpay.netlify.app on 22 Oct); setup in `docs/DEPLOYMENT.md`. Still to do: buy a domain and add it on Netlify, then update `FRONTEND_URL` and `CORS_ORIGINS` on Render. | Going live |

## Validate with schools (PLAN.md §7)

- [ ] Talk to about 10 bursars and proprietors: how they collect fees today,
      what it costs them, what they'd pay.
- [ ] Find one **pilot school** willing to run a term on it.
- [ ] Get anonymised sample fee schedules and student spreadsheets.

## Legal and compliance (NDPA 2023)

- [ ] Register with the NDPC as a data processor (children's data).
- [ ] A Data Processing Agreement template for schools to sign.
- [ ] Privacy policy and terms of service for the website and parent portal. _(new)_ The sign-up page asks schools to accept the terms, so these are needed before sign-up goes public (or set `SIGNUP_ENABLED=false` until then).
- [ ] _(new)_ Decide how long schools may keep downloaded debtor lists (they
      contain parents' phone numbers) and say so in the school agreement.

## Before going live

- [ ] An independent penetration test of the deployed system: give the tester `docs/PENTEST_CHECKLIST.md`.
- [ ] _(new)_ Do one practice restore from a backup (`deploy/restore.sh`) before the first school goes live, then monthly.
- [ ] Decide who holds the encryption keys and where their backup lives
      (separately from database backups).
- [ ] Create the first platform admin on the live system
      (`python -m app.cli create-platform-admin`).

## Done

- Pricing model chosen: ParentPay UK's (2026-10-18).
