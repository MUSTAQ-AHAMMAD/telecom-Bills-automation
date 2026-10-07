import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from .config import settings, get_carrier_settings, get_account_credentials, CarrierSettings
from .file_organizer import invoice_file_path, find_existing_invoice_for_month
from .jobs import Job
from .models import Account
from .data_store import save_accounts_for_account_slot
from .oracle_sync import record_invoice_amounts
from . import live_control


def _bucket_status(raw_status: str) -> str:
    """Normalizes the portal's raw status text into Paid / Unpaid (merged with Overdue)."""
    normalized = raw_status.strip().lower()
    if "paid" in normalized and "unpaid" not in normalized:
        return "Paid"
    return "Unpaid"


async def _text_or_none(row, selector: str) -> Optional[str]:
    el = await row.query_selector(selector)
    if not el:
        return None
    text = await el.inner_text()
    return text.strip()


def _parse_relative_days(text: str) -> Optional[int]:
    """Extracts the number from strings like 'Issued 21 Days ago' or 'Next bill after 10 days'."""
    match = re.search(r"(\d+)\s*days?", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


async def _scrape_stc_row(row, entity: str, account_slot: int) -> Account:
    """
    STC's billing accounts page is a div-based infinite-scroll list, not a
    <table> — confirmed against the real portal on 2026-09-14. Each item:

        <div class="billing-account-list-item-container">
          <div class="billing-account-list-item-container__item">
            <div class="account-list-item-container">
              <p class="billing-account-list-item-container__item__account-id">37001899273</p>
              <div class="circle --paid"></div>
              <p class="lastBillStatus --paid">Paid</p>
            </div>
            <p class="billing-account-list-item-container__item__balance">SAR 5,968.50</p>
            <div class="billing-item-details-container">
              <div>...</div>
              <div>Next bill after 10 days</div>  <!-- or "Issued 21 Days ago" -->
            </div>
          </div>
        </div>

    There's no machine-readable due date anywhere in the list — only this
    relative-time text — so due_date is approximated from it. Status is
    Paid/Unpaid only (Unpaid+Overdue merged) — the day count still shows up
    in status_detail so severity isn't lost, just not a separate bucket.
    """
    account_number = await _text_or_none(row, ".billing-account-list-item-container__item__account-id") or "UNKNOWN"
    raw_status = await _text_or_none(row, ".lastBillStatus") or "Unpaid"
    amount_text = await _text_or_none(row, ".billing-account-list-item-container__item__balance") or "0"
    detail_text = await _text_or_none(row, ".billing-item-details-container") or ""

    amount = float("".join(ch for ch in amount_text if ch.isdigit() or ch == "."))
    is_paid = "paid" in raw_status.lower() and "unpaid" not in raw_status.lower()
    days = _parse_relative_days(detail_text)
    today = datetime.now()

    if is_paid:
        status = "Paid"
        due_date = (today + timedelta(days=days)) if days is not None else today
    else:
        status = "Unpaid"
        due_date = (today - timedelta(days=days)) if days is not None else today

    return Account(
        account_number=account_number,
        entity=entity,
        provider="STC",
        account_slot=account_slot,
        status=status,
        status_detail=detail_text or raw_status,
        amount=amount,
        due_date=due_date.strftime("%Y-%m-%d"),
    )


async def _scrape_row(row, entity: str, provider: str, account_slot: int) -> Account:
    """
    Extracts one account's data from a row element.

    NOTE: the child selectors below (`.account-number`, `.status`, `.amount`,
    `.due-date`) are placeholders for carriers other than STC. Inspect the
    real portal's markup with devtools and update them to match (see
    `_scrape_stc_row` above for a worked example against STC's real markup).
    """
    if provider == "STC":
        return await _scrape_stc_row(row, entity, account_slot)

    account_number = await _text_or_none(row, ".account-number") or "UNKNOWN"
    raw_status = await _text_or_none(row, ".status") or "Unpaid"
    amount_text = await _text_or_none(row, ".amount") or "0"
    due_date = await _text_or_none(row, ".due-date") or datetime.now().strftime("%Y-%m-%d")

    amount = float("".join(ch for ch in amount_text if ch.isdigit() or ch == "."))
    status = _bucket_status(raw_status)

    return Account(
        account_number=account_number,
        entity=entity,
        provider=provider,
        account_slot=account_slot,
        status=status,
        status_detail=raw_status,
        amount=amount,
        due_date=due_date,
    )


async def _load_all_rows(page, container_selector: str, row_selector: str, max_rounds: int = 40) -> None:
    """
    STC's (and possibly other carriers') accounts list only renders an
    initial batch of rows (confirmed ~10) inside a virtualized/infinite-
    scroll container, loading more as it's scrolled — without this, any
    accounts beyond that first batch are silently missed. Repeatedly
    scrolls the container to its bottom until the row count stops growing.
    """
    if not container_selector:
        return

    previous_count = -1
    stable_rounds = 0
    for _ in range(max_rounds):
        current_count = len(await page.query_selector_all(row_selector))
        if current_count == previous_count:
            stable_rounds += 1
            if stable_rounds >= 2:  # two consecutive no-growth scrolls = truly done
                return
        else:
            stable_rounds = 0
        previous_count = current_count

        scrolled = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.scrollTop = el.scrollHeight;
                return true;
            }""",
            container_selector,
        )
        if not scrolled:
            return
        await page.wait_for_timeout(700)


async def _wait_for_selector_optional(page, selector: str, timeout: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False


def _month_label_to_folder(label: str) -> str:
    """Turns a portal label like 'July, 2026' into a folder name 'July_2026'."""
    return re.sub(r"[,\s]+", "_", label.strip()).strip("_")


# Hard ceiling on how many billing cycles we'll ever page through per
# account, regardless of STC_MONTHS_TO_FETCH, so a markup change that keeps
# the "Previous bill" button perpetually enabled can't loop forever.
STC_MAX_MONTHS_HARD_CAP = 24


async def _open_dropdown_and_capture_download(page, carrier_settings: CarrierSettings, dest_path: str) -> Optional[str]:
    """
    Confirmed against the real portal on 2026-09-14: clicking the "Download
    bill" toggle opens a dropdown menu; clicking a menu item (e.g. "View Tax
    invoice") does NOT download or open a new tab — it navigates the same
    modal to an in-page PDF preview screen (rendered via react-pdf/canvas),
    which has its own explicit `.billing-preview-pdf-screen-download-btn`
    ("Download PDF") button that's what actually triggers the file save.

    After downloading, clicks the preview screen's back button to return to
    the bill-detail view, so month pagination (clicking "Previous bill")
    still works for the next iteration.
    """
    try:
        await page.click(carrier_settings.selector_invoice_download_button, timeout=10000)
        menu_item = page.locator(
            ".ant-dropdown-menu-item",
            has_text=re.compile(carrier_settings.selector_invoice_menu_item_text, re.IGNORECASE),
        ).first
        await menu_item.wait_for(state="visible", timeout=5000)
        await menu_item.click()

        download_btn = page.locator(carrier_settings.selector_invoice_download_confirm_button).first
        await download_btn.wait_for(state="visible", timeout=10000)

        async with page.expect_download(timeout=15000) as download_info:
            await download_btn.click()
        download = await download_info.value
        await download.save_as(dest_path)
        return dest_path
    except Exception:  # noqa: BLE001 - a download hiccup on one month shouldn't fail the whole scrape
        return None
    finally:
        # Best-effort: leave the PDF preview screen so month pagination /
        # the next account's row-click still works.
        try:
            back_btn = page.locator(".billing-shared-content-card-header-back-btn").first
            if await back_btn.count() > 0:
                await back_btn.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass


async def _download_invoice_stc(page, row, account: Account, carrier_settings: CarrierSettings) -> Optional[str]:
    """
    Opens STC's per-account bill detail modal and downloads the current
    bill's invoice. If STC_SELECTOR_PREV_MONTH_BUTTON and
    STC_MONTHS_TO_FETCH > 1 are configured, also walks backward through
    previous billing cycles via the "Previous bill" arrow, downloading each
    one into its own real Month_Year folder (read from the portal's own
    "Selected bill: <Month, Year>" label) before stopping at the configured
    depth or whenever that button becomes disabled (no more history).

    Returns the path of the current (first) month's invoice — the one shown
    by the dashboard's own Download Invoice button. Any earlier months are
    downloaded straight to disk only; they aren't separate dashboard rows.
    """
    first_invoice_path: Optional[str] = None
    try:
        await row.click()
        await page.wait_for_selector(carrier_settings.selector_invoice_download_button, timeout=10000)

        months_to_fetch = max(1, min(carrier_settings.months_to_fetch, STC_MAX_MONTHS_HARD_CAP))
        can_paginate = bool(carrier_settings.selector_prev_month_button)

        for month_index in range(months_to_fetch):
            month_folder = None
            if carrier_settings.selector_month_label:
                label_text = await _text_or_none(page, carrier_settings.selector_month_label)
                if label_text:
                    month_folder = _month_label_to_folder(label_text)

            status_text = await _text_or_none(page, ".billing-bill-status-value")
            if status_text:
                status = _bucket_status(status_text)
            else:
                status = account.status

            # Skip re-downloading a month that's already archived on disk
            # from a previous refresh — a bill's status folder can change
            # (Unpaid -> Paid) but the underlying document doesn't, so check
            # every status folder for this entity+carrier+account+month.
            existing_path = (
                find_existing_invoice_for_month(account.entity, account.provider, account.account_number, month_folder)
                if month_folder else None
            )

            if existing_path:
                downloaded = existing_path
            else:
                dest_path = invoice_file_path(
                    entity=account.entity,
                    provider=account.provider,
                    account_number=account.account_number,
                    due_date=account.due_date,
                    status=status,
                    filename=f"{account.account_number}_{month_folder or account.due_date}.pdf",
                    month_folder=month_folder,
                )
                downloaded = await _open_dropdown_and_capture_download(page, carrier_settings, dest_path)

            if month_index == 0:
                first_invoice_path = downloaded

            if not can_paginate or month_index == months_to_fetch - 1:
                break

            prev_button = page.locator(carrier_settings.selector_prev_month_button).first
            if await prev_button.get_attribute("disabled") is not None:
                break
            try:
                await prev_button.click(timeout=5000)
                await page.wait_for_timeout(1200)  # let the panel re-render with the new month's data
            except PlaywrightTimeoutError:
                break

    except Exception:  # noqa: BLE001 - a download hiccup on one row shouldn't fail the whole scrape
        pass
    finally:
        # Best-effort close of the detail modal so the next row is clickable.
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

    return first_invoice_path


async def _download_invoice(page, row, account: Account, carrier_settings: CarrierSettings) -> Optional[str]:
    if account.provider == "STC":
        return await _download_invoice_stc(page, row, account, carrier_settings)

    button = await row.query_selector(carrier_settings.selector_invoice_download_button)
    if not button:
        return None
    try:
        async with page.expect_download(timeout=30000) as download_info:
            await button.click()
        download = await download_info.value
        dest_path = invoice_file_path(
            entity=account.entity,
            provider=account.provider,
            account_number=account.account_number,
            due_date=account.due_date,
            status=account.status,
            filename=f"{account.account_number}_{account.due_date}.pdf",
        )
        await download.save_as(dest_path)
        return dest_path
    except PlaywrightTimeoutError:
        return None


async def run_scrape_job(job: Job, entity: str, provider: str, account_slot: int) -> None:
    job.status = "running"
    job.message = f"Launching browser for {entity} / {provider} (account {account_slot})..."
    carrier_settings = get_carrier_settings(provider)
    credentials = get_account_credentials(entity, provider, account_slot)

    if not credentials.username:
        job.status = "failed"
        job.message = (
            f"No credentials configured for {entity} / {provider} account slot {account_slot} "
            f"(set {entity.upper()}_{provider.upper()}_ACCOUNT_{account_slot}_USERNAME / _PASSWORD in .env)."
        )
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.headless)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()
            await live_control.register_page(job.job_id, page)

            try:
                job.message = f"Navigating to {provider} portal..."
                await page.goto(carrier_settings.portal_url, timeout=30000)

                job.message = f"Logging in to {provider} for {entity} ({credentials.label or f'Account {account_slot}'})..."
                await page.fill(carrier_settings.selector_username_input, credentials.username)
                await page.fill(carrier_settings.selector_password_input, credentials.password)
                await page.click(carrier_settings.selector_login_button)

                otp_appeared = await _wait_for_selector_optional(
                    page, carrier_settings.selector_otp_input, timeout=15000
                )
                if otp_appeared:
                    otp = await job.wait_for_otp(settings.otp_timeout_seconds)
                    await page.fill(carrier_settings.selector_otp_input, otp)
                    await page.click(carrier_settings.selector_otp_submit_button)

                job.status = "running"

                if carrier_settings.selector_post_login_dismiss_button:
                    await _wait_for_selector_optional(
                        page, carrier_settings.selector_post_login_dismiss_button, timeout=8000
                    )
                    try:
                        await page.click(carrier_settings.selector_post_login_dismiss_button, timeout=3000)
                    except PlaywrightTimeoutError:
                        pass  # no ad/interstitial this time — nothing to dismiss

                job.message = f"Logged in to {provider}. Navigating to billing accounts..."

                if carrier_settings.selector_nav_menu_item:
                    try:
                        await page.click(carrier_settings.selector_nav_menu_item, timeout=10000)
                        if carrier_settings.selector_nav_submenu_item:
                            await page.click(carrier_settings.selector_nav_submenu_item, timeout=10000)
                    except PlaywrightTimeoutError:
                        # Best-effort: maybe login already landed on the right
                        # page and this nav item genuinely isn't there/needed.
                        pass

                job.message = f"Logged in to {provider}. Loading billing accounts..."

                try:
                    await page.wait_for_selector(carrier_settings.selector_accounts_table_row, timeout=45000)
                except PlaywrightTimeoutError:
                    # A bare timeout doesn't say what's actually on screen (a WAF
                    # block page? an infinite loading spinner? a cookie banner
                    # covering the list?) — capture that so it's diagnosable
                    # instead of needing another blind round-trip.
                    diag_title = await page.title()
                    diag_text = (await page.evaluate("document.body.innerText.slice(0, 400)")).strip()
                    raise RuntimeError(
                        f"Accounts list never appeared (selector: {carrier_settings.selector_accounts_table_row!r}). "
                        f"Page title: {diag_title!r}. Page text: {diag_text!r}"
                    )
                job.message = f"Loading full {provider} accounts list..."
                await _load_all_rows(page, carrier_settings.selector_scroll_container, carrier_settings.selector_accounts_table_row)
                rows = await page.query_selector_all(carrier_settings.selector_accounts_table_row)

                accounts: List[Account] = []
                for row in rows:
                    accounts.append(await _scrape_row(row, entity, provider, account_slot))

                job.message = f"Downloading {provider} invoices for {entity}..."
                for account, row in zip(accounts, rows):
                    dest_path = await _download_invoice(page, row, account, carrier_settings)
                    account.invoice_path = dest_path

                await browser.close()
            finally:
                await live_control.unregister(job.job_id)

        save_accounts_for_account_slot(entity, provider, account_slot, accounts)
        record_invoice_amounts(accounts)
        job.accounts = accounts
        job.status = "completed"
        job.message = f"Scraped {len(accounts)} account(s) for {entity} / {provider} ({credentials.label or f'Account {account_slot}'})."

    except asyncio.TimeoutError:
        job.status = "failed"
        job.message = job.message or "Timed out waiting for OTP."
    except PlaywrightTimeoutError as exc:
        job.status = "failed"
        job.message = f"Timed out waiting for a portal element: {exc}"
    except Exception as exc:  # noqa: BLE001 - surface any automation failure to the UI
        job.status = "failed"
        # Include the exception type name too: some exceptions (e.g.
        # asyncio.CancelledError, raised when --reload restarts the server
        # mid-job) stringify to an empty message and would otherwise show
        # up as a bare "Automation failed:" with no useful detail.
        job.message = f"Automation failed: {type(exc).__name__}: {exc}" if str(exc) else f"Automation failed: {type(exc).__name__}"
