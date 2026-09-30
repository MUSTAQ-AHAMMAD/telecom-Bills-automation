# Telecom Bills Automation Dashboard

A single-page dashboard that automates logging into telecom billing portals
(STC, Mobily, Zain — with support for multiple real-world accounts per
brand, e.g. three separate STC business logins), scrapes account/invoice
data, downloads invoices into an organized folder structure, and shows
brand- and account-segregated live analytics.

- **Backend:** FastAPI + Playwright (async), in `backend/`
- **Frontend:** Single-page HTML/JS + Tailwind CDN (light theme), in
  `frontend/`, served directly by the FastAPI app. No other external script
  dependency — the brand comparison chart is a self-contained CSS bar chart,
  so nothing breaks if a CDN is unreachable.

## Brand + account segregation

Each brand (STC / Mobily / Zain) shares one portal URL and one set of CSS
selectors (`STC_*`, `MOBILY_*`, `ZAIN_*` in `.env`), but can have **multiple
independent logins** under it — e.g. three separate STC business accounts,
each with its own username/password. Each login is a numbered "account
slot" (`STC_ACCOUNT_1_*`, `STC_ACCOUNT_2_*`, `STC_ACCOUNT_3_*`, ...); the
backend auto-discovers however many slots have a username configured.

The frontend has two tab layers:

- **Brand tabs** (**All / STC / Mobily / Zain**) — selecting one filters the
  analytics cards and the accounts table to that brand, tints the active tab
  and the **Refresh** button in that brand's color.
- **Account sub-tabs** — appear under a brand tab whenever it has more than
  one configured account slot (e.g. under STC: "All STC Accounts",
  "Account 1", "Account 2", "Account 3"). Selecting one:
  - Filters the analytics cards and table down to just that account's data.
  - Relabels **Refresh** to **"Refresh `<Brand>` – `<Account Label>`"** and,
    when clicked, runs the automation against *only that one login* — it
    logs in with that slot's credentials and does not touch the other
    accounts' cached data.
- Leaving a brand tab on "All `<Brand>` Accounts" and clicking Refresh
  queues every configured account under that brand, one after another.
  Selecting the **All** brand tab and clicking Refresh queues every account
  across every brand. Only one Playwright job runs at a time; the button
  and status banner show progress as each one completes.
- Per-brand analytics cards and a grouped bar chart (three bars per brand —
  Paid / Unpaid / Overdue, amount printed above each bar) are always
  visible regardless of which tab is active, for at-a-glance comparison
  across brands (aggregated across all of a brand's accounts).
- The accounts table also has its own toolbar: free-text search by account
  number and a **Status** dropdown, plus pagination (5/10/25/50 per page).
  Each row shows which account slot it came from underneath the account
  number.
- **Download All Invoices** respects the active brand tab — it zips just
  that brand's folder, or everything under `DOWNLOAD_ROOT` when "All" is
  active.

## How the OTP flow works

The refresh automation runs as an `asyncio` background task on the server,
scoped to one brand + account slot (`run_scrape_job(job, provider,
account_slot)`). When that login's portal shows an OTP field after
submitting username/password, the job's status flips to `waiting_otp` and
the coroutine suspends on an `asyncio.Event`. The frontend polls
`GET /api/jobs/{job_id}` every 2 seconds; when it sees `waiting_otp` it
opens the OTP modal, titled with the brand and account label. Submitting the
modal calls `POST /api/jobs/{job_id}/otp`, which sets the event and unblocks
the automation coroutine so it can type the OTP into the page and continue.
If no OTP is submitted within `OTP_TIMEOUT_SECONDS` (default 180s), the job
fails.

## Folder structure produced

```
<DOWNLOAD_ROOT>/
  STC/
    <Account Number>/
      <Month_Year>/          e.g. August_2026
        Bills/
          Paid/
          Unpaid/
          Overdue/
  Mobily/
    ... same structure ...
  Zain/
    ... same structure ...
```

The top-level folder is set from the brand a refresh job targeted; the
`<Account Number>` folder comes from whatever account number that specific
login's portal actually shows (which account slot logged in to produce it
is tracked internally for correct caching, but doesn't change the folder
layout).

### STC: real markup, month-history downloads, and known caveats

STC's billing accounts page (confirmed 2026-09-14) is **not a `<table>`** —
it's a div-based infinite-scroll list, and there's no due date anywhere in
it, only relative text like "Issued 21 Days ago" or "Next bill after 10
days". `_scrape_stc_row()` in `app/automation.py` parses this directly:
Unpaid bills issued `STC_OVERDUE_THRESHOLD_DAYS` (default 15) days ago or
more are bucketed as **Overdue** rather than Unpaid.

There's also no inline download button per row — clicking an account row
opens a bill-detail modal with a "Download bill" button that opens a
dropdown menu (View bill PDF summary / View bill PDF details / **View Tax
invoice** / Multiple bills download). The modal also has a bill-cycle
selector ("Selected bill: `<Month, Year>`" with Previous/Next arrow
buttons) for browsing past billing cycles. Set `STC_MONTHS_TO_FETCH` in
`.env` (default 3) to walk backward through that many previous months per
account, each downloaded into its own real `Month_Year` folder read
straight from that label — set it to `1` to only fetch the current bill.

**Known caveat**: I could inspect the dropdown menu's markup (confirming
`STC_SELECTOR_INVOICE_MENU_ITEM_TEXT=Tax invoice` is the right item to
click) but not what happens *after* clicking it — a real file download, a
new tab pointing at the PDF, or something else. `_open_dropdown_and_capture_download()`
handles the first two cases (races a Playwright `download` event against a
new tab opening, and if it's a new tab, fetches that URL's bytes using the
same authenticated session). If invoices still don't download after
selectors are otherwise working, open DevTools' **Network** tab on the real
portal, click "View Tax invoice", and share the resulting request URL/type
so this can be tightened to hit that exact endpoint.

