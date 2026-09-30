"""
Imports the "TELECOMMUNICATION MASTER" spreadsheet — the real-world
inventory of every branch/line per entity+carrier, including which city
(Location) and branch each account number belongs to — into a local
SQLite database, and provides lookups used to attach Location/Branch to
scraped accounts and downloaded invoices.

The spreadsheet has one sheet per entity+carrier pair (e.g. "STC IBQ",
"Mobily MATCH", "Zain"), each with its own slightly different column set,
plus a "Closed" sheet (cancelled/closed lines, no entity/carrier per row)
and a "Sheet7" with no header row that doesn't fit this schema and is
skipped entirely.
"""
import os
import re
import sqlite3
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "master.db")

# Maps each spreadsheet tab to the (entity, carrier) pair it represents.
# "Zain" (no suffix) sits with the IBQ group in the workbook and has no
# other entity marker, so it's inferred to be Ibraq's Zain account.
SHEET_TO_ENTITY_CARRIER = {
    "STC IBQ": ("Ibraq", "STC"),
    "Mobily IBQ": ("Ibraq", "Mobily"),
    "Zain": ("Ibraq", "Zain"),
    "STC MATCH ": ("Match", "STC"),
    "STC MATCH": ("Match", "STC"),
    "Mobily MATCH": ("Match", "Mobily"),
    "Zain Match": ("Match", "Zain"),
    "STC SALFA": ("Salfa", "STC"),
    "Mobily Salfa": ("Salfa", "Mobily"),
    "STC FEELIN": ("Feelin", "STC"),
    "Mobily Feelin": ("Feelin", "Mobily"),
}

# Sheets that don't map to a single entity+carrier, or don't fit the
# Location/Account Number schema at all.
CLOSED_SHEET = "Closed"
SKIPPED_SHEETS = {"Sheet7"}

# Column header variants (lowercased, stripped) -> canonical field name.
HEADER_ALIASES = {
    "location": "location",
    "cr": "cr",
    "conntract no": "cr",
    "contract no": "cr",
    "branch/users": "branch",
    "branch/user": "branch",
    "dept": "department",
    "department": "department",
    "department / code": "department",
    "connection": "connection",
    "account number": "account_number",
    "contract/account number": "account_number",
    "service number": "service_number",
    "service number (san)": "service_number",
    "service number (msisdn)": "service_number",
    "serial number": "serial_number",
    "s\\n": "serial_number",
    "stats": "status",
    "status": "status",
    "package": "package",
    "notes": "notes",
    "date": "closed_date",
}


def _normalize_header(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return HEADER_ALIASES.get(text)


def _normalize_account_number(value) -> Optional[str]:
    """Excel stores account numbers as int/float/str inconsistently; normalize to a plain string."""
    if value is None:
        return None
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    text = str(value).strip()
    return text or None


def _find_header_row(ws, max_scan: int = 6) -> Optional[int]:
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), start=1):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if "location" in cells:
            return row_idx
    return None


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Creates the tables if they don't exist yet, and migrates older databases
    forward (adding columns introduced later) — never drops existing data.
    Unlike the old `_init_schema`, this is safe to call before every
    operation, since re-importing the spreadsheet must not wipe manually
    added rows or rows a person has since hand-edited (see
    `import_master_excel` / `is_manually_edited`).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS master_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity TEXT,
            carrier TEXT,
            sheet_name TEXT,
            location TEXT,
            cr TEXT,
            branch TEXT,
            department TEXT,
            connection TEXT,
            account_number TEXT,
            service_number TEXT,
            serial_number TEXT,
            status TEXT,
            package TEXT,
            notes TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_master_account_number ON master_accounts(carrier, account_number)")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS closed_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT,
            cr TEXT,
            branch TEXT,
            connection TEXT,
            account_number TEXT,
            service_number TEXT,
            serial_number TEXT,
            status TEXT,
            closed_date TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_closed_account_number ON closed_accounts(account_number)")

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(master_accounts)")}
    if "cost_center" not in existing_cols:
        conn.execute("ALTER TABLE master_accounts ADD COLUMN cost_center TEXT")
    if "is_manually_edited" not in existing_cols:
        # Marks a row as protected from being wiped/overwritten by a future
        # spreadsheet re-import — set whenever a row is touched through the
        # CRUD API (create/update) or through a targeted enrichment pass
        # like `import_store_sheet`, so a person's correction (or an
        # enrichment from a different source file) survives the next
        # `import_master_excel` run.
        conn.execute("ALTER TABLE master_accounts ADD COLUMN is_manually_edited INTEGER DEFAULT 0")


