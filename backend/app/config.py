import json
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


class OracleSettings(BaseSettings):
    """
    Oracle Fusion Cloud Procurement integration (see app/oracle_sync.py),
    loaded from ORACLE_* env vars. ID lookup tables (Business Units,
    Suppliers/Sites, GL charge accounts) live in the JSON file at
    `mappings_path` rather than here, since they're nested per entity/carrier.
    """

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", env_prefix="ORACLE_")

    base_url: str = ""  # e.g. https://xxxx.fa.em2.oraclecloud.com
    api_version: str = "11.13.18.05"
    username: str = ""
    password: str = ""
    timeout_seconds: int = 60

    # Safety switch: while false, a sync only builds and returns the payloads
    # it *would* send — nothing is created or submitted in Oracle.
    live_mode: bool = False
    # Submit each requisition for approval after creating + attaching it.
    auto_submit: bool = True

    preparer_email: str = ""  # PreparerEmail on the requisition header
    requester_email: str = ""  # RequesterEmail on lines; defaults to preparer_email when blank
    preparer_id: str = ""  # Person ID of the system integration user (if the payload template uses IDs)
    requester_id: str = ""  # defaults to preparer_id when blank
    line_type: str = "ORA_Rate Based Services"
    item_number: str = ""  # Oracle Item on the requisition line ({item})
    quantity: float = 1  # line / distribution Quantity ({quantity})
    category_name: str = "IT - Mobile & Internet"
    need_by_days: int = 2  # {need_by_date} = invoice date + this many days (RequestedDeliveryDate)
    currency_code: str = "SAR"
    # Oracle attachment category CODE (not its display name): Oracle answers 201
    # for an unknown one but drops the file. Valid on requisition lines:
    # REQ_INTERNAL, TO_SUPPLIER, TO_BUYER, TO_APPROVER, MISC.
    attachment_category: str = "REQ_INTERNAL"

    mappings_path: str = os.path.join(os.path.dirname(__file__), "..", "data", "oracle_mappings.json")

    # Monthly trigger: on/after this day of the month, a background check
    # syncs that month's Unpaid invoices once. 0 disables the schedule
    # (sync is then only run via POST /api/oracle/sync).
    schedule_day_of_month: int = 0

    # Procurement Administrator alerts (unmapped cost centers, failures).
    # Alerts are always stored and listed at GET /api/oracle/alerts; email
    # is only sent when SMTP is configured.
    admin_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True


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

# Oracle settings edited from the dashboard are saved here and take
# precedence over .env, so the integration can be configured without
# editing files or restarting the server.
ORACLE_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "oracle_settings.json")


def _load_oracle_overrides() -> dict:
    if not os.path.isfile(ORACLE_SETTINGS_PATH):
        return {}
    with open(ORACLE_SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_oracle_settings(changes: dict) -> None:
    """
    Validates and persists dashboard edits, then applies them to the shared
    `oracle_settings` object in place (other modules hold a reference to it).
    Raises pydantic.ValidationError on a bad value.
    """
    allowed = set(OracleSettings.model_fields) - {"mappings_path"}
    overrides = _load_oracle_overrides()
    overrides.update({k: v for k, v in changes.items() if k in allowed})
    if overrides.get("base_url"):
        # Keep just the pod host if a full REST endpoint was pasted.
        host = overrides["base_url"].strip().split("/fscmRestApi")[0].rstrip("/")
        overrides["base_url"] = host[:-4] if host.startswith("https://") and host.endswith(":443") else host
    validated = OracleSettings(**overrides)
    os.makedirs(os.path.dirname(ORACLE_SETTINGS_PATH), exist_ok=True)
    with open(ORACLE_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({k: getattr(validated, k) for k in overrides}, f, indent=2)
    for field in OracleSettings.model_fields:
        setattr(oracle_settings, field, getattr(validated, field))


oracle_settings = OracleSettings(**_load_oracle_overrides())
