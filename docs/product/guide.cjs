// Builds "<BRAND> - user guide.docx": every feature, what it's for and how to use it, for the people who
// use it (school staff, parents, the WardPay team). Keep it current: whenever a feature ships or changes,
// edit SECTIONS below, add a line to CHANGES, then run
//   cd docs/product && npm install && npm run build
const fs = require('fs');
const path = require('path');
const {
  AlignmentType, BorderStyle, Document, Footer, Header, HeadingLevel, LevelFormat, PageBreak, PageNumber, Packer,
  Paragraph, ShadingType, Table, TableCell, TableOfContents, TableRow, TextRun, WidthType,
} = require('docx');

const BRAND = 'WardPay';
const UPDATED = '25 October 2026';
const GREEN = '0F5132';
const LIGHT = 'E8F3EE';

// --- Who can do what ----------------------------------------------------------------------------

const ROLES = [
  ['Proprietor (school owner)', 'Everything in the school, including managing staff, the school’s bank account, approving write-offs, re-opening a closed term and seeing unusual activity.'],
  ['Bursar', 'Money: bills, fees, payments, approvals, cash book, spending, reports and messages. Not school set-up or staff.'],
  ['School admin', 'Everything the bursar does, plus school set-up (calendar, classes, pupils, school profile). Not staff or the bank account.'],
  ['Head office (school groups)', 'For an owner with several branches: the owner works in every branch as its proprietor, a central bursar as its bursar, one branch at a time.'],
  ['Parent', 'Their own children only: balances, paying, receipts, their messages and their data.'],
  [`${BRAND} team (platform admin)`, 'Runs the service for every school: schools, subscription bills, prices, providers and support. Never sees children’s details.'],
];

// --- The features ---------------------------------------------------------------------------------
// Each feature: name, who (can use it), purpose (what it's for), steps (how to use it), notes (good to know).