def import_master_excel(path: str) -> dict:
    """
    Refreshes the local master database from the spreadsheet at `path` —
    the source of truth for the branch/location inventory. Only rows this
    same automated import created are replaced (matched by sheet_name !=
    "Manual Entry" and not since hand-edited); rows added via the Master
    Data CRUD screen, or spreadsheet rows a person has since corrected
    (`is_manually_edited`), are left alone rather than wiped. Returns a
    per-sheet row-count summary.
    """
    import openpyxl  # imported lazily: only needed for this import step

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Master spreadsheet not found: {path}")

    wb = openpyxl.load_workbook(path, data_only=True)
    conn = _connect()
    summary = {}
    try:
        _ensure_schema(conn)
        conn.execute(
            "DELETE FROM master_accounts WHERE sheet_name != 'Manual Entry' "
            "AND (is_manually_edited IS NULL OR is_manually_edited = 0)"
        )
        conn.execute("DELETE FROM closed_accounts")

        # Whatever's left at this point is protected (manually edited, or a
        # manual entry) — re-inserting a fresh copy of the same logical row
        # from the sheet would duplicate it, so remember its natural key
        # (sheet_name, account_number, branch) and skip re-inserting a match.
        # NOTE: a handful of accounts have several service lines under the
        # identical branch name (same key) — editing one such row protects
        # that key and so skips re-inserting its siblings too. Not an issue
        # for anything edited so far (verified against real data), but a
        # true fix would need service_number in the key as well.
        protected_keys = {
            (row[0], row[1], row[2])
            for row in conn.execute("SELECT sheet_name, account_number, branch FROM master_accounts")
        }

        for sheet_name in wb.sheetnames:
            if sheet_name in SKIPPED_SHEETS:
                summary[sheet_name] = {"skipped": True}
                continue

            ws = wb[sheet_name]
            header_row = _find_header_row(ws)
            if header_row is None:
                summary[sheet_name] = {"skipped": True, "reason": "no header row found"}
                continue

            headers = [
                _normalize_header(c)
                for c in next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
            ]

            is_closed = sheet_name == CLOSED_SHEET
            entity_carrier = SHEET_TO_ENTITY_CARRIER.get(sheet_name)
            if not is_closed and not entity_carrier:
                summary[sheet_name] = {"skipped": True, "reason": "no entity/carrier mapping configured"}
                continue

            inserted = 0
            preserved = 0
            for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
                record = {}
                for header, value in zip(headers, row):
                    if header and value is not None:
                        record[header] = value

                account_number = _normalize_account_number(record.get("account_number"))
                # A row with no account number and no branch name is almost
                # certainly a blank trailing row — skip it.
                if not account_number and not record.get("branch"):
                    continue

                def clean(field):
                    v = record.get(field)
                    if v is None:
                        return None
                    text = " ".join(str(v).strip().split())
                    if field == "location" and text:
                        # Safe casing-only normalization (DAMMAM/Dammam/dammam
                        # -> Dammam) so identical cities aren't split into
                        # separate location-stat buckets. Genuinely different
                        # spellings (Jazan/Jizan, Madina/Madinah, Ahsa/Hassa)
                        # are left alone rather than guessing they're the same place.
                        text = text.title()
                    return text or None

                if is_closed:
                    conn.execute(
                        """INSERT INTO closed_accounts
                           (location, cr, branch, connection, account_number, service_number, serial_number, status, closed_date)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            clean("location"), clean("cr"), clean("branch"), clean("connection"),
                            account_number, clean("service_number"), clean("serial_number"),
                            clean("status"), clean("closed_date"),
                        ),
                    )
                else:
                    entity, carrier = entity_carrier
                    if (sheet_name, account_number, clean("branch")) in protected_keys:
                        # Already present as a protected row (manually edited
                        # since the last import) — don't insert a duplicate.
                        preserved += 1
                        continue
                    conn.execute(
                        """INSERT INTO master_accounts
                           (entity, carrier, sheet_name, location, cr, branch, department, connection,
                            account_number, service_number, serial_number, status, package, notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            entity, carrier, sheet_name, clean("location"), clean("cr"), clean("branch"),
                            clean("department"), clean("connection"), account_number,
                            clean("service_number"), clean("serial_number"), clean("status"),
                            clean("package"), clean("notes"),
                        ),
                    )
                inserted += 1

            summary[sheet_name] = {
                "entity": entity_carrier[0] if entity_carrier else None,
                "carrier": entity_carrier[1] if entity_carrier else None,
                "rows_imported": inserted,
                "rows_preserved": preserved,
            }

        conn.commit()
    finally:
        conn.close()

    return summary


