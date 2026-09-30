import json
import os
from typing import List

from .models import Account

STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")


def save_accounts(accounts: List[Account]) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump([a.model_dump() for a in accounts], f, indent=2)


def clear_accounts() -> None:
    """Wipes the latest-scrape snapshot (the Dashboard's cards/table) back to empty."""
    save_accounts([])


def save_accounts_for_account_slot(entity: str, provider: str, account_slot: int, new_accounts: List[Account]) -> None:
    """Replaces only one entity+carrier+login-slot's accounts, leaving all other cached data intact."""
    existing = [
        a for a in load_accounts()
        if not (a.entity == entity and a.provider == provider and a.account_slot == account_slot)
    ]
    save_accounts(existing + new_accounts)


def load_accounts() -> List[Account]:
    if not os.path.exists(STORE_PATH):
        return []
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    accounts = [Account(**item) for item in raw]
    # Legacy cached data from before Overdue was merged into Unpaid can still
    # have status="Overdue" sitting in accounts.json — normalize it here so
    # it matches the "Unpaid" filter/bucket everywhere else, same as the
    # legacy Overdue folders on disk (see file_organizer.py).
    for account in accounts:
        if account.status != "Paid":
            account.status = "Unpaid"
    return accounts
