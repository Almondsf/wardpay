// Builds "<BRAND> - what it does.docx": a plain-language overview of what's built and how it helps
// schools and parents. Edit the content below when a feature ships, add a line to CHANGES, then run
//   cd docs/product && npm install && npm run build
const fs = require('fs');
const path = require('path');
const {
  AlignmentType, BorderStyle, Document, Footer, Header, HeadingLevel, LevelFormat, PageNumber, Packer,
  Paragraph, ShadingType, Table, TableCell, TableRow, TextRun, WidthType,
} = require('docx');

const BRAND = 'WardPay'; // the product's working name (APP_NAME)
const UPDATED = '25 October 2026';
const GREEN = '0F5132';
const LIGHT = 'E8F3EE';

// --- Content ------------------------------------------------------------------------------------

const SUMMARY = [
  `${BRAND} helps Nigerian private schools bill fees, collect them and know at any moment who has paid. ` +
    'Every pupil gets their own bank account number, so a transfer into it is matched to that child and receipted automatically, ' +
    'with no bursar matching bank narrations by hand.',
  'Parents see all their children in one place on their phone, pay by transfer or card, get a receipt on WhatsApp or by text, ' +
    'and are reminded politely before and after fees are due. Proprietors see billed, collected and owed at a glance.',
  `The school’s money goes straight into the school’s own bank account; ${BRAND} never holds it. ` +
    `Parents use ${BRAND} free; the school pays a set-up fee and a small fee per pupil each term.`,
];

