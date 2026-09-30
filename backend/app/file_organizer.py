import os
import shutil
from datetime import datetime

from .config import settings


def status_folder_name(status: str) -> str:
    """
    Maps a scraped status to a bucket folder. Only Paid/Unpaid are created
    going forward — Overdue is merged into Unpaid (still conveyed via
    status_detail, e.g. "Issued 21 days ago") — but legacy `Overdue/`
    folders from before this merge are still read transparently elsewhere
    (see `list_archived_invoices` / `resolve_archived_invoice_path`).
    """
    normalized = status.strip().lower()
    if normalized == "paid":
        return "Paid"
    return "Unpaid"


def invoice_month_folder(due_date: str) -> str:
    """Turns a due date string into a `Month_Year` folder name, e.g. August_2026."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(due_date, fmt)
            return parsed.strftime("%B_%Y")
        except ValueError:
            continue
    # Fall back to the current month if the portal's date format is unrecognized
    return datetime.now().strftime("%B_%Y")


def build_invoice_dir(entity: str, provider: str, account_number: str, due_date: str, status: str, month_folder: str = None) -> str:
    """
    Builds (and creates) the folder:
    Root/<Entity>/<Carrier>/<Account Number>/<Month_Year>/Bills/<Paid|Unpaid>/

    Pass `month_folder` directly (e.g. "July_2026") when the portal already
    gives you an exact billing-cycle label, instead of `due_date` (which is
    otherwise parsed to derive the month — useful when only an approximate
    due date is known).
    """
    root = os.path.abspath(settings.download_root)
    folder = month_folder or invoice_month_folder(due_date)
    bucket = status_folder_name(status)

    target_dir = os.path.join(
        root, entity, provider, account_number, folder, "Bills", bucket
    )
    os.makedirs(target_dir, exist_ok=True)
    return target_dir


def invoice_file_path(entity: str, provider: str, account_number: str, due_date: str, status: str, filename: str, month_folder: str = None) -> str:
    target_dir = build_invoice_dir(entity, provider, account_number, due_date, status, month_folder=month_folder)
    return os.path.join(target_dir, filename)


def find_existing_invoice_for_month(entity: str, provider: str, account_number: str, month_folder: str) -> str:
    """
    Checks whether an invoice for this entity+carrier+account+month was
    already downloaded (in any status folder — a bill can move from Unpaid
    to Paid between refreshes without changing the underlying document).
    Used to skip re-downloading a month that's already archived on disk.
    """
    root = os.path.abspath(settings.download_root)
    bills_dir = os.path.join(root, entity, provider, account_number, month_folder, "Bills")
    if not os.path.isdir(bills_dir):
        return None
    for status_folder in sorted(os.listdir(bills_dir)):
        status_dir = os.path.join(bills_dir, status_folder)
        if not os.path.isdir(status_dir):
            continue
        for filename in sorted(os.listdir(status_dir)):
            if filename.lower().endswith(".pdf"):
                return os.path.join(status_dir, filename)
    return None


def _normalize_status_for_display(status_folder: str) -> str:
    """Legacy `Overdue/` folders (from before Unpaid+Overdue were merged) still display as Unpaid."""
    return "Unpaid" if status_folder != "Paid" else "Paid"


def list_archived_invoices(entity: str = None, provider: str = None, account_number: str = None, month: str = None) -> list:
    """
    Scans DOWNLOAD_ROOT directly for every downloaded invoice PDF, matching
    the Root/<Entity>/<Carrier>/<Account>/<Month_Year>/Bills/<Status>/*.pdf
    layout. This is the source of truth for "what invoices actually exist
    on disk" — independent of accounts.json — so it survives server
    restarts and surfaces every previously downloaded month, not just the
    latest scrape. Status is normalized to Paid/Unpaid (legacy Overdue
    folders are merged into Unpaid for display).
    """
    root = os.path.abspath(settings.download_root)
    if not os.path.isdir(root):
        return []

    results = []
    for ent in sorted(os.listdir(root)):
        if entity and ent != entity:
            continue
        entity_dir = os.path.join(root, ent)
        if not os.path.isdir(entity_dir):
            continue

        for prov in sorted(os.listdir(entity_dir)):
            if provider and prov != provider:
                continue
            prov_dir = os.path.join(entity_dir, prov)
            if not os.path.isdir(prov_dir):
                continue

            for acct in sorted(os.listdir(prov_dir)):
                if account_number and acct != account_number:
                    continue
                acct_dir = os.path.join(prov_dir, acct)
                if not os.path.isdir(acct_dir):
                    continue

                for month_folder in sorted(os.listdir(acct_dir)):
                    if month and month_folder != month:
                        continue
                    bills_dir = os.path.join(acct_dir, month_folder, "Bills")
                    if not os.path.isdir(bills_dir):
                        continue

                    for status_folder in sorted(os.listdir(bills_dir)):
                        status_dir = os.path.join(bills_dir, status_folder)
                        if not os.path.isdir(status_dir):
                            continue

                        for filename in sorted(os.listdir(status_dir)):
                            if not filename.lower().endswith(".pdf"):
                                continue
                            file_path = os.path.join(status_dir, filename)
                            results.append({
                                "entity": ent,
                                "provider": prov,
                                "account_number": acct,
                                "month": month_folder,
                                "status": _normalize_status_for_display(status_folder),
                                "filename": filename,
                                "size_bytes": os.path.getsize(file_path),
                                "modified_at": os.path.getmtime(file_path),
                            })
    return results


def resolve_archived_invoice_path(entity: str, provider: str, account_number: str, month: str, status: str, filename: str) -> str:
    """
    Rebuilds an invoice's path from its identifying parts and verifies it
    stays inside DOWNLOAD_ROOT and actually exists, guarding against path
    traversal (each part must not smuggle in `..`/separators). Since
    Unpaid now also covers legacy `Overdue/` folders on disk, a status of
    "Unpaid" tries both. Returns None if invalid or missing.
    """
    for part in (entity, provider, account_number, month, status, filename):
        if not part or "/" in part or "\\" in part or ".." in part:
            return None

    root = os.path.abspath(settings.download_root)
    candidate_statuses = [status] if status == "Paid" else [status, "Overdue"]
    for candidate_status in candidate_statuses:
        candidate = os.path.abspath(
            os.path.join(root, entity, provider, account_number, month, "Bills", candidate_status, filename)
        )
        if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
            return candidate
    return None


def clear_all_invoices() -> dict:
    """
    Permanently deletes every downloaded invoice under DOWNLOAD_ROOT (all
    entities/carriers/accounts/months) and recreates an empty root
    directory — this is what the Dashboard's "Clear Data" action calls.
    Does NOT touch accounts.json (the latest scrape snapshot) or master.db
    (the Location/Branch directory); only the invoice archive on disk.
    """
    root = os.path.abspath(settings.download_root)
    files_deleted = 0
    if os.path.isdir(root):
        for dirpath, _dirnames, filenames in os.walk(root):
            files_deleted += len(filenames)
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    return {"files_deleted": files_deleted}