const SECTIONS = [
  {
    title: 'Getting started',
    intro: `${BRAND} runs in a web browser on a computer or phone. Staff use the console (signing in with an email and password); parents use the parent portal (signing in with their phone number), or simply the payment link in their messages.`,
    features: [
      {
        name: 'Signing your school up',
        who: 'Anyone starting a new school on ' + BRAND,
        purpose: 'Creates your school and your proprietor account, so you can start setting up.',
        steps: [
          `Open the ${BRAND} website and choose “Sign your school up”.`,
          'Fill in the school’s name, your name, email and phone, then press Sign up.',
          'Open the email we send and choose a password. Your school is now active and you are signed in as the proprietor.',
        ],
        notes: ['If the email isn’t confirmed, the sign-up is removed after a while; just sign up again.'],
      },
      {
        name: 'Signing in, and two-factor sign-in',
        who: 'All staff',
        purpose: 'Keeps the school’s money and children’s records safe: a password plus a six-digit code from your phone.',
        steps: [
          'Go to the sign-in page, enter your email and password, and press Sign in.',
          'The first time, you’re asked to set up two-factor sign-in: scan the QR code with an authenticator app (Google Authenticator, Microsoft Authenticator or Authy) and enter the code it shows.',
          'Save the recovery codes somewhere safe and press “I’ve saved them”.',
          'From then on, after your password, type the current code from the app.',
        ],
        notes: [
          'Lost your phone? Choose “Lost your phone? Use a recovery code” on the code screen, or ask the proprietor to reset your two-factor sign-in (Staff → Manage → Reset two-factor).',
          'Forgot your password? Choose “Forgot your password?” on the sign-in page and follow the email.',
        ],
      },
      {
        name: 'Your account',
        who: 'All staff',
        purpose: 'Change your password or two-factor sign-in.',
        steps: [
          'Click your name at the bottom of the sidebar: it opens Your account.',
          'To change your password, enter the current and new password and press Change password. Your other devices are signed out.',
        ],
      },
      {
        name: 'Set up your school (the step-by-step wizard)',
        who: 'Proprietor, school admin',
        purpose: 'Takes a new school from nothing to its first bills in one place: details, school year, classes, fees, pupils and bills.',
        steps: [
          'On the Overview page, in the Getting started card, press “Set up step by step”.',
          'Work through the six steps, pressing “Save and continue” on each. Press “Skip for now” on any step to come back later; your progress is kept.',
          'Fees are typed once per section (for example Primary, JSS, SSS) and copied to every class in it.',
          'At the last step, generate the term’s bills.',
        ],
        notes: ['The Getting started checklist on the Overview page shows what is left until everything is done.'],
      },
    ],
  },
  {
    title: 'Setting the school up',
    intro: 'These pages are under Setup in the sidebar. You normally set them once, then change them now and again.',
    features: [
      {
        name: 'School profile',
        who: 'Proprietor, school admin (others can view)',
        purpose: 'The school’s name, address, phone and email, shown on bills, receipts and messages.',
        steps: ['Go to Setup → School profile.', 'Change any detail and press Save.'],
      },
      {
        name: 'School calendar (sessions and terms)',
        who: 'Proprietor, school admin (others can view)',
        purpose: 'The school year runs September to July in three terms. Bills are made per term, and the current term decides what the dashboard shows.',
        steps: [
          'Go to Setup → Calendar.',
          'The first time, the session is filled in with the usual term dates. Change any date, then press Create.',
          'For next year, press New session; it is filled in the same way.',
          'To change which term is current, press Make current beside it.',
        ],
        notes: ['A term that has been closed (see Closing the books) can’t have its dates changed.'],
      },
      {
        name: 'Moving pupils into the next term, or promoting them',
        who: 'Proprietor, school admin',
        purpose: 'At the start of a term, everyone moves into it in their class; at the start of a session, everyone moves up a class and final-year pupils graduate.',
        steps: [
          'Go to Setup → Calendar.',
          'Beside the next term, press Move students here.',
          'Press Preview to see who goes where, then Move students (or Promote students at a new session).',
        ],
      },
      {
        name: 'Classes and arms',
        who: 'Proprietor, school admin (others can view)',
        purpose: 'Classes in promotion order (Creche to SSS 3) and their arms (A, B, Gold…), so pupils can be placed and billed by class.',
        steps: [
          'Go to Setup → Classes.',
          'The first time, the usual classes are all ticked: untick what the school doesn’t run, add its own, choose the arms, then press Create.',
          'Later: Add from the usual list (missing classes slot into the right place), Add class, “+ arm”, Rename, the arrows to reorder, Retire or Restore.',
        ],
      },
      {
        name: 'Fees per class',
        who: 'Bursar, school admin, proprietor',
        purpose: 'What each class pays each term (tuition, levies, bus, lunch…), so bills can be made in one click.',
        steps: [
          'Go to Setup → Fees. On the Fees per class tab, choose the term.',
          'Type the amount for each fee and class in the grid. Mark a fee as optional (for example the bus) so only pupils who take it pay.',
          'Press Save. Use Copy to bring in last term’s fees.',
          'If bills have already gone out, you’re offered “Add them to … bills”: the new fee goes onto every pupil’s bill in that class.',
        ],
      },
      {
        name: 'Fee items (price list)',
        who: 'Bursar, school admin, proprietor',
        purpose: 'The things you charge for, and a standard price for things only some pupils buy (uniform, textbooks).',
        steps: ['Go to Setup → Fees → Fee items.', 'Press Add fee, give it a name, a kind (tuition, levy, service, materials, other) and, if you like, a price.', 'Retire an item you no longer use.'],
      },
      {
        name: 'Discounts and scholarships',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Automatic sibling discounts, and scholarships or staff-child discounts you give to chosen pupils. They come off the bill when it is made.',
        steps: [
          'Go to Setup → Fees → Discounts and press Add discount.',
          'Choose a percentage or a fixed amount, and whether it applies automatically to siblings or only to pupils you choose.',
          'To give a discount to one pupil, open the pupil’s page → Fees and use Give beside the discount.',
        ],
      },
      {
        name: 'Staff',
        who: 'Proprietor (others can view)',
        purpose: 'Who can sign in to your school and what each can do.',
        steps: [
          'Go to Setup → Staff and press Invite staff.',
          'Enter their name and email, choose their role (bursar or school admin), and press Send invite. They choose their own password from the email.',
          'Press Manage beside someone to rename them, change their role, Reset two-factor, Resend invite, Deactivate or Reactivate.',
        ],
        notes: ['Deactivating signs the person out everywhere at once.'],
      },
      {
        name: 'Payment settings: the school’s bank account',
        who: 'Proprietor (others can view)',
        purpose: `Connects the school’s own bank account, so every payment settles straight to it. ${BRAND} never holds the school’s money.`,
        steps: [
          'Go to Setup → Payment settings and press Connect a bank account.',
          'Choose the bank and type the account number. The bank confirms the account name; check it, then press Connect account.',
        ],
      },
      {
        name: 'Payment settings: pupils’ own account numbers',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Each pupil gets their own bank account number. A transfer into it is matched to that child and receipted automatically, with no matching of bank narrations.',
        steps: [
          'Go to Setup → Payment settings.',
          'Press “Issue account numbers to students without one”. The numbers are created in the background and appear on each pupil’s page, bills and pay cards.',
        ],
        notes: ['This needs the payment provider’s approval for per-pupil account numbers.'],
      },
      {
        name: 'Payment settings: sharing one payment between children',
        who: 'Proprietor, school admin, bursar',
        purpose: 'When a parent pays one amount for several children, this rule decides how it is shared.',
        steps: [
          'Go to Setup → Payment settings, under “When a parent pays one amount for several children”.',
          'Choose: old balances first then clear as many children as possible; in proportion to what each child owes; eldest child first; or youngest child first.',
        ],
      },
    ],
  },
  {
    title: 'Pupils and parents',
    intro: 'Students in the sidebar lists every pupil. Each pupil has their own page with their details, parents, fees and payments.',
    features: [
      {
        name: 'Finding pupils',
        who: 'All staff',
        purpose: 'See the roll, and find pupils who still need something (a class, a parent, consent, an account number).',
        steps: ['Go to Students.', 'Use the Term, Class, Status and Show filters, or search by name or admission number.', 'Click a name to open the pupil’s page.'],
        notes: ['The Overview page lists pupils needing attention; each line is a link to those pupils.'],
      },
      {
        name: 'Adding a pupil',
        who: 'Proprietor, school admin',
        purpose: 'Add one pupil with their parent or guardian.',
        steps: [
          'Go to Students and press Add student.',
          'The next admission number is suggested from your own numbering; change it if you like.',
          'Choose the class, type the names, add the parent’s name and phone, tick that consent was given, and press Add student.',
        ],
      },
      {
        name: 'Importing pupils from a spreadsheet',
        who: 'Proprietor, school admin',
        purpose: 'Load the school’s existing student list (Excel or CSV) in one go, with parents.',
        steps: [
          'Go to Students and press Import from spreadsheet.',
          'Press Download template if you want the expected columns; your own headings are usually understood too.',
          'Choose the file and press Check file. Every problem is listed by row number before anything is saved.',
          'Fix the file if needed, then press Import … students.',
        ],
        notes: ['Importing the same file again updates pupils rather than duplicating them.'],
      },
      {
        name: 'The pupil’s page',
        who: 'All staff',
        purpose: 'Everything about one pupil: details and parents (Overview tab), bills and what each fee owes (Fees tab), and payments with receipts (Payments tab).',
        steps: [
          'Open a pupil from Students.',
          'Overview: Edit details, change class for this term, Withdraw (or reinstate), add or Unlink a parent.',
          'Fees: each fee this term (charged, paid, left), earlier debts, discounts, exemptions and one-off charges.',
          'Payments: every payment and its receipt.',
        ],
      },
      {
        name: 'Parents and guardians',
        who: 'Proprietor, school admin; bursar for reminders and calls',
        purpose: 'Link parents to children. Brothers and sisters share one parent record, so a parent sees all their children together.',
        steps: [
          'On the pupil’s page (Overview), press Add to add a parent or guardian, or Unlink to remove one.',
          'Press Stop reminders if a parent asks not to be messaged (receipts still go); Restart reminders to undo.',
          'For parents who prefer phone calls, open Phone calls and choose their language (English or Pidgin).',
        ],
      },
      {
        name: 'Sending one reminder now',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Remind one parent straight away, without waiting for the schedule.',
        steps: ['On the pupil’s page, press Send reminder. It goes once a day at most.'],
      },
      {
        name: 'The family payment link',
        who: 'Bursar, school admin, proprietor',
        purpose: 'One link per parent that shows all their children’s balances and lets them pay, with no sign-in. It is in every bill message and reminder.',
        steps: [
          'On the pupil’s page, press Payment link beside the parent.',
          'Press Copy link to send it yourself (for example on WhatsApp).',
          'If it went to the wrong person, press Replace link: the old one stops working.',
        ],
      },
      {
        name: 'Pay cards',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Printed cards for parents who don’t use apps: the child’s account number in large type and this term’s fees with what’s left to pay.',
        steps: ['Go to Students and press Print pay cards. Choose a class (four cards to a page) or one pupil, then print.'],
      },
      {
        name: 'A parent’s data: export and erase',
        who: 'Proprietor, school admin',
        purpose: 'The law (NDPA 2023) lets parents ask for their data and, once their children have left, have it erased.',
        steps: [
          'On the pupil’s page, press Export data to download everything held about the parent and their children.',
          'To erase a former pupil’s or parent’s details, press Erase personal details and give the reason. Names and contacts are removed; the money records stay, anonymised.',
        ],
        notes: ['A parent with a current pupil can’t be erased. Issued receipts keep the name they were issued with.'],
      },
    ],
  },
  {
    title: 'Bills',
    intro: 'Billing in the sidebar shows the term’s bills. Each bill has its own page, where it can be added to, corrected, put on a plan or written off. Lines on a bill are never edited or deleted: every change is a new line, so the history is always visible.',
    features: [
      {
        name: 'Generating the term’s bills',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Make every pupil’s bill for the term in one click, from the fees per class, discounts and optional fees. Debts from earlier terms stay owed.',
        steps: [
          'Go to Billing and choose the term.',
          'Press Generate invoices. Set a due date if you want one, and tick “Send each child’s main parent a message with the bill” to tell parents.',
          'Press Preview to see the total per class, then Generate … invoices. For a big school the bills take up to a minute; the button says Making the bills… and the result appears by itself.',
        ],
        notes: ['Running it again only bills pupils who don’t have a bill yet.'],
      },
      {
        name: 'Adding a fee to some classes after the bills have gone out',
        who: 'Bursar, school admin, proprietor',
        purpose: 'For WAEC, NECO, JAMB, an excursion or a party: add one fee to chosen classes, leaving out pupils who aren’t taking part.',
        steps: ['Go to Billing and press Add a fee to classes.', 'Choose the fee, amount and classes, untick anyone not taking part, press Preview, then Add to … pupils.'],
      },
      {
        name: 'Exempting a pupil from a fee',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Take a class fee off one pupil’s bill (for example a staff child excused from the bus). Billing again won’t put it back.',
        steps: ['On the pupil’s page → Fees, press Exempt from a fee, choose the fee and press Exempt.'],
      },
      {
        name: 'One-off charges and opening balances',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Charge one pupil for something (a uniform, a lost ID card) or record what they owed before the school started using ' + BRAND + '.',
        steps: ['On the pupil’s page → Fees, press Add a charge (pick from the price list, with a quantity) or Opening balance.'],
      },
      {
        name: 'Correcting a bill, adding a charge, voiding a bill',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Fix a mistake without losing the history.',
        steps: [
          'Open the bill (from Billing or the pupil’s page).',
          'Add charge adds a line; Correct amount adds a correction (negative to reduce, positive to increase), with a reason.',
          'Void cancels a bill issued by mistake. It is only possible while nothing has been paid on it.',
        ],
        notes: ['A bill in a closed term can’t be voided; correct it instead (the correction is dated today).'],
      },
      {
        name: 'Paying in instalments (payment plans)',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Agree with a parent to pay a bill in parts. Reminders follow the instalment dates, and the pupil isn’t counted as a debtor while the parent keeps to the plan.',
        steps: [
          'Open the bill and press Pay in instalments.',
          'Choose the number of instalments, the first date and how often; adjust any row. The button works once the instalments add up to what’s owed.',
          'Press Agree plan. To end it, press Cancel plan and give a reason.',
        ],
      },
      {
        name: 'The school’s own instalment plans',
        who: 'Bursar, school admin, proprietor set them up; parents choose them',
        purpose: 'Plans such as “Pay in 3” that parents can choose for themselves, so they can spread fees without calling the bursary.',
        steps: [
          'Go to Setup → Fees → Instalment plans and press Add plan.',
          'Give it a name parents will see (for example Pay in 3). For each payment, set its share of the bill and how many days after the term starts it is due; the shares must add up to 100%.',
          'Choose who can choose it: any parent, parents with nothing owed from before, or only staff.',
          'The plan charge starts at WardPay’s standard (₦300 for each payment plus ₦200, so ₦1,100 for 3 payments), which covers the extra payment charges and reminders. You can charge more (a fixed amount, or a percentage up to 20%), but not less. It goes on the bill when a family goes on the plan.',
          'Press Save plan. Use Switch off to stop offering a plan; families already on it keep it.',
        ],
        notes: [
          'Parents see the plans on their payment link and in the portal, with the exact dates and amounts, and choose with two taps.',
          'Payments whose day has already passed fall due straight away. Plans are not offered once a term has ended or been closed.',
          'Most parents won’t choose a plan themselves: see “Putting a family on a plan” below.',
          `${BRAND} charges the school a small fee for each instalment plan made, on its next termly ${BRAND} bill.`,
        ],
      },
      {
        name: 'Putting a family on a plan',
        who: 'Bursar, school admin, proprietor',
        purpose: 'For a parent who asks to spread their payments (by phone or at the school): put all their children on one of the school’s plans and have them told, without the parent using the website.',
        steps: [
          'Open one of the children from Students. Beside the parent, press Instalment plan.',
          'Choose the plan. Each child shows what they will pay and when, or why they can’t go on it (nothing owed this term, or already on a plan).',
          'Leave “Tell … now by WhatsApp or text” ticked, and press Put on the plan.',
          'To put a single bill on a plan instead, open the bill and press Use a school plan.',
        ],
        notes: [
          'The parent is sent the first payment due straight away; after that, reminders follow the plan’s dates.',
          'If the parent turned reminders off, no message is sent: let them know yourself.',
          'Staff-only plans (ones parents can’t pick themselves) can be used here.',
        ],
      },
      {
        name: 'Writing off debt',
        who: 'Bursar and school admin ask; the proprietor approves',
        purpose: 'Stop chasing money the school won’t collect (a family that left owing, a hardship case). Only the proprietor can approve, so two staff together can’t clear a debt.',
        steps: [
          'Open the bill and press Write off.',
          'Enter the amount (up to what’s still owed) and the reason, then press Ask the proprietor.',
          'The proprietor sees it in Payments → Awaiting approval and presses Approve or Reject.',
          'A proprietor’s own write-off takes effect at once.',
        ],
        notes: [
          'The bill keeps a “written off” line; nothing is deleted. Reminders stop, and the accountant’s journal shows it as a bad debt.',
          'A bill on a payment plan needs the plan cancelled first. If money arrives while a request waits, approval is refused so you can ask again for the right amount.',
        ],
      },
    ],
  },
  {
    title: 'Payments',
    intro: 'Payments in the sidebar lists every payment, what still waits for approval, and what the payment provider has paid into the bank.',
    features: [
      {
        name: 'How parents pay',
        who: 'Parents',
        purpose: 'Several ways, all matched to the right child automatically.',
        steps: [
          'Bank transfer to the child’s own account number (on bills, messages, pay cards and the portal). It shows as paid within seconds.',
          'The family payment link in the WhatsApp or text message: one tap opens all their children’s balances and the payment page.',
          'Card, transfer or USSD on the payment page, from the link or the parent portal.',
          'Cash, teller or POS at the school, recorded by staff (below).',
        ],
        notes: ['Money that doesn’t say what it’s for pays the oldest debt first, tuition before other fees. Any extra is kept as credit for the next bill.'],
      },
      {
        name: 'Seeing payments',
        who: 'All staff with payments access',
        purpose: 'Every payment, which bills it paid, and its receipt.',
        steps: ['Go to Payments → Received. Click a receipt number to download it.'],
      },
      {
        name: 'Recording cash, teller, POS or a direct transfer',
        who: 'Recorded by one person, approved by another',
        purpose: 'Money taken by hand only counts once a second person approves it, so nobody can both take cash and mark a debt paid.',
        steps: [
          'Go to Payments and press Record cash or teller.',
          'Choose the pupil, the amount, how it was paid, the date received and the teller or slip number. Optionally tick which fees it is for.',
          'Press Record. Someone else opens Payments → Awaiting approval and presses Approve or Reject.',
        ],
        notes: ['A payment can’t be dated in a closed term.'],
      },
      {
        name: 'Saying what a payment was for, after it arrived',
        who: 'Bursar, school admin, proprietor',
        purpose: 'When a parent says “that transfer was for PTA”, mark it for that fee, once; the fees then show it as paid.',
        steps: ['In Payments → Received, open the payment’s menu (⋯) and choose Mark for fees.', 'Tick the fee or fees it was for and press Mark for ….'],
      },
      {
        name: 'Reversing a payment',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Undo a payment recorded in error (a bounced teller, a duplicate) without deleting anything. The debt comes back.',
        steps: ['In Payments → Received, open the payment’s menu (⋯), press Reverse, give the reason and press Reverse payment.'],
      },
      {
        name: 'Card payment link for one pupil',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Send a parent a link to pay a bill by card.',
        steps: ['On the pupil’s page, open the Payments tab and press Card payment link. Choose the amount, press Create link, then send it to the parent.'],
      },
      {
        name: 'Receipts',
        who: 'Everyone',
        purpose: 'Every payment gets a numbered receipt (no gaps), sent to the parent by WhatsApp, text or email, and downloadable as a PDF.',
        steps: [
          'Staff: click the receipt number in Payments or on the pupil’s page.',
          'Parents: in the portal under Payments and receipts.',
          'Anyone can check a receipt is genuine by scanning its QR code: the check page shows the school, receipt number, amount, date and status, and only the pupil’s initials.',
        ],
      },
      {
        name: 'Paid into the bank (Paystack payouts)',
        who: 'Proprietor, bursar, school admin',
        purpose: 'Check that the money parents paid really reached the school’s bank, and what came off it on the way.',
        steps: [
          'Go to Payments → Paid into the bank and choose the dates.',
          'Each payout shows what parents paid, the charges, what reached the bank, and how many payments it matched.',
          '“Not paid out yet” lists payments received over 3 days ago that no payout has covered; “Paid out, but not in WardPay” lists money with no matching payment.',
          'Press Check now to ask the provider for the latest (it also happens every morning).',
        ],
      },
    ],
  },
  {
    title: 'Cash and spending',
    intro: 'For the money that moves outside the bank transfers: cash in the drawer, banking it, and what the school spends.',
    features: [
      {
        name: 'Cash book',
        who: 'Proprietor, bursar, school admin',
        purpose: 'How much cash should be in the drawer, day by day and per staff member: cash taken, less cash banked and cash spent.',
        steps: ['Go to Cash book and choose the dates. Each day shows cash taken, reversed, banked, spent and what should be held.'],
        notes: ['Cash recorded for a day after it was closed is listed separately: that’s where cash goes missing.'],
      },
      {
        name: 'Banking cash, checked by a second person',
        who: 'Recorded by one person, checked by another',
        purpose: 'Record cash taken to the bank; someone else checks it against the bank statement.',
        steps: [
          'In Cash book, press Record banking: the amount, the date banked and the teller or deposit slip number.',
          'Another person, under “Banking to check against the bank statement”, presses It’s on the statement, or Not on the statement with a reason (the cash then counts as still held).',
        ],
      },
      {
        name: 'Closing the day (drawer count)',
        who: 'Proprietor, bursar, school admin',
        purpose: 'At the end of the day, count the cash and record it next to what the system expected, so any shortage shows at once.',
        steps: ['In Cash book, press Close the day, type the cash counted and a note if needed.'],
      },
      {
        name: 'Recording spending',
        who: 'Recorded by one person, approved by another',
        purpose: 'What the school spends (salaries, diesel, repairs…), by category. Cash spending comes off what should be in the drawer.',
        steps: [
          'Go to Expenses and press Record spending.',
          'Choose the category, the amount, the date, who was paid, what for, and whether it was paid from cash, the bank or a card. Press Record spending.',
          'Someone else presses Approve or Reject. An approved one recorded by mistake can be voided with a reason (it stays on record).',
        ],
        notes: ['Every school starts with the usual categories; add or retire your own.'],
      },
      {
        name: 'Income and expenditure',
        who: 'Proprietor, bursar, school admin',
        purpose: 'Money in against money out for any period, with the surplus or deficit.',
        steps: ['Go to Expenses → Income and expenditure and choose the dates. Money in is by how it came; money out is by spending category.'],
      },
    ],
  },
  {
    title: 'Messages to parents',
    intro: 'Receipts, new bills and reminders go to each child’s main parent by WhatsApp, then text message, then email. Nothing is sent between 8pm and 7am.',
    features: [
      {
        name: 'Sent messages',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Every message sent to parents: when, how, or why it wasn’t sent.',
        steps: ['Go to Messages → Sent messages. Filter by kind and status.'],
      },
      {
        name: 'The wording',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Write the messages in the school’s own words, or keep the standard wording.',
        steps: [
          'Go to Messages → Wording and choose the message.',
          'Edit it, inserting the placeholders shown (child’s name, amount, account number, payment link…). Press Preview to see it and how many text pages it costs.',
          'Press Save, or Use the standard wording to go back.',
        ],
        notes: ['Texts write NGN for ₦, which keeps them in the cheaper SMS alphabet.'],
      },
      {
        name: 'The reminder schedule',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Polite reminders go automatically before and after the due date, stop as soon as the bill is paid, and never exceed a cap per bill.',
        steps: [
          'Go to Messages → Reminder schedule.',
          'Set the days before the due date, the days after it (while still owed), the time of day, the most reminders per bill and the least days between them.',
          'Optionally turn on phone calls for parents who prefer them, with a cap per bill. Press Save schedule.',
        ],
      },
    ],
  },
  {
    title: 'The Overview page and reports',
    intro: 'Overview (the first page) shows the term at a glance. Reports has the lists and downloads.',
    features: [
      {
        name: 'Overview',
        who: 'Staff who see money (others see the school at a glance)',
        purpose: 'Billed, collected and still owed for the term, money received today and this week, older debts, credit held and what awaits approval.',
        steps: [
          'Open Overview. Choose another term at the top right.',
          'Tabs below: Money in (per day and how parents paid), Pace and forecast, By class, How late (debts by age), Pupils.',
        ],
      },
      {
        name: 'Pace and forecast',
        who: 'Staff who see reports',
        purpose: 'Are collections ahead of or behind last term at the same point, and what money is due in the next 30, 60 and 90 days?',
        steps: [
          'On Overview, open the Pace and forecast tab.',
          'The collection curve shows the share of the term’s bills collected by each day, against last term (“Day 21: 54%. By day 21 last term: 61%”).',
          'The cash forecast shows what falls due (on a payment plan, only the instalments due), what’s overdue now, and money in and spending at the recent pace.',
        ],
      },
      {
        name: 'Debtors',
        who: 'Staff who see reports',
        purpose: 'Who owes, always current: filter by class, amount or overdue, sort, call the parent from the list, and download it.',
        steps: ['Go to Reports → Debtors. Use the filters and sort; press Download Excel or Download PDF.'],
      },
      {
        name: 'Money received',
        who: 'Staff who see reports',
        purpose: 'Money received for any period, by day and by method.',
        steps: ['Go to Reports → Money received, choose the dates, and download if needed (Excel has every payment).'],
      },
      {
        name: 'Fee status by pupil',
        who: 'Staff who see reports',
        purpose: 'Each pupil’s fees for a term, each marked paid, part paid or unpaid, for the whole school or one class.',
        steps: ['Go to Reports → Fee status by pupil, choose the term and class, then Download PDF (a page per class, to print) or Download Excel (a sheet per class).'],
      },
      {
        name: 'Unusual activity',
        who: 'Proprietor only',
        purpose: 'Where school money usually goes missing, with who recorded it, so you can ask. Most items will have a good reason.',
        steps: [
          'Go to Reports → Unusual activity and choose 7, 30, 90 or 180 days.',
          'It lists payments reversed, cash recorded after the day was closed, short drawer counts, cash entered days late, bills cut or voided, write-offs, banking that didn’t match the bank, and spending that looks entered twice.',
          'Items marked “Look at this” are the ones to check first. Click an item to open the pupil or bill.',
        ],
        notes: ['When something is flagged, a line appears on your Overview page.'],
      },
    ],
  },
  {
    title: 'For the accountant',
    intro: 'Reports → For your accountant gathers what the school’s accountant, board or auditor needs.',
    features: [
      {
        name: 'The general journal',
        who: 'Staff who see reports',
        purpose: 'Everything the school’s money did in a period, as balanced double entries, ready for the accountant or accounting software. Pupils appear by admission number only.',
        steps: [
          'Go to Reports → For your accountant and choose the dates.',
          'Press Download Excel (journal, account totals, chart of accounts) or Download CSV (for accounting software). The file is prepared first (the button says Preparing…); for a long period at a big school this can take a minute. Keep the page open and it downloads by itself.',
        ],
        notes: ['Card and online payments are shown at the full amount; the provider’s charges appear in Payments → Paid into the bank.'],
      },
      {
        name: 'Chart of accounts',
        who: 'Bursar, school admin, proprietor',
        purpose: 'Use the account codes and names the school’s accountant already uses.',
        steps: ['Under Chart of accounts, change any code or name and press Save. Leave a box empty to go back to the default.'],
      },
      {
        name: 'Closing the books (closing a term)',
        who: 'Bursar, school admin, proprietor close; only the proprietor re-opens',
        purpose: 'Once a term’s figures have been reported, close it, so nothing can be backdated into it and the reported figures stay the figures.',
        steps: [
          'Under Closing the books, press Close [term] (it appears once the term has ended), then Close the term.',
          'Anything still waiting for approval in that term must be decided first; you’ll be told what.',
          'To re-open, the proprietor presses Re-open the last closed term and gives a reason.',
        ],
        notes: ['After closing, corrections are still possible: they are dated today. Money arriving through the provider is never refused.'],
      },
      {
        name: 'End-of-session pack',
        who: 'Staff who see reports',
        purpose: 'One Excel file that closes the school year: a summary, each term’s bills, money in and spending, who still owes, the cash book by month and the whole year’s journal.',
        steps: [
          'Under End-of-session pack, choose the school year and press Download the pack. For the current year it runs to today.',
          'The pack is prepared first (the button says Preparing…) and downloads by itself when ready.',
        ],
        notes: ['A prepared file can only be downloaded by the person who asked for it, and is deleted after a day.'],
      },
    ],
  },
  {
    title: 'The school’s own records',
    intro: '',
    features: [
      {
        name: 'Activity log',
        who: 'Proprietor, bursar, school admin',
        purpose: 'Every sign-in and every change to money or permissions, newest first, with who did it. Nobody can edit it.',
        steps: ['Go to Setup → Activity log.'],
      },
      {
        name: 'Subscription',
        who: 'Proprietor, bursar, school admin',
        purpose: `What the school pays for ${BRAND} (a set-up fee once, a fee per pupil each term, and a small fee for each family put on an instalment plan), how to pay, and the bills.`,
        steps: [
          'Go to Setup → Subscription. The plan card shows the prices, what this term’s pupils cost, and how many instalment plans the next bill will include.',
          'Under each bill, press What it’s for to see it line by line (set-up fee, pupils × price, instalment plans × fee, any discount).',
          'Download a bill as a PDF, or press Pay by card; it shows as paid as soon as the payment clears.',
        ],
        notes: ['Parents never pay anything to use ' + BRAND + '.'],
      },
    ],
  },
  {
    title: 'For parents',
    intro: 'Parents don’t need to install anything. The link in their messages opens straight to the payment page; the portal shows everything.',
    features: [
      {
        name: 'Paying from the message link',
        who: 'Parents',
        purpose: 'Pay for all children with one tap, without signing in or working out amounts.',
        steps: [
          'Open the link in the WhatsApp or text message.',
          'See every child’s balance and what it’s made of, and each child’s account number (press Copy to copy it).',
          'Press Pay everything due, or Pay a different amount to pay part (the page shows how the school will share it), or choose the amount for each child. Then pay by card, transfer or USSD.',
        ],
      },
      {
        name: 'The parent portal',
        who: 'Parents',
        purpose: 'All children at the school in one place: bills, payments, receipts, instalment plans and messages.',
        steps: [
          'Go to the parent portal, enter your phone number and press Text me a code; type the code and press Sign in.',
          'If you have children at more than one school, choose the school. If they are all branches of one group there is nothing to choose: other branches appear under At other branches, with what is owed there; press Open … to see and pay for those children.',
          'Each child shows what is owed, any credit and their account number. Press Pay now (or Pay for all your children). Receipts are under Payments and receipts.',
        ],
      },
      {
        name: 'Spreading the fees with a school plan',
        who: 'Parents',
        purpose: 'Pay a child’s fees in parts on dates the school has set, instead of all at once.',
        steps: [
          'On the payment link (or Pay for all your children in the portal), look under the child for “Can’t pay it all at once?”.',
          'Each plan shows what is due and when, and any plan charge. Press Choose, then Yes, choose ….',
          'The page then asks only for the first payment. Reminders follow the plan’s dates.',
        ],
      },
      {
        name: 'Messages and my data',
        who: 'Parents',
        purpose: 'Choose whether to get bill messages and reminders, and see or take away your data.',
        steps: [
          'In the portal, open Settings to turn bill messages and reminders off (receipts still come).',
          'Press Download my data for everything the school holds about you and your children.',
        ],
      },
    ],
  },
  {
    title: `For the ${BRAND} team`,
    intro: 'The platform console is for the people who run ' + BRAND + ' itself. It never shows children’s details.',
    features: [
      {
        name: 'Platform overview and trends',
        who: 'Platform admin',
        purpose: 'Every school at a glance, and the last 12 months: subscription income, money parents paid through ' + BRAND + ', schools joining or suspended.',
        steps: ['Go to Overview in the platform console.'],
      },
      {
        name: 'Schools',
        who: 'Platform admin',
        purpose: 'Create a school, suspend or reactivate one, and help its staff.',
        steps: [
          'Go to Schools. Press New school, fill it in and press Create school to invite its proprietor.',
          'Press Suspend (staff are signed out at once) or Reactivate, with a reason.',
          'Open a school to see its staff, figures and bills; for a locked-out staff member press Send password link or Reset two-factor.',
        ],
      },
      {
        name: 'School groups (several branches, one owner)',
        who: 'Platform admin; then the owner and central bursar',
        purpose:
          'Lets an owner with several branches or campuses sign in once and work in any branch. Each branch stays its own school: its own pupils, bills, receipt numbers, bank account and ' +
          BRAND +
          ' bill. Branch staff still see only their own branch.',
        steps: [
          'Platform console: go to School groups, type the group’s name and press Add group.',
          'On the group’s page, type a school’s name under Add a school and press Add as a branch. Do this for each branch.',
          'Under Head office, fill in the owner’s name and email, choose Owner or Central bursar and press Send invite. They choose a password from the email and turn on two-factor sign-in.',
          'Head office: after signing in, pick the branch from the Branch menu under the logo. Everything on screen is then that branch; pick another branch to switch.',
          'To take a branch out press Take out; to stop someone press Deactivate. Either takes effect at once.',
          'Head office: press All branches in the menu to see every branch’s money side by side with the group’s totals. Press Download Excel for a copy, or Work in this branch to open one.',
        ],
        notes: ['All branches shows money only, never pupils’ or parents’ details. Parents with children in several branches are coming next.'],
      },
      {
        name: 'Subscription billing and plans',
        who: 'Platform admin',
        purpose: 'Bill every school for the term, record payments, and set prices.',
        steps: [
          'Go to Billing and press Bill this term; Preview, then Send … bills. A school is never billed twice for a term.',
          'On a bill: Record payment (a bank transfer), Adjust (waive the set-up fee or give a discount), Change due date, Void, or Download PDF.',
          'Go to Plans to set the set-up fee, the per-pupil fee, the fee per instalment plan (₦200 to start) and the standard plan charge per payment (₦300 to start), and move a school to another plan.',
        ],
      },
      {
        name: 'Problems, providers and support lookup',
        who: 'Platform admin',
        purpose: 'Fix what got stuck, switch providers without a release, and look things up when someone calls.',
        steps: [
          'Problems: payments set aside, card payments in review, background tasks that gave up and failed messages. Press Retry, or Resolve with a note.',
          'Providers: switch the payment provider (Paystack or Bachs) or the SMS provider (Termii or Africa’s Talking) and press Switch.',
          'Support lookup: search for a school, a staff email, a payment, an account number or a receipt, and press Search.',
        ],
      },
    ],
  },
];