const AREAS = [
  {
    title: 'For the proprietor (school owner)',
    rows: [
      ['A live dashboard: billed, collected and still owed for the term, per class, with a progress bar each',
        'Know where the money stands without asking the bursar or waiting for month-end'],
      ['Unusual activity: reversals, cash entered late, short drawer counts, bills cut or voided, spending entered twice, each with who recorded it',
        'See the warning signs of missing money early, without auditing every entry yourself'],
      ['Write off debt the school won’t collect: staff can ask, only you approve',
        'Bad debts leave the books cleanly, and nobody can quietly clear a debt they took cash for'],
      ['See what Paystack paid into your bank each day, the charges taken, and any payment it hasn’t paid out',
        'Know the money parents paid actually reached the bank, without reading statements line by line'],
      ['Close a term once its figures are reported: nothing can be backdated into it, and only you can re-open it, with a reason',
        'The figures you reported to the board or the accountant stay the figures'],
      ['An end-of-session pack: the year’s summary, each term, money in and out, debtors, cash book and journal in one file',
        'Close the year in one download, for the accountant, the board or the auditor'],
      ['A general journal for your accountant: every bill, payment, banking and approved expense as double entries, in Excel or CSV',
        'The accountant gets clean books without retyping receipts, using the account codes they already use'],
      ['This term against last term at the same point, and what falls due in the next 30, 60 and 90 days',
        'See early whether collections are behind, and plan salaries and suppliers around money that is actually coming'],
      ['Money received per day and how parents paid (transfer, card, cash, teller, POS)',
        'Spot slow weeks early and see which payment channels parents actually use'],
      ['How late the debts are: not yet due, 1–30, 31–60, 61–90 and over 90 days',
        'Focus follow-up on the oldest debts first'],
      ['Every figure comes from the same records as invoices and receipts',
        'Trust the numbers: they can’t drift from the books'],
      ['Connect the school’s own bank account once (proprietor only)',
        'Parents’ payments settle directly to the school; nobody in between holds the money'],
      ['An activity log nobody can edit: every sign-in, payment, correction and change, with who and when',
        'Accountability for staff, and evidence if something is ever disputed'],
      ['Two-factor sign-in for proprietors and bursars',
        'A stolen password alone can’t move money or see the books'],
    ],
  },
  {
    title: 'For the bursar',
    rows: [
      ['Set fees once per term in one grid per class, copy last term’s, mark items like the bus as optional',
        'Term set-up takes minutes, not days'],
      ['Sibling discounts applied automatically; scholarships and staff-child discounts given to chosen pupils',
        'No more working out discounts by hand on every bill'],
      ['Generate every pupil’s bill for the term in one click, after a preview; old debts carry forward',
        'Bills go out on day one, correct and consistent'],
      ['Each pupil has their own bank account number; transfers into it are matched and receipted within seconds',
        'The biggest time-saver: no reconciling bank statements against names'],
      ['Record cash, teller and POS payments, approved by a second person before they count',
        'Cash is controlled; one person can’t record and approve their own entry'],
      ['Payments clear the oldest debt first; overpayments are kept as credit and used on the next bill',
        'Arrears are handled fairly and automatically'],
      ['Numbered receipts with no gaps, as a PDF with a QR code anyone can scan to check it’s genuine',
        'Fake or altered receipts are caught; missing receipt numbers can’t hide'],
      ['Reverse a payment recorded in error (e.g. a bounced teller) without deleting anything',
        'Mistakes are fixed openly, with a trail'],
      ['Correct a bill by adding a line (lines are never edited), void a bill issued by mistake, record old debts',
        'The full history of every bill is always visible'],
      ['Fee status by pupil: for the whole school or one class, each fee (tuition, bus, levies) marked paid, part paid or unpaid with what was paid and what’s left; each class lists only its own fees, as a printable PDF with a page per class or Excel with a sheet per class',
        'Class teachers and the bursary see at a glance who still owes what, without adding anything up'],
      ['Debtors list: who owes, how much, how late, the parent’s phone (tap to call), reminders sent; download for Excel or PDF',
        'Follow-up calls start from an up-to-date list'],
      ['Add a fee to some classes after the bills have gone out (WAEC, NECO, JAMB, an end-of-year party), ticking off pupils not taking part',
        'Mid-term fees reach the right pupils in one step, without editing bills one by one'],
      ['Exempt a pupil from a class fee; it comes off their bill and stays off',
        'Pupils who aren’t taking part are never charged by mistake'],
      ['A price list for uniforms, textbooks and other extras; add them to one pupil’s bill with a quantity',
        'Individual purchases are billed and tracked like everything else'],
      ['The school’s own instalment plans (“Pay in 3”) that parents choose themselves on their phone, with an optional plan charge',
        'Parents spread fees without phoning the bursary, and the school decides who may and what it costs'],
      ['Agree with a parent to pay a bill in instalments (2 to 12 dated amounts)',
        'Parents who keep to a plan aren’t chased; reminders ask for the next instalment, not the whole bill'],
      ['Automatic polite reminders before and after the due date by WhatsApp, then text, then email',
        'Fewer awkward phone calls; parents are reminded consistently, never at night'],
      ['Write the messages in the school’s own words, with a preview; send one parent a reminder on demand',
        'Messages sound like the school, not a robot'],
    ],
  },
  {
    title: 'For the school admin',
    rows: [
      ['Everything the bursar can do (bills, extra fees, payments, approvals, reports, messages), with a code from an authenticator app at sign-in',
        'One person can run the school’s fees day to day; only the proprietor controls staff and the bank account'],
      ['Load all pupils and their parents from the school’s existing Excel or CSV list, see every problem by row before saving',
        'Getting started takes an afternoon, not weeks of typing'],
      ['Set up the school year, terms, classes and arms (the usual classes from Creche to SSS 3 come ready to tick); move everyone into the next term or promote them at the end of the year',
        'The calendar and classes stay right year after year'],
      ['Brothers and sisters share one parent record; record which parent gets fee messages',
        'Parents see all their children together; no duplicate messages'],
      ['A getting-started checklist on the dashboard for new schools',
        'Nobody has to guess what to set up next'],
      ['The school at a glance when they sign in: pupils on roll, who joined or left this term, pupils per class and arm, and which pupils still need a class, a parent, consent or an account number (one click shows who)',
        'Gaps are fixed before bills go out, without the admin seeing any money figures'],
      ['Give a parent a copy of their data, and erase a former pupil’s or parent’s details when asked',
        'The school meets its duties under the Nigeria Data Protection Act without spreadsheets'],
    ],
  },
  {
    title: 'For parents',
    rows: [
      ['Sign in with the phone number the school has on record and a one-time code; no password',
        'Nothing to forget, and nothing to reset'],
      ['One simple page with everything needed to pay: the total at the top, then each child with what’s owed, a Pay online button, their own account number (with a copy button) and the term’s fees; “Pay for all your children” at the bottom. Bills, receipts and settings are a tap away',
        'Built for parents who aren’t confident on smartphones: nothing to search for, no tapping around to pay'],
      ['Pay by bank transfer to the child’s own account number, or by card',
        'Pay the way that suits them; the payment finds the right child automatically'],
      ['Parents who don’t read messages easily can get a phone call, in English or Pidgin: what’s owed, the child’s account number read out slowly twice, and a call when the payment arrives',
        'Nobody is left out because they can’t read a text; they can hand the number to any POS agent'],
      ['One payment link of their own, in every bill and reminder: all their children, no sign-in. One payment (bank transfer, card or USSD) covers several children, and they choose how much goes to each; the same “Pay for all your children” button is in the portal',
        'Paying for three children takes one minute, part now and the rest later if needed, with a receipt per child'],
      ['See every bill with its items, every payment, and download receipts any time',
        'No more asking the bursary for a receipt or a breakdown'],
      ['A receipt by WhatsApp or text as soon as a payment arrives; a message when a new bill is ready',
        'Peace of mind that the payment was received'],
      ['See an instalment plan, what’s paid against it and what’s next',
        'Clarity on what’s due and when'],
      ['Turn off bill messages and reminders (receipts still come); download all their data',
        'Parents stay in control of their messages and information'],
      ['Made for phones first; the phone’s own keypad comes up for numbers, and the texted sign-in code can be filled in with one tap; free for parents',
        'No app to install, nothing new to learn, and no extra charges'],
    ],
  },
  {
    title: 'Money safety and trust',
    rows: [
      [`${BRAND} never holds school money: payments settle to the school’s account through the payment company`,
        'No risk of the platform holding or delaying a school’s funds'],
      ['Payments, receipts and the activity log can’t be edited or deleted, by anyone, even in the database',
        'The books can’t be quietly changed'],
      ['Every school’s data is kept apart; staff of one school can never see another’s',
        'Schools can trust a shared platform with their records'],
      ['Encrypted backups kept off the server; regular test restores',
        'A lost server doesn’t mean lost records'],
    ],
  },
  {
    title: 'Children’s data (NDPA 2023)',
    rows: [
      ['Only what fee collection needs is collected; parents’ phones and emails are encrypted',
        'Less data held means less to lose'],
      ['Parents’ consent recorded; a plain-language privacy notice on the website',
        'The school can show how it handles children’s data'],
      ['Old message texts deleted after a year; pupils who left over six years ago are anonymised automatically',
        'Data isn’t kept longer than needed'],
      ['Error reports and logs never contain codes, tokens, BVNs or full account numbers',
        'Sensitive details don’t leak through technical systems'],
    ],
  },
  {
    title: `For ${BRAND} itself (the platform)`,
    rows: [
      ['Schools sign themselves up on the website; the proprietor confirms by email',
        'New schools can start without a sales call'],
      ['Platform overview: active schools, new sign-ups, pupils, money processed, subscription income owed, and 12-month charts of income and schools',
        'Run the business from one screen'],
      ['Bill every school for the term in one click; schools pay by card from the console, or by transfer that you record; waive the set-up fee, give a discount or more time; PDF invoices emailed to proprietors',
        'Subscription income is collected without spreadsheets'],
      ['Help a locked-out school: send a staff member their password link again or reset their two-factor sign-in',
        'A proprietor who lost their phone is back in within minutes'],
      ['Problems to fix: payments set aside, card payments in review, stuck tasks and failed messages across every school, with retry',
        'Nothing gets lost quietly; problems are found before a school calls'],
      ['Support lookup by school, staff email, payment reference, account number or receipt (children shown as initials only)',
        'Answer "I paid but it isn’t showing" calls quickly, without exposing children’s details'],
    ],
  },
];