## Setup

### 1. Backend

```bash
cd backend
python -m venv venv
```

Activate the virtual environment, then install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

Copy the environment template and fill in real values:

```bash
cp .env.example .env
```

Edit `backend/.env` — it has two layers per brand:

- **Shared portal config**: `<BRAND>_PORTAL_URL` and `<BRAND>_SELECTOR_*`
  (login form, OTP field, accounts table rows, invoice download button).
  For **STC**, the login page URL and the username/password/login-button
  selectors in `.env.example` have already been confirmed against the real
  portal (`https://business.stc.com.sa/content/cxp-wp/sa/en/home/login.html`
  — note this differs from the `/login` URL you might guess). Its login
  form is a React app with no `id`/`name`/`class` on the username/password
  fields at all, so they're targeted positionally: `input[type="text"]` and
  `input[type="password"]` (there's only one of each on that page). Mobily
  and Zain still ship placeholder selectors — inspect those portals the
  same way (see below).

  The **OTP field and the accounts table only exist after a real login**,
  so nobody but you (with real credentials) can discover those selectors.
  To find them:
  1. Set `HEADLESS=false` in `.env` and run a refresh for that account.
  2. When the visible browser window reaches the OTP screen, right-click
     the OTP input → **Inspect** → note its `id`/`class`/`name` and update
     `<BRAND>_SELECTOR_OTP_INPUT` (and the submit button the same way).
  3. Enter the OTP in the automation's own modal in the dashboard (not in
     the Playwright browser window) to let it continue past login.
  4. Once on the accounts page, inspect one table row and its cells the
     same way, and update `<BRAND>_SELECTOR_ACCOUNTS_TABLE_ROW` plus the
     child selectors `.account-number`, `.status`, `.amount`, `.due-date`
     hardcoded in `_scrape_row()` in `app/automation.py` to match.
- **Per-account logins**: `<BRAND>_ACCOUNT_<N>_LABEL` /
  `_USERNAME` / `_PASSWORD` for each real account under that brand (STC
  ships 3 in the example, Mobily/Zain ship 1 — add more the same way,
  `_ACCOUNT_2_*`, `_ACCOUNT_3_*`, etc., for any brand). Leave a slot's
  `_USERNAME` blank to skip it — the backend only lists slots with a
  username set. **Never commit `.env`** (it's already in `.gitignore`).
- `DOWNLOAD_ROOT`, `HEADLESS`, `OTP_TIMEOUT_SECONDS` — global settings that
  apply to every brand and account.
- `HEADLESS=false` while you're first wiring up selectors, so you can watch
  the browser and confirm each step; switch back to `true` for normal use.

### 2. Run

```bash
cd backend
uvicorn app.main:app --port 8000
```

**On Windows, do not add `--reload`.** uvicorn switches to `SelectorEventLoop`
instead of `ProactorEventLoop` whenever `--reload` (or `--workers > 1`) is
used, because its file-watcher needs it — but `SelectorEventLoop` cannot spawn
subprocesses, and that's exactly how Playwright launches the browser. With
`--reload` on, every refresh fails with `Automation failed: NotImplementedError`.
Plain `uvicorn app.main:app --port 8000` (no `--reload`) gets
`ProactorEventLoop` automatically and works correctly. After changing backend
code, stop (Ctrl+C) and restart the server manually instead.

Open **http://localhost:8000** — this serves the dashboard directly (the
FastAPI app mounts `frontend/` as static files), so there's no separate
frontend server to run.

## Using the dashboard

1. Pick a brand tab (or leave **All** selected). If that brand has more
   than one configured account, pick an account sub-tab too (or leave "All
   `<Brand>` Accounts" selected).
2. Click **Refresh**. The button shows a spinner while the backend logs
   into that specific account's portal.
3. If the portal challenges with an OTP, a modal appears (naming the brand
   and account) asking for the code sent to your phone. Enter it and
   submit — the automation resumes.
4. Once the job completes, the analytics cards, per-brand cards, comparison
   chart, and the accounts table update automatically. If refreshing "All
   `<Brand>` Accounts" or the "All" brand tab, the next account in the
   queue starts automatically.
5. Each row's **Download Invoice** button re-downloads that account's PDF
   from the organized folder on the server. **Download All Invoices** zips
   the active brand's folder (or everything, under "All").

## Security notes

- Credentials live only in `backend/.env`, read via `pydantic-settings`
  (one isolated settings instance per brand, and one per account slot), and
  are never sent to the frontend or logged.
- `.env`, the downloaded invoices, and the accounts cache are all
  git-ignored.
- The backend only ever launches a browser locally against the URLs you
  configure — treat every `<BRAND>_ACCOUNT_<N>_PASSWORD` with the same care
  you'd give any stored password (consider a secrets manager for production
  use instead of a plain `.env` file).

## Known limitations / next steps

- Selectors in `.env.example` and `app/automation.py` are placeholders and
  **will not work against a real portal out of the box** — every telecom
  portal's markup differs, so this needs a short calibration pass per brand
  with devtools open against the real site.
- Only one refresh job runs at a time across all brands/accounts (enforced
  by `JobManager`); an "All accounts" queue runs them sequentially rather
  than in parallel.
- `_scrape_row()` in `app/automation.py` uses one shared set of child
  selectors for the accounts table across all three brands — if their table
  markup differs, branch that function on `provider`.
- Account slots are discovered by scanning `_ACCOUNT_1_USERNAME` through
  `_ACCOUNT_10_USERNAME` (see `MAX_ACCOUNT_SLOTS` in `app/config.py`) —
  raise that constant if a brand ever needs more than 10 accounts.