const CHANGES = [
  ['27 Oct 2026', 'Parents with children at several branches of a group sign in once and open each branch from their home page.'],
  ['27 Oct 2026', 'All branches: head office sees every branch’s money side by side, with totals and an Excel download.'],
  ['27 Oct 2026', 'School groups: an owner with several branches signs in once and switches branch from a menu; set up by the WardPay team.'],
  ['26 Oct 2026', 'Subscription: each WardPay bill line by line, the instalment plan fee, and plans waiting for the next bill.'],
  ['26 Oct 2026', 'Generating a big school’s bills now runs in the background and shows the result when done.'],
  ['25 Oct 2026', 'WardPay’s standard plan charge (₦300 a payment + ₦200), which schools can raise but not lower.'],
  ['25 Oct 2026', 'Putting a whole family on a plan from the pupil’s page, and telling the parent by WhatsApp or text.'],
  ['25 Oct 2026', 'The school’s own instalment plans: set up by staff, chosen by parents; the per-plan fee on WardPay bills.'],
  ['24 Oct 2026', 'The journal and the end-of-session pack are now prepared in the background, then download by themselves.'],
  ['24 Oct 2026', 'First edition: every feature, what it is for and how to use it.'],
];

// --- Layout -------------------------------------------------------------------------------------

const PAGE_WIDTH = 11906; // A4
const MARGIN = 1134; // 2 cm
const CONTENT = PAGE_WIDTH - 2 * MARGIN;
const border = { style: BorderStyle.SINGLE, size: 4, color: 'D0D7D3' };
const borders = { top: border, bottom: border, left: border, right: border };