const PRICING = [
  ['Set-up fee', 'Once, when the school joins'],
  ['Per pupil', 'A third of the yearly fee each term, for the pupils enrolled that term'],
  ['Parents', 'Free. Card and bank charges come off each payment.'],
];

const COMING = [
  'Phone calls in Yoruba, Hausa and Igbo (needs voices recorded by native speakers)',
  'Going live needs your accounts: Paystack approval for pupil account numbers, WhatsApp Business and Termii for messages (see OWNER_TODO.md)',
];

// Newest first. Add a line whenever something ships.
const CHANGES = [
  ['25 Oct 2026', 'Schools set their own instalment plans (“Pay in 3”), and parents choose them on their phone'],
  ['24 Oct 2026', 'Unusual activity for the proprietor: reversals, late cash, short counts, cut bills, write-offs and more, with who recorded them'],
  ['24 Oct 2026', 'Writing off debt the school won’t collect: staff ask, only the proprietor approves'],
  ['24 Oct 2026', 'Paid into the bank: Paystack payouts matched to payments, with charges and anything missing'],
  ['24 Oct 2026', 'Closing a term: nothing can be backdated into reported figures; only the proprietor re-opens'],
  ['24 Oct 2026', 'End-of-session pack: the whole school year in one Excel file'],
  ['24 Oct 2026', 'A general journal for the accountant (Excel or CSV) with the school’s own account codes'],
  ['24 Oct 2026', 'Pace and forecast on the dashboard: this term against last at the same point, and what falls due in the next 30, 60 and 90 days'],
  ['24 Oct 2026', 'Expenses with a second person’s approval, cash spending off the drawer, and income against expenditure'],
  ['24 Oct 2026', 'Cash book: what should be in the drawer, cash banked (checked by a second person), daily cash counts and late entries flagged'],
  ['23 Oct 2026', 'Parents pay for all their children with one tap from their WhatsApp or text link; the school chooses how a smaller amount is shared'],
  ['23 Oct 2026', 'WardPay team can discount a school’s bill (e.g. waive the set-up fee) or move its due date'],
  ['23 Oct 2026', 'Month-by-month charts for the WardPay team: income, money through WardPay, schools joining'],
  ['22 Oct 2026', 'WardPay team can resend password links and reset two-factor sign-in for a school’s staff'],
  ['22 Oct 2026', 'Problems to fix: one page for the WardPay team showing everything stuck across schools'],
  ['22 Oct 2026', 'Texts can go through Termii or Africa’s Talking, switched at any time from the platform console'],
  ['22 Oct 2026', 'Schools pay their WardPay bill by card from the console; the money goes to the platform’s own account'],
  ['20 Oct 2026', 'Parents (and the bursary) can pay for particular fees, such as WAEC; the receipt says what it was for'],
  ['20 Oct 2026', 'Fees added to the fee grid after billing can be added to the bills already sent, in one click'],
  ['20 Oct 2026', 'Fee breakdowns everywhere: each fee charged, paid and left, for staff, in the portal and on the payment page'],
  ['20 Oct 2026', 'School admins can now do everything the bursar does, with two-factor sign-in'],
  ['20 Oct 2026', 'Payment provider switchable at any time (Paystack or Bachs), with Bachs checkout and onboarding'],
  ['20 Oct 2026', 'Fee status by pupil: every fee paid, part paid or unpaid, for the school or one class (Excel and PDF)'],
  ['21 Oct 2026', 'Fee status download: each class with only its own fees, what was paid on part-paid fees, a printable page per class'],
  ['22 Oct 2026', 'Adding a pupil suggests the next admission number in the school’s own pattern; staff can change it'],
  ['22 Oct 2026', 'Set up a new school step by step: details, school year, classes, fees once per section (Primary, JSS, SSS), pupils and bills'],
  ['22 Oct 2026', 'Text messages cost less than half as much: they write NGN for ₦ and keep to the plain SMS alphabet (a reminder went from 7 SMS pages to 3)'],
  ['22 Oct 2026', 'Reminders and new-bill messages list what the money is for (biggest first), with the child’s account number'],
  ['22 Oct 2026', 'Parent portal redesigned: one simple page with the total, each child’s amount, account number and fees, and one button to pay for everyone'],
  ['21 Oct 2026', 'Money that doesn’t say what it’s for covers tuition first; a payment can be marked afterwards for the fee the parent meant (e.g. PTA)'],
  ['21 Oct 2026', 'A fee added to a class after billing (e.g. WAEC for SSS 3) goes straight onto every pupil’s bill in that class'],
  ['21 Oct 2026', 'Printable pay cards: each child’s account number in large type and this term’s fees with what’s left to pay, a class at a time'],
  ['21 Oct 2026', 'The school year comes filled in with the usual term dates; check them and create it in one click'],
  ['21 Oct 2026', 'Classes set up in one click: the usual list from Creche to SSS 3 comes ticked; untick what you don’t run and add your own'],
  ['21 Oct 2026', 'New look: a sidebar for every page, calmer colours and type, a dashboard that fits on one screen, and the pupil page split into tabs'],
  ['19 Oct 2026', 'Pay for all your children by bank transfer, card or USSD, from the link or the portal'],
  ['19 Oct 2026', 'Phone calls for parents who prefer them: overdue reminders and payment confirmations, in English or Pidgin'],
  ['19 Oct 2026', 'Family payment link: one link per parent to pay for all their children, choosing the amount per child'],
  ['19 Oct 2026', 'School at a glance for school admins: pupils, classes and records that need attention'],
  ['19 Oct 2026', 'Fees added to classes mid-term, pupil exemptions, and a price list for uniforms and books'],
  ['19 Oct 2026', 'Payment plans: bills can be paid in instalments; lateness and reminders follow the plan'],
  ['19 Oct 2026', 'Data protection tools, error monitoring, encrypted backups, public website and privacy notice'],
  ['18 Oct 2026', 'School sign-up, subscription billing and the platform console'],
  ['18 Oct 2026', 'Dashboard and reports: debtors, money received, Excel and PDF downloads'],
  ['18 Oct 2026', 'Messages to parents: receipts, bill notices and reminders by WhatsApp, text and email'],
  ['18 Oct 2026', 'Parent portal with phone sign-in'],
  ['18 Oct 2026', 'Receipts with QR verification'],
  ['18 Oct 2026', 'Payments: pupil account numbers, card payments, cash and teller with approval'],
  ['17 Oct 2026', 'Fees and one-click termly billing'],
  ['17 Oct 2026', 'School set-up, pupils and parents, spreadsheet import'],
  ['17 Oct 2026', 'Sign-in, roles, two-factor sign-in and the activity log'],
];

