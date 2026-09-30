import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file (backend/app/config.py -> backend/.env)
# rather than the process's current working directory, since that varies
# depending on how uvicorn is launched (e.g. `--app-dir backend` from the
# project root vs `cd backend && uvicorn ...`).
ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

# Entities are the business units the dashboard is organized around; each
# one has its own independent set of logins across every carrier below (its
# own STC account, its own Mobily account, its own Zain account, ...).
ENTITY_NAMES = ["Ibraq", "Match", "Salfa", "Feelin"]

# Carriers are the telecom portals themselves — shared config (portal URL,
# CSS selectors) lives here since the portal's markup doesn't change per
# entity, only the login credentials used against it do.
CARRIER_NAMES = ["STC", "Mobily", "Zain"]

# Highest account slot number scanned per entity+carrier when discovering
# configured logins (IBRAQ_STC_ACCOUNT_1_*, IBRAQ_STC_ACCOUNT_2_*, ...).
MAX_ACCOUNT_SLOTS = 10

# Carrier accent colors used consistently across the API and mirrored in the
# frontend's CARRIER_THEME map.
CARRIER_COLORS = {
    "STC": "#6b21e8",
    "Mobily": "#00a651",
    "Zain": "#7c3aed",
}

# Entity accent colors, mirrored in the frontend's ENTITY_THEME map.
ENTITY_COLORS = {
    "Ibraq": "#2563eb",
    "Match": "#db2777",
    "Salfa": "#059669",
    "Feelin": "#d97706",
}


class Settings(BaseSettings):
    """Global, entity/carrier-agnostic settings."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    download_root: str = "./data/Root"
    headless: bool = True
    otp_timeout_seconds: int = 180

    # Path to the "TELECOMMUNICATION MASTER" spreadsheet (one sheet per
    # entity+carrier, mapping account numbers to real-world Location/Branch)
    # used by POST /api/master/import to (re)build the local master.db.
    master_excel_path: str = ""


class CarrierSettings(BaseSettings):
    """
    Per-carrier portal config shared by every entity's login against that
    carrier: the portal URL and CSS selectors. Loaded from env vars
    prefixed with the carrier name, e.g. STC_PORTAL_URL, STC_SELECTOR_OTP_INPUT
    — the same portal config applies no matter which entity logs in.
    """

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    portal_url: str = ""

    selector_username_input: str = "#username"
    selector_password_input: str = "#password"
    selector_login_button: str = 'button[type="submit"]'
    selector_otp_input: str = "#otp"
    selector_otp_submit_button: str = "#otp-submit"

    # Some portals show a promo/ad modal right after login, before the
    # normal dashboard is usable — if set, this close/dismiss button is
    # clicked (best-effort, short timeout, never fails the job if it
    # doesn't appear) right after login/OTP and before navigating to
    # billing. Leave blank for a portal with no such interstitial.
    selector_post_login_dismiss_button: str = ""

    selector_accounts_table_row: str = "table.accounts tbody tr"
    selector_invoice_download_button: str = "a.download-invoice"

    # If the accounts list is a virtualized/infinite-scroll component (only
    # renders ~10 rows at a time, loading more as you scroll), set this to
    # the scrollable container so all accounts get loaded before scraping.
    # Leave blank for a portal that renders every row immediately.
    selector_scroll_container: str = ""

    # Post-login navigation: some portals land on a Home/dashboard page after
    # login rather than the billing accounts list, requiring a sidebar/menu
    # click (and optionally a submenu click) to actually get there. Leave
    # both blank to skip navigation entirely (assume login already lands on
    # the accounts list).
    selector_nav_menu_item: str = ""
    selector_nav_submenu_item: str = ""

    # The "Download bill" button opens a dropdown menu of options (View PDF
    # summary/details, Tax invoice, Multiple bills download, ...); this is a
    # case-insensitive substring match against the menu item to click.
    selector_invoice_menu_item_text: str = "invoice"

    # Clicking that menu item navigates to an in-page PDF preview screen
    # (not a direct download or new tab) with its own explicit download
    # button — this is what actually triggers the file save.
    selector_invoice_download_confirm_button: str = 'button:has-text("Download")'

    # Optional month-cycle pagination (e.g. STC's "Selected bill: <Month, Year>"
    # panel with Previous/Next bill arrow buttons). Leave selector_prev_month_button
    # blank to disable pagination entirely (download only the current/latest bill).
    selector_prev_month_button: str = ""
    selector_month_label: str = ""
    months_to_fetch: int = 1


class AccountCredentials(BaseSettings):
    """
    One login under an entity+carrier, e.g. IBRAQ_STC_ACCOUNT_1_USERNAME /
    IBRAQ_STC_ACCOUNT_1_PASSWORD. An entity with multiple real-world
    accounts against the same carrier gets one of these per account.
    """

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    label: str = ""
    username: str = ""
    password: str = ""


@lru_cache
def get_carrier_settings(carrier: str) -> CarrierSettings:
    if carrier not in CARRIER_NAMES:
        raise ValueError(f"Unknown carrier '{carrier}'. Expected one of {CARRIER_NAMES}.")
    return CarrierSettings(_env_prefix=f"{carrier.upper()}_")


@lru_cache
def get_account_credentials(entity: str, carrier: str, slot: int) -> AccountCredentials:
    if entity not in ENTITY_NAMES:
        raise ValueError(f"Unknown entity '{entity}'. Expected one of {ENTITY_NAMES}.")
    if carrier not in CARRIER_NAMES:
        raise ValueError(f"Unknown carrier '{carrier}'. Expected one of {CARRIER_NAMES}.")
    return AccountCredentials(_env_prefix=f"{entity.upper()}_{carrier.upper()}_ACCOUNT_{slot}_")


def list_account_slots(entity: str, carrier: str) -> List[dict]:
    """
    Discovers which account slots have credentials configured for an
    entity+carrier pair, e.g. IBRAQ_STC_ACCOUNT_1_USERNAME,
    IBRAQ_STC_ACCOUNT_2_USERNAME => [{"slot": 1, "label": "..."}, {"slot": 2, "label": "..."}].
    Falls back to a single unconfigured slot 1 so the pair still has
    something to select and configure.
    """
    slots = []
    for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
        creds = get_account_credentials(entity, carrier, slot)
        if creds.username:
            slots.append({"slot": slot, "label": creds.label or f"Account {slot}"})
    if not slots:
        slots.append({"slot": 1, "label": "Account 1"})
    return slots


settings = Settings()