function cell(text, width, { header = false } = {}) {
  return new TableCell({
    borders,
    width: { size: width, type: WidthType.DXA },
    shading: header ? { fill: GREEN, type: ShadingType.CLEAR, color: 'auto' } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({ children: [new TextRun({ text, bold: header, color: header ? 'FFFFFF' : undefined, size: 21 })] })],
  });
}

function table(headers, rows, widths) {
  return new Table({
    width: { size: CONTENT, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], { header: true })) }),
      ...rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, widths[i])) })),
    ],
  });
}

const p = (text, opts = {}) =>
  new Paragraph({ spacing: { after: 120 }, ...opts, children: [new TextRun({ text, size: 22, ...(opts.run || {}) })] });
const label = (title, text) =>
  new Paragraph({
    spacing: { after: 100 },
    children: [new TextRun({ text: `${title} `, bold: true, size: 22, color: GREEN }), new TextRun({ text, size: 22 })],
  });
const h1 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, pageBreakBefore: true, spacing: { after: 160 }, children: [new TextRun(text)] });
const h2 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 280, after: 100 }, children: [new TextRun(text)] });
const bullet = (text) =>
  new Paragraph({ numbering: { reference: 'bullets', level: 0 }, spacing: { after: 60 }, children: [new TextRun({ text, size: 22 })] });