// --- Layout -------------------------------------------------------------------------------------

const PAGE_WIDTH = 11906; // A4
const MARGIN = 1134; // 2 cm
const CONTENT = PAGE_WIDTH - 2 * MARGIN;
const border = { style: BorderStyle.SINGLE, size: 4, color: 'D0D7D3' };
const borders = { top: border, bottom: border, left: border, right: border };

function cell(text, width, { header = false, shade } = {}) {
  return new TableCell({
    borders,
    width: { size: width, type: WidthType.DXA },
    shading: header ? { fill: GREEN, type: ShadingType.CLEAR, color: 'auto' } : shade ? { fill: shade, type: ShadingType.CLEAR, color: 'auto' } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({ children: [new TextRun({ text, bold: header, color: header ? 'FFFFFF' : undefined, size: 20 })] })],
  });
}

function table(headers, rows, widths) {
  return new Table({
    width: { size: CONTENT, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], { header: true })) }),
      ...rows.map((r, n) => new TableRow({ children: r.map((t, i) => cell(t, widths[i], { shade: n % 2 ? 'F6F8F7' : undefined })) })),
    ],
  });
}

const p = (text, opts = {}) => new Paragraph({ spacing: { after: 120 }, ...opts, children: [new TextRun({ text, size: 22, ...(opts.run || {}) })] });
const h1 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 360, after: 160 }, children: [new TextRun(text)] });
const bullet = (text) => new Paragraph({ numbering: { reference: 'bullets', level: 0 }, spacing: { after: 80 }, children: [new TextRun({ text, size: 22 })] });