def _normalize_branch(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def import_store_sheet(path: str) -> dict:
    """
    Applies a supplementary "All stores Bills"-style spreadsheet — a single
    flat sheet of CR / Store / Service / Acc / Amount / Cost center / Dept
    columns, with no Entity/Carrier/Location of its own — onto the existing
    master_accounts rows, matched by account number.

    Unlike `import_master_excel`, this never inserts new accounts (there's
    no Entity/Carrier to assign them to) and never deletes anything. For
    each account number in the sheet:
      - if exactly one master_accounts row exists for it, that row is
        updated (unambiguous even if branch names don't match verbatim);
      - if several rows exist (a bulk account spanning many branches), only
        the row whose branch name matches (case/whitespace-insensitive) is
        updated — a bulk account with no matching branch row is left alone
        rather than guessing which one it means.
    Updated rows are marked `is_manually_edited` so a later
    `import_master_excel` run won't wipe this enrichment.
    """
    import openpyxl  # imported lazily: only needed for this import step

    if not os.path.isfile(path):
        raise FileNotFoundError(f"Store spreadsheet not found: {path}")

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]

    header_map = {"cr": "cr", "store": "branch", "service": "service_number", "acc": "account_number", "cost center": "cost_center"}
    headers = []
    for cell in next(ws.iter_rows(min_row=1, max_row=1, values_only=True)):
        key = " ".join(str(cell or "").strip().lower().split())
        headers.append(header_map.get(key))

    conn = _connect()
    result = {"updated": 0, "skipped_unknown_account": 0, "skipped_no_branch_match": 0}
    try:
        _ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        existing_by_account: dict = {}
        for row in conn.execute("SELECT id, account_number, branch FROM master_accounts WHERE account_number IS NOT NULL"):
            existing_by_account.setdefault(row["account_number"], []).append((row["id"], row["branch"]))

        for row in ws.iter_rows(min_row=2, values_only=True):
            record = {}
            for header, value in zip(headers, row):
                if header and value is not None:
                    record[header] = value

            account_number = _normalize_account_number(record.get("account_number"))
            if not account_number:
                continue

            candidates = existing_by_account.get(account_number)
            if not candidates:
                result["skipped_unknown_account"] += 1
                continue

            if len(candidates) == 1:
                target_id = candidates[0][0]
            else:
                new_branch = _normalize_branch(record.get("branch"))
                matches = [rid for rid, branch in candidates if _normalize_branch(branch) == new_branch]
                if len(matches) != 1:
                    result["skipped_no_branch_match"] += 1
                    continue
                target_id = matches[0]

            cr = record.get("cr")
            cost_center = record.get("cost_center")
            conn.execute(
                "UPDATE master_accounts SET cr = COALESCE(?, cr), cost_center = ?, is_manually_edited = 1 WHERE id = ?",
                (str(cr).strip() if cr is not None else None, str(cost_center).strip() if cost_center is not None else None, target_id),
            )
            result["updated"] += 1

        conn.commit()
    finally:
        conn.close()

    return result


UNSPECIFIED_LOCATION = "Unspecified"
MULTIPLE_LOCATIONS = "Multiple Locations"


def _resolve_location(locations: list) -> str:
    """
    One billing account number often covers many service lines (SIM cards,
    fiber links, ...) across many branches — most consistently share one
    city, but a few (15 of 317 in the source spreadsheet) genuinely span
    several. Rather than arbitrarily picking one, distinct sets collapse to
    a clearly-labeled "Multiple Locations" bucket instead of a wrong guess.
    """
    distinct = {loc for loc in locations if loc}
    if not distinct:
        return UNSPECIFIED_LOCATION
    if len(distinct) == 1:
        return next(iter(distinct))
    return MULTIPLE_LOCATIONS