let listNumber = 0;
const steps = (items) => {
  listNumber += 1;
  return items.map(
    (text) =>
      new Paragraph({
        numbering: { reference: 'steps', level: 0, instance: listNumber },
        spacing: { after: 60 },
        children: [new TextRun({ text, size: 22 })],
      }),
  );
};

const children = [
  new Paragraph({ spacing: { before: 2400, after: 200 }, children: [new TextRun({ text: `${BRAND} user guide`, bold: true, size: 56, color: GREEN })] }),
  p('Every feature: what it is for, and how to use it.', { run: { size: 28 } }),
  p(`For proprietors, bursars, school admins, parents and the ${BRAND} team. Updated ${UPDATED}.`, { run: { color: '555555' } }),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ spacing: { after: 160 }, children: [new TextRun({ text: 'Contents', bold: true, size: 34, color: GREEN })] }),
  new TableOfContents('Contents', { hyperlink: true, headingStyleRange: '1-2' }),
  p('If the contents look empty, right-click them in Word and choose Update Field.', { run: { color: '777777', size: 18 } }),
  h1('Who can do what'),
  p('What each person sees depends on their role. Buttons a person can’t use don’t appear for them.'),
  table(['Role', 'What they can do'], ROLES, [2600, CONTENT - 2600]),
];

