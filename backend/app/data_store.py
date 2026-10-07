import json
import os
from typing import Dict, List, Optional

from .models import Account

STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "accounts.json")
# Which login slot each account number was scraped under. Kept separately
# from accounts.json because "Clear Data" wipes that snapshot, while the
# invoice archive still needs to know e.g. "this STC number is Account 2".
SLOT_MAP_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "account_slots.json")


def _slot_key(entity: str, provider: str, account_number: str) -> str:
    return f"{entity}|{provider}|{account_number}"


def _load_slot_map() -> Dict[str, int]:
    if not os.path.exists(SLOT_MAP_PATH):
        return {}
    with open(SLOT_MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _remember_account_slots(accounts: List[Account]) -> None:
    slot_map = _load_slot_map()
    for a in accounts:
        slot_map[_slot_key(a.entity, a.provider, a.account_number)] = a.account_slot
    os.makedirs(os.path.dirname(SLOT_MAP_PATH), exist_ok=True)
    with open(SLOT_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(slot_map, f, indent=2, sort_keys=True)


def account_slot_index() -> Dict[str, int]:
    """
    "entity|provider|account_number" -> login slot, from the persisted map
    plus the current accounts.json (which backfills accounts scraped before
    the map existed).
    """
    index = {_slot_key(a.entity, a.provider, a.account_number): a.account_slot for a in load_accounts()}
    index.update(_load_slot_map())
    return index


def lookup_account_slot(index: Dict[str, int], entity: str, provider: str, account_number: str) -> Optional[int]:
    return index.get(_slot_key(entity, provider, account_number))


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
    _remember_account_slots(new_accounts)


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