const children = [
  new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: BRAND, bold: true, size: 48, color: GREEN })] }),
  new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: 'What it does for schools and parents', size: 32 })] }),
  new Paragraph({
    spacing: { after: 240 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: GREEN, space: 6 } },
    children: [new TextRun({ text: `Updated ${UPDATED}. Everything listed here is built and tested unless it’s under “Coming next”.`, size: 20, color: '5F6B73' })],
  }),
  h1('In one minute'),
  ...SUMMARY.map((t) => p(t)),
  ...AREAS.flatMap((area) => [h1(area.title), table(['What it does', 'How it helps'], area.rows, [5400, CONTENT - 5400])]),
  h1('Pricing'),
  p('Modelled on ParentPay in the UK: the school pays, parents use it free. Prices are set in the platform console.'),
  table(['Charge', 'When'], PRICING, [2600, CONTENT - 2600]),
  h1('Coming next'),
  ...COMING.map(bullet),
  h1('What’s new'),
  table(['Date', 'Added'], CHANGES, [1800, CONTENT - 1800]),
];

const doc = new Document({
  creator: 'Oluwafemi Oloyede',
  title: `${BRAND}: what it does`,
  styles: {
    default: { document: { run: { font: 'Arial', size: 22 } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 30, bold: true, font: 'Arial', color: GREEN }, paragraph: { spacing: { before: 360, after: 160 }, outlineLevel: 0 } },
    ],
  },
  numbering: {
    config: [{ reference: 'bullets', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] }],
  },
  sections: [{
    properties: { page: { size: { width: PAGE_WIDTH, height: 16838 }, margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new TextRun({ text: `${BRAND} · what it does`, size: 16, color: '5F6B73' })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: ['Page ', PageNumber.CURRENT], size: 16, color: '5F6B73' })] })] }) },
    children,
  }],
});

const out = path.join(__dirname, `${BRAND} - what it does.docx`);
Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync(out, buffer);
  console.log(`Written: ${out}`);
});