for (const section of SECTIONS) {
  children.push(h1(section.title));
  if (section.intro) children.push(p(section.intro));
  for (const f of section.features) {
    children.push(h2(f.name));
    children.push(label('Who:', f.who));
    children.push(label('What it’s for:', f.purpose));
    children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [new TextRun({ text: 'How to use it', bold: true, size: 22, color: GREEN })] }));
    children.push(...steps(f.steps));
    if (f.notes && f.notes.length) {
      children.push(new Paragraph({ spacing: { before: 80, after: 60 }, children: [new TextRun({ text: 'Good to know', bold: true, size: 22, color: GREEN })] }));
      children.push(...f.notes.map(bullet));
    }
  }
}

children.push(h1('What changed in this guide'));
children.push(table(['Date', 'Change'], CHANGES, [1900, CONTENT - 1900]));

const doc = new Document({
  creator: BRAND,
  title: `${BRAND} user guide`,
  features: { updateFields: true },
  styles: {
    default: { document: { run: { font: 'Calibri', size: 22 } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 34, bold: true, color: GREEN }, paragraph: { spacing: { after: 160 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { size: 26, bold: true, color: '1F2A24' }, paragraph: { spacing: { before: 240, after: 100 }, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: 'bullets', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
      { reference: 'steps', levels: [{ level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 320 } } } }] },
    ],
  },
  sections: [
    {
      properties: { page: { size: { width: PAGE_WIDTH, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: `${BRAND} user guide`, size: 18, color: '777777' })] })] }) },
      footers: {
        default: new Footer({
          children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: ['Page ', PageNumber.CURRENT], size: 18, color: '777777' })] })],
        }),
      },
      children,
    },
  ],
});

const out = path.join(__dirname, `${BRAND} - user guide.docx`);
Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync(out, buffer);
  console.log(`Written: ${out}`);
});