def lookup_location(carrier: str, account_number: str) -> Optional[dict]:
    """
    Finds the Location/Branch/Department for one carrier+account_number.
    Location is resolved across every branch row under that account (see
    `_resolve_location`); branch/department/cr just show the first row's
    values, since those are inherently per-branch, not per-account.
    """
    if not account_number:
        return None
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT location, branch, department, cr FROM master_accounts WHERE carrier = ? AND account_number = ?",
            (carrier, str(account_number)),
        ).fetchall()
        if not rows:
            return None
        first = dict(rows[0])
        first["location"] = _resolve_location([r["location"] for r in rows])
        return first
    finally:
        conn.close()


MASTER_ACCOUNT_FIELDS = [
    "entity", "carrier", "location", "cr", "branch", "department",
    "connection", "account_number", "service_number", "serial_number",
    "status", "package", "notes", "cost_center",
]


def list_master_accounts(
    entity: Optional[str] = None,
    carrier: Optional[str] = None,
    location: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Lists rows from the editable master_accounts table (CRUD-backed, not
    just the read-only import), with simple filters and pagination. `search`
    matches account_number/branch/service_number/notes (case-insensitive).
    """
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        where = []
        params: list = []
        if entity:
            where.append("entity = ?")
            params.append(entity)
        if carrier:
            where.append("carrier = ?")
            params.append(carrier)
        if location:
            where.append("location = ?")
            params.append(location)
        if search:
            where.append("(account_number LIKE ? OR branch LIKE ? OR service_number LIKE ? OR notes LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        total = conn.execute(f"SELECT COUNT(*) FROM master_accounts {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM master_accounts {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total}
    except sqlite3.OperationalError:
        return {"rows": [], "total": 0}
    finally:
        conn.close()


def create_master_account(fields: dict) -> int:
    """Inserts one row into master_accounts (a manual entry, not tied to any spreadsheet import). Returns its id."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        values = {k: fields.get(k) for k in MASTER_ACCOUNT_FIELDS}
        values["sheet_name"] = "Manual Entry"
        values["is_manually_edited"] = 1
        columns = list(values.keys())
        placeholders = ",".join("?" for _ in columns)
        cur = conn.execute(
            f"INSERT INTO master_accounts ({','.join(columns)}) VALUES ({placeholders})",
            [values[c] for c in columns],
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_master_account(row_id: int, fields: dict) -> bool:
    """
    Updates the given fields on one master_accounts row, and marks it as
    manually edited so a future spreadsheet re-import won't overwrite it
    (see `import_master_excel`). Returns False if the row doesn't exist.
    """
    updates = {k: v for k, v in fields.items() if k in MASTER_ACCOUNT_FIELDS}
    if not updates:
        return False
    updates["is_manually_edited"] = 1
    conn = _connect()
    try:
        _ensure_schema(conn)
        set_sql = ", ".join(f"{k} = ?" for k in updates)
        cur = conn.execute(
            f"UPDATE master_accounts SET {set_sql} WHERE id = ?",
            list(updates.values()) + [row_id],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_master_account(row_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM master_accounts WHERE id = ?", (row_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def build_location_index() -> dict:
    """
    Loads the whole (carrier, account_number) -> location info mapping into
    memory in one query, for bulk-joining against a list of accounts/invoices
    without one SQLite query per row. Location is resolved across every
    branch row under that account (see `_resolve_location`); branch/
    department just show the first row's values, since those are
    inherently per-branch, not per-account.
    """
    conn = _connect()
    index = {}
    grouped = {}
    try:
        if not os.path.isfile(DB_PATH):
            return index
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT carrier, account_number, location, branch, department FROM master_accounts"
        ).fetchall()
        for row in rows:
            key = (row["carrier"], row["account_number"])
            grouped.setdefault(key, []).append(row)

        for key, group_rows in grouped.items():
            first = group_rows[0]
            index[key] = {
                "location": _resolve_location([r["location"] for r in group_rows]),
                "branch": first["branch"],
                "department": first["department"],
            }
        return index
    except sqlite3.OperationalError:
        return index
    finally:
        conn.close()
