"""
Syncs archived carrier invoices into Oracle Fusion Cloud Procurement as
Purchase Requisitions - one requisition per carrier per month, covering
that carrier's pending invoices for the month across all its accounts:

  1. POST /purchaseRequisitions                         - header + one line
     per invoice (per cost center when the bill is split) + one distribution
     each (charge account from the cost center)
  2. POST /purchaseRequisitions/{id}/child/lines/{lineId}/child/attachments
                                                        - each invoice PDF, Base64, on that invoice's line
  3. POST /purchaseRequisitions/{id}/action/submitRequisition (if auto_submit)

Each invoice's progress is recorded in a local ledger (data/oracle_sync.db),
all rows of one requisition pointing at it, so a run that fails half-way
resumes from the failed step next time instead of creating a second
requisition. Before creating, Oracle is also searched for
an existing requisition with the same Description (which embeds the account
number and month) — the duplicate guard if the ledger is lost.

Oracle IDs (Business Units, Suppliers/Sites, charge accounts) come from the
mappings saved from the dashboard (data/oracle_mappings.json). Accounts whose
location is unmapped, or that have no single cost center, are charged to the
entity's suspense cost center and raise a Procurement Administrator alert.

Nothing is sent to Oracle unless ORACLE_LIVE_MODE=true; otherwise a sync
just returns the payloads it would send.
"""
import base64
import calendar
import json
import os
import re
import smtplib
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional

import httpx

from .config import oracle_settings, OracleSettings
from .data_store import load_accounts
from .file_organizer import list_archived_invoices, resolve_archived_invoice_path, invoice_month_folder
from .master_data import lookup_billing_info, list_master_locations, bill_line_allocation

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "oracle_sync.db")

_sync_lock = threading.Lock()


class OracleError(Exception):
    """An Oracle REST call failed (HTTP error or unexpected response)."""


class MappingError(Exception):
    """A required Oracle ID isn't configured for this invoice — fix the mappings file."""


# ---- Local ledger -------------------------------------------------------

def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sync_log (
            entity TEXT, provider TEXT, account_number TEXT, month TEXT,
            filename TEXT, amount REAL, cost_center TEXT, charge_account_id TEXT,
            used_suspense INTEGER DEFAULT 0,
            requisition_uniq_id TEXT, requisition_number TEXT,
            attached INTEGER DEFAULT 0, submitted INTEGER DEFAULT 0,
            status TEXT, message TEXT, updated_at TEXT,
            PRIMARY KEY (entity, provider, account_number, month)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, severity TEXT,
            entity TEXT, provider TEXT, account_number TEXT, month TEXT, message TEXT,
            emailed INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS invoice_amounts (
            entity TEXT, provider TEXT, account_number TEXT, month TEXT, amount REAL, recorded_at TEXT,
            PRIMARY KEY (entity, provider, account_number, month)
        )"""
    )
    if "invoice_date" not in {r[1] for r in conn.execute("PRAGMA table_info(invoice_amounts)")}:
        conn.execute("ALTER TABLE invoice_amounts ADD COLUMN invoice_date TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS charge_account_cache (
            entity TEXT, cost_center TEXT, charge_account_id TEXT,
            PRIMARY KEY (entity, cost_center)
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS schedule_runs (period TEXT PRIMARY KEY, ran_at TEXT, summary TEXT)")
    # One row per live run (bulk / manual / scheduled) ...
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sync_batches (
            id TEXT PRIMARY KEY, source TEXT, started_at TEXT, finished_at TEXT, status TEXT,
            total INTEGER DEFAULT 0, created INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
            message TEXT
        )"""
    )
    # ... and one row per Oracle REST call made during it, with what came back.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS oracle_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, created_at TEXT,
            entity TEXT, provider TEXT, account_number TEXT, month TEXT,
            step TEXT, method TEXT, path TEXT, http_status INTEGER, ok INTEGER, elapsed_ms INTEGER,
            requisition_number TEXT, requisition_header_id TEXT,
            request_json TEXT, response_json TEXT, error TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_responses_batch ON oracle_responses(batch_id)")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ledger_get(conn, inv: dict) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sync_log WHERE entity=? AND provider=? AND account_number=? AND month=?",
        (inv["entity"], inv["provider"], inv["account_number"], inv["month"]),
    ).fetchone()


def _ledger_put(conn, inv: dict, **fields) -> None:
    fields["updated_at"] = _now()
    key = (inv["entity"], inv["provider"], inv["account_number"], inv["month"])
    conn.execute(
        "INSERT OR IGNORE INTO sync_log (entity, provider, account_number, month) VALUES (?,?,?,?)", key
    )
    set_sql = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE sync_log SET {set_sql} WHERE entity=? AND provider=? AND account_number=? AND month=?",
        list(fields.values()) + list(key),
    )
    conn.commit()


def list_sync_log(entity: Optional[str] = None, status: Optional[str] = None) -> list:
    conn = _connect()
    try:
        where, params = [], []
        if entity:
            where.append("entity = ?")
            params.append(entity)
        if status:
            where.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        return [dict(r) for r in conn.execute(f"SELECT * FROM sync_log {where_sql} ORDER BY updated_at DESC", params)]
    finally:
        conn.close()


def list_alerts(limit: int = 200) -> list:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


# ---- Invoice amounts ----------------------------------------------------

def record_invoice_amounts(accounts) -> None:
    """
    Remembers each scraped account's amount (and invoice date) against the
    archived invoice's month. accounts.json only holds the latest scrape per login, so without
    this an older month's PDF would have no amount left to bill by the time
    it's synced. Called right after a scrape job saves its accounts.
    """
    conn = _connect()
    try:
        for account in accounts:
            if not account.invoice_path:
                continue
            # .../<Entity>/<Carrier>/<Account>/<Month_Year>/Bills/<Status>/<file>.pdf
            parts = os.path.normpath(account.invoice_path).split(os.sep)
            if len(parts) < 5:
                continue
            conn.execute(
                """INSERT INTO invoice_amounts (entity, provider, account_number, month, amount, recorded_at, invoice_date)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (entity, provider, account_number, month)
                   DO UPDATE SET amount = excluded.amount, recorded_at = excluded.recorded_at, invoice_date = excluded.invoice_date""",
                (account.entity, account.provider, account.account_number, parts[-4], account.amount, _now(), account.due_date),
            )
        conn.commit()
    finally:
        conn.close()


def _scraped_account(inv: dict):
    """The latest scrape's row for this invoice (same PDF, or a due date in the invoice's month)."""
    for account in load_accounts():
        if (account.entity, account.provider, account.account_number) != (inv["entity"], inv["provider"], inv["account_number"]):
            continue
        if account.invoice_path and os.path.basename(account.invoice_path) == inv["filename"]:
            return account
        if account.due_date and invoice_month_folder(account.due_date) == inv["month"]:
            return account
    return None


def _resolve_invoice_date(conn, inv: dict) -> str:
    """
    The invoice's date, YYYY-MM-DD: recorded at scrape time, else from the
    latest scrape (STC's "Issued N days ago"), else the bill month's last day
    (or today, if that's still ahead).
    """
    row = conn.execute(
        "SELECT invoice_date FROM invoice_amounts WHERE entity=? AND provider=? AND account_number=? AND month=?",
        (inv["entity"], inv["provider"], inv["account_number"], inv["month"]),
    ).fetchone()
    if row and row["invoice_date"]:
        return row["invoice_date"]
    account = _scraped_account(inv)
    if account and account.due_date and invoice_month_folder(account.due_date) == inv["month"]:
        return account.due_date
    month_end = _month_bounds(inv["month"])[1]
    today = datetime.now().strftime("%Y-%m-%d")
    return min(month_end, today) if month_end else today


def _need_by_date(invoice_date: str, days: int) -> str:
    """
    Invoice date + `days`, but never before today: Oracle rejects a
    RequestedDeliveryDate in the past (POR-2010567), which an older invoice
    would otherwise get.
    """
    need_by = datetime.strptime(invoice_date, "%Y-%m-%d") + timedelta(days=days)
    return max(need_by.strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d"))


def _resolve_amount(conn, inv: dict) -> Optional[float]:
    row = conn.execute(
        "SELECT amount FROM invoice_amounts WHERE entity=? AND provider=? AND account_number=? AND month=?",
        (inv["entity"], inv["provider"], inv["account_number"], inv["month"]),
    ).fetchone()
    if row:
        return row["amount"]
    # Fall back to the latest scrape snapshot, for invoices downloaded before
    # amounts were being recorded.
    account = _scraped_account(inv)
    return account.amount if account else None


# ---- Mappings -----------------------------------------------------------

def load_mappings(path: Optional[str] = None) -> dict:
    path = path or oracle_settings.mappings_path
    if not os.path.isfile(path):
        raise MappingError(
            "No Oracle mappings saved yet: fill in Oracle Sync → Mappings "
            "(Business Unit, suppliers, ...) and click Save Mappings."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_mappings_for_editing() -> dict:
    """The saved mappings, or the example skeleton (flagged) if none are saved yet."""
    try:
        result = {"saved": True, "mappings": load_mappings()}
    except MappingError:
        # Start from an empty form (examples are shown as input hints only):
        # pre-filling sample values made it too easy to save them as real ones.
        result = {"saved": False, "mappings": {"gl_lookup": {
            "resource": "accountCombinationsLOV", "id_field": "_CODE_COMBINATION_ID",
            "segment_field_format": "_SEGMENT{n}", "separator": "-", "chart_of_accounts_field": "_CHART_OF_ACCOUNTS_ID",
        }}}
    result["mappings"]["payload_template"] = get_payload_template(result["mappings"])
    result["default_payload_template"] = DEFAULT_PAYLOAD_TEMPLATE
    result["placeholders"] = placeholder_catalog()
    result["locations"] = list_master_locations()
    return result


def save_mappings(mappings: dict) -> None:
    for key in ("business_units", "business_unit_names", "suppliers", "suspense_cost_center",
                "charge_account_templates", "charge_accounts", "deliver_to_locations", "deliver_to_location_ids",
                "destination_organizations", "payload_template"):
        if key in mappings and not isinstance(mappings[key], dict):
            raise MappingError(f"'{key}' must be an object.")
    for section, fields in (mappings.get("payload_template") or {}).items():
        if section not in DEFAULT_PAYLOAD_TEMPLATE:
            raise MappingError(f"Payload template has an unknown section '{section}' (expected header, line, distribution).")
        if not isinstance(fields, dict):
            raise MappingError(f"Payload template section '{section}' must be an object of Oracle field -> value.")
        for field, value in fields.items():
            if isinstance(value, str):
                unknown = [n for n in _PLACEHOLDER_RE.findall(value) if n not in PLACEHOLDERS]
                if unknown:
                    raise MappingError(f"Payload template {section}.{field} uses unknown placeholder(s): {', '.join('{' + n + '}' for n in unknown)}")
    path = oracle_settings.mappings_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mappings, f, indent=2)
    # A changed template or explicit mapping can change which GL account a
    # cost center resolves to, so drop previously looked-up IDs.
    conn = _connect()
    try:
        conn.execute("DELETE FROM charge_account_cache")
        conn.commit()
    finally:
        conn.close()


def set_invoice_amount(entity: str, provider: str, account_number: str, month: str, amount: float) -> None:
    """Manually sets the amount to bill for one archived invoice (when no scrape recorded it)."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO invoice_amounts (entity, provider, account_number, month, amount, recorded_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (entity, provider, account_number, month)
               DO UPDATE SET amount = excluded.amount, recorded_at = excluded.recorded_at""",
            (entity, provider, account_number, month, float(amount), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def invoice_states() -> list:
    """Per archived invoice: recorded amount and Oracle sync status — merged into the Invoice Archive table."""
    conn = _connect()
    try:
        amounts = {
            (r["entity"], r["provider"], r["account_number"], r["month"]): r["amount"]
            for r in conn.execute("SELECT * FROM invoice_amounts")
        }
        ledger = {
            (r["entity"], r["provider"], r["account_number"], r["month"]): dict(r)
            for r in conn.execute("SELECT * FROM sync_log")
        }
    finally:
        conn.close()
    rows = []
    for key in set(amounts) | set(ledger):
        entry = ledger.get(key, {})
        rows.append({
            "entity": key[0], "provider": key[1], "account_number": key[2], "month": key[3],
            "amount": amounts.get(key, entry.get("amount")),
            "oracle_status": entry.get("status"),
            "requisition_number": entry.get("requisition_number"),
            "message": entry.get("message"),
        })
    return rows


def clear_alerts() -> int:
    conn = _connect()
    try:
        count = conn.execute("DELETE FROM alerts").rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def reset_ledger_entry(entity: str, provider: str, account_number: str, month: str) -> bool:
    """
    Forgets one invoice's sync record so the next sync treats it as new
    (it will still be skipped if Oracle already has a matching requisition).
    """
    conn = _connect()
    try:
        count = conn.execute(
            "DELETE FROM sync_log WHERE entity=? AND provider=? AND account_number=? AND month=?",
            (entity, provider, account_number, month),
        ).rowcount
        conn.commit()
        return count > 0
    finally:
        conn.close()


def test_connection(transport: Optional[httpx.BaseTransport] = None) -> dict:
    """Read-only check that the base URL and credentials work: fetches at most one requisition."""
    client = OracleClient(transport=transport)
    try:
        data = client._request("GET", "/purchaseRequisitions", params={"limit": 1, "onlyData": "true", "fields": "Requisition"})
        return {"ok": True, "message": f"Connected. Oracle returned {len(data.get('items') or [])} requisition(s) for a 1-row test query."}
    finally:
        client.close()


def _normalize_cost_center(cost_center: str, mappings: dict) -> str:
    """
    Pads to the GL segment width (e.g. "112" -> "0112") when `cost_center_width`
    is set. A zero cost center ("0") is always sent as "0000", width or not.
    """
    width = mappings.get("cost_center_width")
    cc = str(cost_center).strip()
    if cc.isdigit() and int(cc) == 0:
        return "0" * max(width or 0, 4)
    return cc.zfill(width) if width and cc.isdigit() else cc


def _charge_segments(template: Optional[str], cost_center: str, dept_code: Optional[str]) -> Optional[str]:
    """The charge account pattern with {cost_center} and {dept_code} filled in."""
    if not template:
        return None
    return template.replace("{cost_center}", cost_center).replace("{dept_code}", dept_code or "")


def _month_label(month_folder: str) -> str:
    return month_folder.replace("_", " ")


# ---- Oracle REST client -------------------------------------------------

def oracle_host(base_url: str) -> str:
    """
    Accepts either the pod host or a full endpoint pasted from Postman
    (https://pod.fa.em2.oraclecloud.com:443/fscmRestApi/resources/.../purchaseRequisitions)
    and returns just the host part, since the client adds the REST path itself.
    """
    host = base_url.strip().split("/fscmRestApi")[0].rstrip("/")
    return host[:-4] if host.startswith("https://") and host.endswith(":443") else host


class OracleClient:
    def __init__(self, cfg: OracleSettings = oracle_settings, transport: Optional[httpx.BaseTransport] = None):
        if not cfg.base_url or not cfg.username:
            raise MappingError("Oracle base URL, username and password are not set (Oracle Sync → Settings).")
        self.cfg = cfg
        self.http = httpx.Client(
            base_url=f"{oracle_host(cfg.base_url)}/fscmRestApi/resources/{cfg.api_version}",
            auth=(cfg.username, cfg.password),
            timeout=cfg.timeout_seconds,
            headers={"REST-Framework-Version": "4", "Accept": "application/json"},
            transport=transport,
        )
        # Every request/response, drained per invoice into oracle_responses.
        self.calls: list = []

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, *, json_body=None, params=None, content_type="application/json") -> dict:
        headers = {"Content-Type": content_type} if json_body is not None else {}
        call = {"method": method, "path": path, "step": _step_name(method, path), "request": _loggable_request(json_body, params)}
        self.calls.append(call)
        started = time.monotonic()
        try:
            resp = self.http.request(method, path, json=json_body, params=params, headers=headers)
        except httpx.HTTPError as exc:
            call.update(elapsed_ms=int((time.monotonic() - started) * 1000), error=f"{type(exc).__name__}: {exc}")
            raise OracleError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        call.update(http_status=resp.status_code, elapsed_ms=int((time.monotonic() - started) * 1000), response=resp.text[:200_000])
        if resp.status_code >= 400:
            reason = _oracle_error_text(resp)
            call["error"] = f"HTTP {resp.status_code}: {reason}"
            raise OracleError(f"{STEP_LABELS.get(call['step'], call['step'])} failed (HTTP {resp.status_code}): {reason}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    @staticmethod
    def _quote(value) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def find_requisition(self, header: dict) -> Optional[dict]:
        """An existing requisition with the same Description (embeds account + month) in the same BU."""
        clauses = [f"Description = {self._quote(header['Description'])}"]
        if header.get("RequisitioningBUId") is not None:
            clauses.append(f"RequisitioningBUId = {int(header['RequisitioningBUId'])}")
        elif header.get("RequisitioningBU"):
            clauses.append(f"RequisitioningBU = {self._quote(header['RequisitioningBU'])}")
        data = self._request(
            "GET",
            "/purchaseRequisitions",
            params={
                "q": " and ".join(clauses),
                "fields": "RequisitionHeaderId,Requisition,DocumentStatus",
                "onlyData": "true",
                "limit": 1,
            },
        )
        items = data.get("items") or []
        return items[0] if items else None

    def find_charge_account_id(self, entity: str, segments: list, mappings: dict) -> Optional[str]:
        lookup = mappings.get("gl_lookup", {})
        field_format = lookup.get("segment_field_format", "_SEGMENT{n}")
        id_field = lookup.get("id_field", "_CODE_COMBINATION_ID")
        clauses = [f"{field_format.format(n=i)} = {self._quote(v)}" for i, v in enumerate(segments, start=1)]
        coa = lookup.get("chart_of_accounts_id", {}).get(entity)
        if coa:
            clauses.insert(0, f"{lookup.get('chart_of_accounts_field', '_CHART_OF_ACCOUNTS_ID')} = {coa}")
        data = self._request(
            "GET",
            f"/{lookup.get('resource', 'accountCombinationsLOV')}",
            params={"q": " and ".join(clauses), "onlyData": "true", "limit": 2},
        )
        items = data.get("items") or []
        if len(items) != 1 or items[0].get(id_field) is None:
            return None
        return str(items[0][id_field])

    def create_requisition(self, payload: dict) -> dict:
        return self._request("POST", "/purchaseRequisitions", json_body=payload)

    def line_ids(self, uniq_id: str) -> dict:
        """LineNumber -> the line's linesUniqID (last segment of its self link) for a requisition."""
        data = self._request(
            "GET", f"/purchaseRequisitions/{uniq_id}/child/lines",
            params={"fields": "LineNumber,RequisitionLineId", "limit": 500},
        )
        ids = {}
        for item in data.get("items") or []:
            href = next((l.get("href") for l in item.get("links", []) if l.get("rel") == "self" and l.get("href")), None)
            line_id = href.rstrip("/").rsplit("/", 1)[-1] if href else item.get("RequisitionLineId")
            if item.get("LineNumber") is not None and line_id is not None:
                ids[int(item["LineNumber"])] = str(line_id)
        return ids

    def line_attachment_names(self, uniq_id: str, line_id: str) -> set:
        data = self._request(
            "GET", f"/purchaseRequisitions/{uniq_id}/child/lines/{line_id}/child/attachments",
            params={"fields": "FileName", "onlyData": "true", "limit": 500},
        )
        return {item.get("FileName") for item in data.get("items") or []}

    def add_line_attachment(self, uniq_id: str, line_id: str, payload: dict) -> dict:
        return self._request("POST", f"/purchaseRequisitions/{uniq_id}/child/lines/{line_id}/child/attachments", json_body=payload)

    def submit_requisition(self, uniq_id: str) -> dict:
        return self._request(
            "POST",
            f"/purchaseRequisitions/{uniq_id}/action/submitRequisition",
            json_body={"approverCheckoutFlowFlag": False},
            content_type="application/vnd.oracle.adf.action+json",
        )


STEP_LABELS = {
    "create": "Create requisition", "attach": "Attach PDF", "submit": "Submit for approval",
    "line_lookup": "Read requisition lines", "attach_check": "Check line attachments",
    "duplicate_check": "Duplicate check", "gl_lookup": "GL account lookup",
}


def _oracle_error_text(resp: httpx.Response) -> str:
    """Oracle's own reason for a failed call (o:errorDetails / detail / title), else the raw body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:500] or resp.reason_phrase
    if isinstance(body, dict):
        details = [d.get("detail") for d in body.get("o:errorDetails") or [] if isinstance(d, dict) and d.get("detail")]
        if details:
            return " | ".join(details)[:500]
        for key in ("detail", "title", "message"):
            if body.get(key):
                return str(body[key])[:500]
    return resp.text.strip()[:500]


def _step_name(method: str, path: str) -> str:
    if path.endswith("/child/attachments"):
        return "attach" if method == "POST" else "attach_check"
    if path.endswith("/child/lines"):
        return "line_lookup"
    if "/action/submitRequisition" in path:
        return "submit"
    if "accountCombinations" in path:
        return "gl_lookup"
    if path.endswith("/purchaseRequisitions"):
        return "create" if method == "POST" else "duplicate_check"
    return f"{method.lower()} {path}"


def _loggable_request(json_body, params) -> Optional[str]:
    """The request as stored for the Responses view — the PDF's Base64 is replaced by its size."""
    if json_body is None:
        return json.dumps({"params": params}) if params else None
    body = dict(json_body) if isinstance(json_body, dict) else json_body
    if isinstance(body, dict) and isinstance(body.get("FileContents"), str):
        body["FileContents"] = f"<base64, {len(body['FileContents'])} characters>"
    return json.dumps(body)


def _uniq_id_from_response(resp: dict) -> Optional[str]:
    """The purchaseRequisitionsUniqID is the last path segment of the created resource's self link."""
    for link in resp.get("links", []):
        if link.get("rel") == "self" and link.get("href"):
            return link["href"].rstrip("/").rsplit("/", 1)[-1]
    header_id = resp.get("RequisitionHeaderId")
    return str(header_id) if header_id is not None else None


# ---- Payload construction -----------------------------------------------

def _resolve_charge_account(conn, client: Optional[OracleClient], entity: str, cost_center: str, mappings: dict,
                            dept_code: Optional[str] = None) -> tuple:
    """
    Returns (charge_account_id or None, segments-string or None). Order:
    explicit mapping -> local cache -> Oracle GL account combinations lookup
    (cached). Without a client (dry run) the lookup is skipped.
    """
    explicit = mappings.get("charge_accounts", {}).get(entity, {}).get(cost_center)
    template = mappings.get("charge_account_templates", {}).get(entity)
    separator = mappings.get("gl_lookup", {}).get("separator", "-")
    segments_text = _charge_segments(template, cost_center, dept_code)
    if explicit:
        return str(explicit), segments_text
    # With {dept_code} in the pattern the same cost center can map to several
    # GL accounts, so the cache key carries the department too.
    cache_key = f"{cost_center}|{dept_code or ''}" if template and "{dept_code}" in template else cost_center
    cached = conn.execute(
        "SELECT charge_account_id FROM charge_account_cache WHERE entity=? AND cost_center=?", (entity, cache_key)
    ).fetchone()
    if cached:
        return cached["charge_account_id"], segments_text
    if not template or client is None:
        return None, segments_text
    account_id = client.find_charge_account_id(entity, segments_text.split(separator), mappings)
    if not account_id:
        raise MappingError(f"Oracle has no single enabled GL account combination {segments_text} for {entity} cost center {cost_center}.")
    conn.execute("INSERT OR REPLACE INTO charge_account_cache VALUES (?,?,?)", (entity, cache_key, account_id))
    conn.commit()
    return account_id, segments_text


# ---- Payload template ---------------------------------------------------
#
# The requisition body is built from a template (Mappings -> Payload
# template) rather than hard-coded, since which Oracle fields a pod needs -
# and whether it takes names (RequisitioningBU, PreparerEmail) or IDs - varies.
# In a template value:
#   "{name}"          the whole value is replaced (numbers stay numbers)
#   "text {name} ..." placeholders are substituted into the text
#   "?{name}"         optional: the field is left out when the value is empty
# Any other field whose placeholder has no value stops that invoice with a
# message saying which mapping/setting to fill in.

# Mirrors the payload verified against the pod (Rate Based Services line,
# one distribution with the charge account as segments). One requisition
# covers a carrier's pending invoices for one month, each account's invoice
# on its own line: on a line {account_number} / {amount} are that invoice's
# (per cost center when the bill is split); in the header they cover all.
DEFAULT_PAYLOAD_TEMPLATE = {
    "header": {
        "RequisitioningBU": "{bu_name}",
        "RequisitioningBUId": "?{bu_id}",
        "PreparerEmail": "{preparer_email}",
        "PreparerId": "?{preparer_id}",
        "Description": "Mobile & Internet",
        "ExternallyManagedFlag": "N",
    },
    "line": {
        "LineNumber": 1,
        "LineTypeCode": "{line_type}",
        "ItemDescription": "Mobile & Internet - {carrier} {account_number} - {invoice_months}",
        "CategoryName": "{category}",
        "Quantity": 1,
        "UOM": "Each",
        "CurrencyCode": "{currency}",
        "Price": "{amount}",
        "RequestedDeliveryDate": "{need_by_date}",
        "DestinationTypeCode": "EXPENSE",
        "DestinationOrganizationId": "?{destination_org_id}",
        "DeliverToLocationId": "?{deliver_to_location_id}",
        "DeliverToLocationCode": "?{deliver_to_location}",
        "RequesterEmail": "{requester_email}",
    },
    "distribution": {
        "DistributionNumber": 1,
        "Quantity": 1,
        "ChargeAccount": "{charge_account}",
        "BudgetDate": "{invoice_date}",
    },
}

# name -> (group, description, where to set it when missing). "Line" values
# differ per requisition line (one line per cost center on the bill); used in
# the header they describe the whole invoice.
PLACEHOLDERS = {
    "entity": ("Invoice", "Entity (Ibraq, Match, ...)", None),
    "carrier": ("Invoice", "Carrier (STC, Mobily, Zain)", None),
    "account_number": ("Invoice", "Billing account number (header: all accounts on the requisition, comma separated)", None),
    "month": ("Invoice", "Archive month folder(s), comma separated, e.g. August_2026, September_2026", None),
    "month_label": ("Invoice", "Month(s) as text, e.g. August 2026, September 2026", None),
    "invoice_months": ("Invoice", "The bill months on this requisition, comma separated (same as month_label)", None),
    "invoice_count": ("Invoice", "Number of invoices on this requisition", None),
    "month_start": ("Invoice", "First day of the earliest bill month, YYYY-MM-DD", None),
    "month_end": ("Invoice", "Last day of the latest bill month, YYYY-MM-DD", None),
    "today": ("Invoice", "Today's date, YYYY-MM-DD", None),
    "invoice_date": ("Line", "The invoice's date, YYYY-MM-DD (header: the latest one)", None),
    "need_by_date": ("Line", "Invoice date + the need-by days setting, or today if that is already past, YYYY-MM-DD", "Settings -> Requisition defaults -> Need-by days"),
    "invoice_amount": ("Invoice", "Total of all invoices on this requisition (number)", "Invoice Archive -> Amount, or import the bill sheet"),
    "filename": ("Invoice", "PDF file name(s), comma separated", None),
    "invoice_status": ("Invoice", "Paid / Unpaid", None),
    "line_number": ("Line", "Requisition line number (1, 2, ...)", None),
    "amount": ("Line", "This cost center's share of the invoices (header: their total)", "Invoice Archive -> Amount, or import the bill sheet"),
    "cost_center": ("Line", "Cost center from the bill sheet / master data (suspense when missing)", "Mappings -> Per entity -> Suspense cost center"),
    "dept_code": ("Line", "Department code of this cost center's lines from master data (the department name when the code is missing)", "Master Data -> Dept Code"),
    "location": ("Line", "City of this cost center's lines, from master data", None),
    "branch": ("Line", "Store names of this cost center's lines", "Master Data"),
    "service_numbers": ("Line", "Service numbers billed on this line", None),
    "line_count": ("Line", "Number of service lines on this requisition line", None),
    "suspense_reason": ("Line", "Why the suspense cost center was used (blank otherwise)", None),
    "deliver_to_location": ("Line", "Oracle deliver-to location for this line's city, else entity default", "Mappings -> Deliver-to locations"),
    "deliver_to_location_id": ("Line", "Oracle DeliverToLocationId for this line's city, else entity default", "Mappings -> Per entity -> Deliver-to location ID"),
    "charge_account": ("Line", "Charge account segments from the pattern, e.g. 01-0112-540100-000-000", "Mappings -> Per entity -> Charge account pattern"),
    "charge_account_id": ("Line", "Oracle ChargeAccountId for this cost center (override, or looked up)", "Mappings -> Charge account overrides / pattern"),
    "bu_name": ("Mappings", "Oracle Business Unit name for the entity", "Mappings -> Per entity -> Business Unit name"),
    "bu_id": ("Mappings", "Oracle Business Unit ID for the entity", "Mappings -> Per entity -> Business Unit ID"),
    "destination_org_id": ("Mappings", "Oracle DestinationOrganizationId (inventory org) for the entity", "Mappings -> Per entity -> Destination organization ID"),
    "supplier_name": ("Mappings", "Oracle supplier name for the carrier", "Mappings -> Suppliers -> Supplier name"),
    "supplier_site": ("Mappings", "Oracle supplier site name", "Mappings -> Suppliers -> Supplier site"),
    "supplier_id": ("Mappings", "Oracle supplier ID", "Mappings -> Suppliers -> Supplier ID"),
    "supplier_site_id": ("Mappings", "Oracle supplier site ID", "Mappings -> Suppliers -> Site ID"),
    "preparer_email": ("Settings", "Preparer email", "Settings -> Requisition defaults"),
    "requester_email": ("Settings", "Requester email (defaults to preparer)", "Settings -> Requisition defaults"),
    "preparer_id": ("Settings", "Preparer person ID", "Settings -> Requisition defaults"),
    "requester_id": ("Settings", "Requester person ID (defaults to preparer)", "Settings -> Requisition defaults"),
    "line_type": ("Settings", "Line type", "Settings -> Requisition defaults"),
    "category": ("Settings", "Category name", "Settings -> Requisition defaults"),
    "currency": ("Settings", "Currency code", "Settings -> Requisition defaults"),
    "item": ("Settings", "Oracle item number", "Settings -> Requisition defaults -> Item number"),
    "quantity": ("Settings", "Line quantity (number)", "Settings -> Requisition defaults -> Quantity"),
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# Sample values an earlier version of the Mappings form pre-filled; never send them.
SAMPLE_VALUES = {
    "Your Ibraq BU name", "Your Match BU name", "Your Salfa BU name", "Your Feelin BU name",
    "STC Site", "Mobily Site", "Zain Site", "Your default deliver-to location code",
}


LINE_PLACEHOLDERS = {n for n, (group, _, _) in PLACEHOLDERS.items() if group == "Line"} - {"line_number"}


def _template_placeholders(section: dict) -> set:
    return {n for v in section.values() if isinstance(v, str) for n in _PLACEHOLDER_RE.findall(v)}


def template_splits_lines(template: dict) -> bool:
    """One line per cost center only makes sense if a line uses a per-line value ({amount}, {cost_center}, ...)."""
    used = _template_placeholders(template["line"]) | _template_placeholders(template["distribution"])
    return bool(used & LINE_PLACEHOLDERS)


def description_identifies_bill(template: dict) -> bool:
    """
    Oracle is searched for an existing requisition by Description only when
    the Description contains the account number and bill month(s); a fixed
    Description (e.g. "Mobile & Internet") would match every earlier bill, so
    duplicates are then caught by the local sync log alone.
    """
    used = _template_placeholders(template["header"])
    return "account_number" in used and bool(used & {"month", "month_label", "invoice_months", "filename"})


def placeholder_catalog() -> list:
    return [{"name": n, "group": g, "description": d} for n, (g, d, _) in PLACEHOLDERS.items()]


def get_payload_template(mappings: dict) -> dict:
    template = mappings.get("payload_template") or {}
    return {section: template.get(section, DEFAULT_PAYLOAD_TEMPLATE[section]) for section in DEFAULT_PAYLOAD_TEMPLATE}


def _month_bounds(month_folder: str) -> tuple:
    try:
        first = datetime.strptime(month_folder, "%B_%Y")
    except ValueError:
        return None, None
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first.strftime("%Y-%m-%d"), first.replace(day=last_day).strftime("%Y-%m-%d")


MAX_TEXT = 240  # Oracle's Description / ItemDescription length


def _coerce(field: str, value):
    """Keeps numeric IDs numeric (Oracle *Id fields) and amounts as 2-decimal numbers."""
    if isinstance(value, str) and field.endswith("Id") and value.isdigit():
        return int(value)
    if isinstance(value, float):
        return round(value, 2)
    return value


def _render_section(section: dict, resolve, errors: list) -> dict:
    out = {}
    for field, raw in section.items():
        if not isinstance(raw, str) or "{" not in raw:
            out[field] = raw
            continue
        optional = raw.startswith("?")
        text = raw[1:] if optional else raw
        names = _PLACEHOLDER_RE.findall(text)
        values = {}
        missing = []
        for name in names:
            if name not in PLACEHOLDERS:
                errors.append(f"{field}: unknown placeholder {{{name}}}")
                continue
            value = resolve(name)
            if value is None or value == "":
                missing.append(name)
            values[name] = value
        if missing:
            if not optional:
                hints = "; ".join(
                    f"{{{n}}} (set it in {PLACEHOLDERS[n][2]})" if PLACEHOLDERS[n][2] else f"{{{n}}}" for n in missing
                )
                errors.append(f"{field} has no value: {hints}")
            continue
        if _PLACEHOLDER_RE.fullmatch(text):
            out[field] = _coerce(field, values[names[0]])
        else:
            rendered = _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), text)
            out[field] = rendered if len(rendered) <= MAX_TEXT else rendered[: MAX_TEXT - 3] + "..."
    return out


def _cc_key(cost_center: Optional[str]) -> Optional[str]:
    """Compares cost centers ignoring leading zeros ("0908" == "908")."""
    if cost_center is None or not str(cost_center).strip():
        return None
    return str(cost_center).strip().lstrip("0") or "0"


def _split_by_cost_center(inv: dict, amount: Optional[float], info: Optional[dict], mappings: dict) -> dict:
    """
    Decides the requisition lines for one invoice: one per cost center.

    With bill lines imported for the account (Master Data -> Import Store
    Sheet), each cost center's share is its lines' total, scaled so the
    lines add up to the invoice amount (the sheet's line charges usually
    exclude VAT / adjustments). Lines with no cost center anywhere go to the
    suspense cost center. Without bill lines, an account with exactly one
    cost center in the master data gets one line; anything else is charged
    to suspense as a whole.
    """
    entity = inv["entity"]
    suspense = mappings.get("suspense_cost_center", {}).get(entity)
    account_location = info["location"] if info else "Unmapped"
    alloc = bill_line_allocation(entity, inv["provider"], inv["account_number"], inv["month"])

    groups: list = []
    note = None
    if alloc:
        buckets: dict = {}
        for line in alloc["lines"]:
            key = _cc_key(line["cost_center"])
            bucket = buckets.setdefault(key, {"cost_center": line["cost_center"], "lines": []})
            bucket["lines"].append(line)
        lines_total = round(sum(l["amount"] for l in alloc["lines"]), 2)
        if amount is None:
            amount = lines_total
        note = f"Split by {len(alloc['lines'])} bill lines from {alloc['month'].replace('_', ' ')}"
        if not alloc["same_month"]:
            note += " (proportions; that month's sheet isn't imported)"
        if lines_total and abs(lines_total - amount) >= 0.01:
            note += f"; bill lines total {lines_total:,.2f} scaled to invoice {amount:,.2f}"
        for key, bucket in buckets.items():
            share = sum(l["amount"] for l in bucket["lines"])
            locations = {l["location"] for l in bucket["lines"] if l["location"]}
            groups.append({
                "cost_center": bucket["cost_center"] if key else suspense,
                "suspense_reason": None if key else f"{len(bucket['lines'])} bill line(s) have no cost center",
                "raw": share,
                "location": next(iter(locations)) if len(locations) == 1 else (account_location if not locations else "Multiple Locations"),
                "stores": [l["store"] for l in bucket["lines"] if l["store"]],
                "services": [l["service_number"] for l in bucket["lines"] if l["service_number"]],
                "departments": list(dict.fromkeys(l["department_code"] for l in bucket["lines"] if l.get("department_code"))),
            })
        if lines_total:
            for g in groups:
                g["amount"] = round(amount * g["raw"] / lines_total, 2)
            # put the rounding difference on the largest line so lines add up exactly
            diff = round(amount - sum(g["amount"] for g in groups), 2)
            if diff:
                max(groups, key=lambda g: abs(g["amount"]))["amount"] = round(max(groups, key=lambda g: abs(g["amount"]))["amount"] + diff, 2)
        else:
            groups = []
    if not groups:
        if amount is None:
            raise MappingError(
                "Invoice amount unknown: no scrape recorded it and no bill sheet is imported for this account "
                "(set it in Invoice Archive -> Amount, or Master Data -> Import Store Sheet)."
            )
        # A missing location alone doesn't send a bill to suspense: the cost
        # center (from the bill sheet / master data) is what decides where it's charged.
        reason = None
        if not info:
            reason = "account is not in the master data (Unmapped)"
        elif not info["cost_centers"]:
            reason = "no cost center in the master data"
        elif len(info["cost_centers"]) > 1:
            reason = (f"account spans {len(info['cost_centers'])} cost centers and no bill sheet is imported "
                      f"to split it (Master Data -> Import Store Sheet)")
        departments_by_cc = (info or {}).get("departments_by_cc", {})
        groups = [{
            "cost_center": suspense if reason else info["cost_centers"][0],
            "suspense_reason": reason,
            "amount": round(float(amount), 2),
            "location": account_location,
            "stores": [info["branch"]] if info and info["branch"] else [],
            "services": [],
            "departments": (list(dict.fromkeys(d for ds in departments_by_cc.values() for d in ds)) if reason
                            else departments_by_cc.get(_cc_key(info["cost_centers"][0]), [])),
        }]
    groups = [g for g in groups if g["amount"]] or groups[:1]
    groups.sort(key=lambda g: (g["suspense_reason"] is not None, -g["amount"]))
    for g in groups:
        g["cost_center"] = _normalize_cost_center(g["cost_center"], mappings) if g["cost_center"] else None
    return {"amount": float(amount), "groups": groups, "note": note}


def _month_sort_key(inv: dict):
    start, _ = _month_bounds(inv["month"])
    return start or inv["month"]


def _collapse_groups(groups: list) -> dict:
    """One line for an invoice whose cost-center lines would be identical (the template sends nothing per cost center)."""
    return {
        "cost_center": ", ".join(g["cost_center"] for g in groups if g["cost_center"]),
        "suspense_reason": None,  # no cost center is sent, so nothing is charged to suspense
        "amount": round(sum(g["amount"] for g in groups), 2),
        "location": groups[0]["location"] if len({g["location"] for g in groups}) == 1 else "Multiple Locations",
        "stores": [st for g in groups for st in g["stores"]],
        "services": [sv for g in groups for sv in g["services"]],
        "departments": list(dict.fromkeys(d for g in groups for d in g["departments"])),
    }


def _inv_key(inv: dict) -> tuple:
    return inv["account_number"], inv["month"]


def prepare_invoice(conn, inv: dict, mappings: dict, client: Optional[OracleClient]) -> dict:
    """The requisition for a single archived invoice (see `prepare_requisition`)."""
    return prepare_requisition(conn, [inv], mappings, client)


def prepare_requisition(conn, invs: list, mappings: dict, client: Optional[OracleClient]) -> dict:
    """
    Builds the requisition + attachment payloads for one carrier's invoices
    of one month (same entity / carrier / month, any number of accounts)
    from the payload template: the header once, then each invoice's own
    line(s) - one per cost center on that bill, see `_split_by_cost_center`
    - with one distribution each. Each invoice's PDF is attached to its line(s).
    """
    cfg = oracle_settings
    invs = sorted(invs, key=lambda i: (_month_sort_key(i), i["account_number"]))
    first = invs[0]
    entity, provider = first["entity"], first["provider"]
    multi = len(invs) > 1

    infos = {i["account_number"]: lookup_billing_info(entity, provider, i["account_number"]) for i in invs}
    per_invoice, notes, errors = [], [], []
    for inv in invs:
        prefix = f"{inv['account_number']} {_month_label(inv['month'])}: " if multi else ""
        try:
            amount = _resolve_amount(conn, inv)
            if amount is not None and amount <= 0:
                raise MappingError(f"Invoice amount is {amount}; nothing to requisition.")
            split = _split_by_cost_center(inv, amount, infos[inv["account_number"]], mappings)
        except MappingError as exc:
            errors.append(prefix + str(exc))
            continue
        per_invoice.append((inv, split["groups"]))
        if split["note"]:
            notes.append(prefix + split["note"])
    if errors:
        # One requisition per carrier and month: never send it with some of the invoices left out.
        raise MappingError(" | ".join(errors))
    template = get_payload_template(mappings)
    split_lines = template_splits_lines(template)
    note = "; ".join(notes) if split_lines and notes else None
    # The requisition's lines: each invoice gets its own line(s), so its PDF
    # can be attached to them - one per cost center on the bill, or a single
    # line when nothing on a line depends on the cost center.
    groups = []
    invoice_totals = {}
    invoice_dates = {_inv_key(inv): _resolve_invoice_date(conn, inv) for inv, _ in per_invoice}
    for inv, inv_groups in per_invoice:
        invoice_totals[_inv_key(inv)] = round(sum(g["amount"] for g in inv_groups), 2)
        if not split_lines:
            inv_groups = [_collapse_groups(inv_groups)]
        groups.extend({**g, "inv": inv} for g in inv_groups)
    amount = round(sum(invoice_totals.values()), 2)

    supplier = (mappings.get("suppliers", {}).get(entity, {}).get(provider)
                or mappings.get("suppliers", {}).get("*", {}).get(provider) or {})
    deliver_to = mappings.get("deliver_to_locations", {}).get(entity, {})
    deliver_to_ids = mappings.get("deliver_to_location_ids", {}).get(entity, {})
    month_start = _month_bounds(invs[0]["month"])[0]
    month_end = _month_bounds(invs[-1]["month"])[1]

    charge_cache: dict = {}

    def charge_account(g, key):
        cost_center = g["cost_center"]
        if not cost_center:
            return None
        dept_code = joined(g["departments"])
        if key == "segments":
            # The segments come from the pattern alone; the Oracle ID lookup is
            # only needed when the template sends {charge_account_id}.
            return _charge_segments(mappings.get("charge_account_templates", {}).get(entity), cost_center, dept_code)
        if (cost_center, dept_code) not in charge_cache:
            charge_cache[(cost_center, dept_code)] = _resolve_charge_account(conn, client, entity, cost_center, mappings, dept_code)
        account_id, segments = charge_cache[(cost_center, dept_code)]
        if key == "id" and account_id is None and segments and client is None:
            return f"<looked up in Oracle: {segments}>"  # dry run: shown, not sent
        return account_id if key == "id" else segments

    def joined(values, limit=6):
        values = list(dict.fromkeys(v for v in values if v))
        return ", ".join(values[:limit]) + (f" +{len(values) - limit} more" if len(values) > limit else "")

    months = list(dict.fromkeys(i["month"] for i in invs))
    month_labels = ", ".join(_month_label(m) for m in months)
    invoice_sources = {
        "entity": lambda: entity,
        "carrier": lambda: provider,
        "account_number": lambda: ", ".join(dict.fromkeys(i["account_number"] for i in invs)),
        "month": lambda: ", ".join(months),
        "month_label": lambda: month_labels,
        "invoice_months": lambda: month_labels,
        "invoice_count": lambda: len(invs),
        "month_start": lambda: month_start,
        "month_end": lambda: month_end,
        "today": lambda: datetime.now().strftime("%Y-%m-%d"),
        "invoice_date": lambda: max(invoice_dates.values()),
        "need_by_date": lambda: _need_by_date(max(invoice_dates.values()), cfg.need_by_days),
        "invoice_amount": lambda: amount,
        "filename": lambda: ", ".join(i["filename"] for i in invs),
        "invoice_status": lambda: ", ".join(dict.fromkeys(i.get("status") or "" for i in invs)),
        "bu_name": lambda: mappings.get("business_unit_names", {}).get(entity),
        "bu_id": lambda: mappings.get("business_units", {}).get(entity),
        "destination_org_id": lambda: mappings.get("destination_organizations", {}).get(entity),
        "supplier_name": lambda: supplier.get("supplier_name"),
        "supplier_site": lambda: supplier.get("supplier_site"),
        "supplier_id": lambda: supplier.get("supplier_id"),
        "supplier_site_id": lambda: supplier.get("supplier_site_id"),
        "preparer_email": lambda: cfg.preparer_email,
        "requester_email": lambda: cfg.requester_email or cfg.preparer_email,
        "preparer_id": lambda: cfg.preparer_id,
        "requester_id": lambda: cfg.requester_id or cfg.preparer_id,
        "line_type": lambda: cfg.line_type,
        "category": lambda: cfg.category_name,
        "currency": lambda: cfg.currency_code,
        "item": lambda: cfg.item_number,
        "quantity": lambda: int(cfg.quantity) if float(cfg.quantity).is_integer() else cfg.quantity,
    }

    def line_sources(index, g):
        inv = g["inv"]
        inv_start, inv_end = _month_bounds(inv["month"])
        return {
            # On a line, the invoice values describe that line's own invoice.
            "account_number": lambda: inv["account_number"],
            "month": lambda: inv["month"],
            "month_label": lambda: _month_label(inv["month"]),
            "invoice_months": lambda: _month_label(inv["month"]),
            "invoice_count": lambda: 1,
            "month_start": lambda: inv_start,
            "month_end": lambda: inv_end,
            "filename": lambda: inv["filename"],
            "invoice_status": lambda: inv.get("status"),
            "invoice_amount": lambda: invoice_totals[_inv_key(inv)],
            "invoice_date": lambda: invoice_dates[_inv_key(inv)],
            "need_by_date": lambda: _need_by_date(invoice_dates[_inv_key(inv)], cfg.need_by_days),
            "line_number": lambda: index,
            "amount": lambda: g["amount"],
            "cost_center": lambda: g["cost_center"],
            "location": lambda: g["location"],
            "branch": lambda: joined(g["stores"]),
            "service_numbers": lambda: joined(g["services"], limit=10),
            "line_count": lambda: len(g["services"]) or len(g["stores"]),
            "suspense_reason": lambda: g["suspense_reason"],
            "deliver_to_location": lambda: deliver_to.get(g["location"]) or deliver_to.get("*"),
            "deliver_to_location_id": lambda: deliver_to_ids.get(g["location"]) or deliver_to_ids.get("*"),
            "charge_account": lambda: charge_account(g, "segments"),
            "charge_account_id": lambda: charge_account(g, "id"),
            "dept_code": lambda: joined(g["departments"]),
        }

    # In the header, line values describe the whole requisition.
    locations = [infos[i["account_number"]]["location"] if infos[i["account_number"]] else "Unmapped" for i in invs]
    account_location = locations[0] if len(set(locations)) == 1 else "Multiple Locations"
    header_line_sources = {
        "line_number": lambda: 1,
        "amount": lambda: amount,
        "cost_center": lambda: joined([g["cost_center"] for g in groups]),
        "dept_code": lambda: joined([d for g in groups for d in g["departments"]]),
        "location": lambda: joined(locations),
        "branch": lambda: joined([s for g in groups for s in g["stores"]]),
        "service_numbers": lambda: joined([s for g in groups for s in g["services"]], limit=10),
        "line_count": lambda: sum(len(g["services"]) for g in groups),
        "suspense_reason": lambda: joined([g["suspense_reason"] for g in groups]),
        "deliver_to_location": lambda: deliver_to.get(locations[0]) or deliver_to.get("*"),
        "deliver_to_location_id": lambda: deliver_to_ids.get(locations[0]) or deliver_to_ids.get("*"),
        "charge_account": lambda: None,
        "charge_account_id": lambda: None,
    }

    def resolver(extra):
        cache: dict = {}

        def resolve(name):
            if name not in cache:
                source = extra.get(name) or invoice_sources.get(name)
                cache[name] = source() if source else None
            return cache[name]
        return resolve

    used_placeholders = _template_placeholders(template["line"]) | _template_placeholders(template["distribution"])
    errors = []
    header = _render_section(template["header"], resolver(header_line_sources), errors)
    lines = []
    for index, g in enumerate(groups, start=1):
        if g["cost_center"] is None:
            errors.append(f"needs the suspense cost center ({g['suspense_reason']}); set it in Mappings -> Per entity -> Suspense cost center")
            continue
        resolve = resolver(line_sources(index, g))
        line = _render_section(template["line"], resolve, errors)
        distribution = _render_section(template["distribution"], resolve, errors)
        if "LineNumber" in line:
            line["LineNumber"] = index
        # Only resolve (and possibly look up in Oracle) a charge account the template actually sends.
        g["charge_account_id"] = resolve("charge_account_id") if "charge_account_id" in used_placeholders else None
        g["charge_account"] = resolve("charge_account") if used_placeholders & {"charge_account", "charge_account_id"} else None
        lines.append({**line, "distributions": [distribution]})
    if errors:
        raise MappingError("; ".join(dict.fromkeys(errors)))
    if not header.get("Description"):
        raise MappingError("The payload template's header needs a Description (it is used to detect duplicates).")
    requisition = {**header, "lines": lines}

    leftovers = [
        f"{field} = {value!r}"
        for section in [header] + lines + [d for l in lines for d in l["distributions"]]
        for field, value in section.items()
        if isinstance(value, str) and value in SAMPLE_VALUES
    ]
    if leftovers:
        raise MappingError(f"Still has sample text from the example mappings: {', '.join(dict.fromkeys(leftovers))}; replace it in Oracle Sync -> Mappings.")

    attachments = [{
        "DatatypeCode": "FILE",
        "CategoryName": cfg.attachment_category,
        "FileName": inv["filename"],
        "Title": inv["filename"],
        "Description": "Attached automatically by system integration.",
        "FileContents": None,  # filled in just before upload
    } for inv in invs]
    suspense_reasons = [g["suspense_reason"] for g in groups if g["suspense_reason"]]
    real_ids = [g["charge_account_id"] for g in groups if g["charge_account_id"] and not str(g["charge_account_id"]).startswith("<")]
    return {
        "description": header["Description"],
        "description_is_unique": description_identifies_bill(template),
        "amount": amount,
        "invoices": [
            {"account_number": inv["account_number"], "month": inv["month"], "filename": inv["filename"],
             "status": inv.get("status"), "amount": invoice_totals[_inv_key(inv)], "invoice_date": invoice_dates[_inv_key(inv)],
             # the requisition lines this invoice's PDF is attached to
             "lines": [i for i, g in enumerate(groups, start=1) if g["inv"] is inv]}
            for inv, _ in per_invoice
        ],
        "location": account_location,
        "cost_center": ", ".join(g["cost_center"] for g in groups),
        "charge_account_id": ", ".join(map(str, real_ids)) or None,
        "charge_account_segments": ", ".join(g["charge_account"] for g in groups if g["charge_account"]) or None,
        "suspense_reason": "; ".join(suspense_reasons) or None,
        "allocation_note": note,
        "allocation": [
            {"line": i, "account_number": g["inv"]["account_number"], "month": g["inv"]["month"], "cost_center": g["cost_center"], "amount": g["amount"], "location": g["location"],
             "stores": joined(g["stores"], limit=20), "services": len(g["services"]), "dept_code": joined(g["departments"]), "suspense_reason": g["suspense_reason"]}
            for i, g in enumerate(groups, start=1)
        ],
        "requisition": requisition,
        "attachment": attachments[0],
        "attachments": attachments,
    }


def _read_pdf_base64(inv: dict) -> str:
    path = resolve_archived_invoice_path(
        inv["entity"], inv["provider"], inv["account_number"], inv["month"], inv["status"], inv["filename"]
    )
    if not path:
        raise MappingError("Invoice PDF is missing from the archive.")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


# ---- Alerts -------------------------------------------------------------

def _add_alert(conn, inv: dict, severity: str, message: str) -> dict:
    alert = {
        "created_at": _now(), "severity": severity, "entity": inv["entity"], "provider": inv["provider"],
        "account_number": inv["account_number"], "month": inv["month"], "message": message,
    }
    conn.execute(
        "INSERT INTO alerts (created_at, severity, entity, provider, account_number, month, message) VALUES (?,?,?,?,?,?,?)",
        tuple(alert.values()),
    )
    conn.commit()
    return alert


def _email_alerts(conn, alerts: list) -> Optional[str]:
    """Sends one summary email per sync run to the Procurement Administrator. Returns an error string on failure."""
    cfg = oracle_settings
    if not alerts or not (cfg.smtp_host and cfg.admin_email):
        return None
    msg = EmailMessage()
    msg["Subject"] = f"Telecom → Oracle PR sync: {len(alerts)} item(s) need attention"
    msg["From"] = cfg.smtp_from or cfg.smtp_username or cfg.admin_email
    msg["To"] = cfg.admin_email
    lines = [
        f"[{a['severity'].upper()}] {a['entity']} / {a['provider']} / account {a['account_number']} / {_month_label(a['month'])}: {a['message']}"
        for a in alerts
    ]
    msg.set_content("The monthly telecom invoice sync raised the following:\n\n" + "\n".join(lines))
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            if cfg.smtp_starttls:
                smtp.starttls()
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        return f"{type(exc).__name__}: {exc}"
    conn.execute("UPDATE alerts SET emailed = 1 WHERE emailed = 0")
    conn.commit()
    return None


# ---- Sync ---------------------------------------------------------------

def _preview(prepared: dict, invs: list) -> dict:
    attachments = []
    for inv, attachment in zip(invs, prepared["attachments"]):
        try:
            size = len(base64.b64decode(_read_pdf_base64(inv)))
            contents = f"<base64 of {size} bytes>"
        except MappingError:
            contents = "<PDF missing>"
        attachments.append(dict(attachment, FileContents=contents))
    return {"requisition": prepared["requisition"], "attachment": attachments[0], "attachments": attachments}


def _group_by_carrier_month(invoices: list) -> list:
    """Invoices grouped per (entity, carrier, month) - one requisition each - accounts in order."""
    groups: dict = {}
    for inv in sorted(invoices, key=lambda i: (_month_sort_key(i), i["account_number"])):
        groups.setdefault((inv["entity"], inv["provider"], inv["month"]), []).append(inv)
    return list(groups.values())


def _unit_ref(invs: list) -> dict:
    """How one requisition (a carrier's invoices for a month) is reported: accounts / files comma separated."""
    first = invs[0]
    return {
        "entity": first["entity"], "provider": first["provider"],
        "account_number": ", ".join(dict.fromkeys(i["account_number"] for i in invs)),
        "month": ", ".join(dict.fromkeys(i["month"] for i in invs)),
        "filename": ", ".join(i["filename"] for i in invs),
        "invoice_count": len(invs),
        "invoices": [{"account_number": i["account_number"], "month": i["month"], "filename": i["filename"],
                      "status": i.get("status")} for i in invs],
    }


def _split_list(value: Optional[str]) -> Optional[list]:
    """A month / account filter may list several values comma separated (as a requisition's result reports them)."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def sync_invoices(
    entity: Optional[str] = None,
    provider: Optional[str] = None,
    month: Optional[str] = None,
    status: Optional[str] = "Unpaid",
    dry_run: bool = False,
    account_number: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
    batch_id: Optional[str] = None,
    source: str = "manual",
    email_alerts: bool = True,
    wait_for_lock: float = 0,
) -> dict:
    """
    Pushes the matching archived invoices to Oracle (or previews them, when
    dry_run is set or ORACLE_LIVE_MODE is off) as ONE requisition per
    carrier per month: all of that carrier's pending invoices for the month
    (every account) become one PR, each invoice on its own line with its PDF
    attached to that line. `month` and `account_number` may each list
    several values comma separated.

    Invoices already submitted - or found in Oracle as an existing
    requisition - are skipped. A requisition that failed part-way resumes
    from the failed step with the invoices it was created for; invoices
    of that month added to the archive since then go on a new requisition.
    """
    live = oracle_settings.live_mode and not dry_run
    acquired = _sync_lock.acquire(timeout=wait_for_lock) if wait_for_lock else _sync_lock.acquire(blocking=False)
    if not acquired:
        raise RuntimeError("An Oracle sync is already running.")
    conn = _connect()
    client = None
    results, alerts = [], []
    own_batch = False
    email_error = None
    try:
        # A preview still runs before any mappings are saved, so it can show
        # per invoice which values are missing (and where to set them); a
        # live run needs them saved first.
        try:
            mappings = load_mappings()
        except MappingError:
            if live:
                raise
            mappings = {}
        if live:
            client = OracleClient(transport=transport)

        months = _split_list(month)
        accounts_filter = _split_list(account_number)
        invoices = list_archived_invoices(
            entity=entity, provider=provider,
            account_number=accounts_filter[0] if accounts_filter and len(accounts_filter) == 1 else None,
            month=months[0] if months and len(months) == 1 else None,
        )
        if months:
            invoices = [inv for inv in invoices if inv["month"] in months]
        if accounts_filter:
            invoices = [inv for inv in invoices if inv["account_number"] in accounts_filter]
        if status:
            invoices = [inv for inv in invoices if inv["status"] == status]
        requisitions = _group_by_carrier_month(invoices)
        if live and batch_id is None and requisitions:
            batch_id = _create_batch(conn, source, len(requisitions))
            own_batch = True

        for group in requisitions:
            # Already-finished invoices are reported per requisition they went on.
            done: dict = {}
            resume: dict = {}
            fresh = []
            for inv in group:
                ledger = _ledger_get(conn, inv)
                if ledger and (ledger["submitted"] or ledger["status"] == "duplicate" or (ledger["attached"] and not oracle_settings.auto_submit)):
                    done.setdefault((ledger["requisition_number"], ledger["status"]), []).append(inv)
                elif ledger and ledger["requisition_uniq_id"]:
                    resume.setdefault(ledger["requisition_uniq_id"], []).append(inv)
                else:
                    fresh.append(inv)
            for (req_number, req_status), invs in done.items():
                results.append({**_unit_ref(invs), "result": "skipped", "reason": f"already {req_status}", "requisition_number": req_number})
            for invs in list(resume.values()) + ([fresh] if fresh else []):
                try:
                    _sync_requisition(conn, client, invs, mappings, live, results, alerts)
                finally:
                    if client is not None:
                        _store_responses(conn, client, batch_id, invs)

        counts: dict = {}
        for r in results:
            counts[r["result"]] = counts.get(r["result"], 0) + 1
        if own_batch:
            _finish_batch(conn, batch_id, counts, "finished")
        email_error = _email_alerts(conn, alerts) if live and email_alerts else None
    finally:
        if client:
            client.close()
        conn.close()
        _sync_lock.release()

    return {
        "mode": "live" if live else "dry_run",
        "batch_id": batch_id if live else None,
        "counts": counts,
        "alerts": alerts if live else [
            {"severity": "warning", **{k: r[k] for k in ("entity", "provider", "account_number", "month")}, "message": r.get("suspense_reason") or r.get("reason")}
            for r in results if r.get("suspense_reason") or r["result"] == "failed"
        ],
        "alert_email_error": email_error,
        "results": results,
    }


def _sync_requisition(conn, client: Optional[OracleClient], invs: list, mappings: dict, live: bool,
                      results: list, alerts: list) -> None:
    """
    Creates (or resumes) the one requisition for `invs` - one carrier's
    invoices for one month - and appends its result. Each invoice keeps its own ledger
    row, all pointing at the same requisition, so attaching resumes per PDF.
    """
    ref = _unit_ref(invs)

    def put_all(**fields):
        for inv in invs:
            _ledger_put(conn, inv, **fields)

    try:
        prepared = prepare_requisition(conn, invs, mappings, client)
    except MappingError as exc:
        results.append({**ref, "result": "failed", "reason": str(exc)})
        if live:
            for inv in invs:
                _ledger_put(conn, inv, filename=inv["filename"], status="failed", message=str(exc))
            alerts.append(_add_alert(conn, ref, "error", str(exc)))
        return

    summary = {
        **ref,
        "amount": prepared["amount"],
        "invoices": prepared["invoices"],
        "location": prepared["location"],
        "cost_center": prepared["cost_center"],
        "charge_account_id": prepared["charge_account_id"],
        "charge_account_segments": prepared["charge_account_segments"],
        "suspense_reason": prepared["suspense_reason"],
        "allocation_note": prepared["allocation_note"],
        "allocation": prepared["allocation"],
    }

    if not live:
        results.append({**summary, "result": "preview", **_preview(prepared, invs)})
        return

    by_invoice = {_inv_key(i): i["amount"] for i in prepared["invoices"]}
    for inv in invs:
        _ledger_put(
            conn, inv, filename=inv["filename"], amount=by_invoice.get(_inv_key(inv)), cost_center=prepared["cost_center"],
            charge_account_id=prepared["charge_account_id"], used_suspense=1 if prepared["suspense_reason"] else 0,
        )
    ledgers = [_ledger_get(conn, inv) for inv in invs]
    uniq_id = next((l["requisition_uniq_id"] for l in ledgers if l["requisition_uniq_id"]), None)
    requisition_number = next((l["requisition_number"] for l in ledgers if l["requisition_number"]), None)
    try:
        if not uniq_id and prepared["description_is_unique"]:
            existing = client.find_requisition(prepared["requisition"])
            if existing:
                put_all(status="duplicate", requisition_number=existing.get("Requisition"),
                        message="Requisition with the same description already exists in Oracle.")
                results.append({**summary, "result": "skipped", "reason": "already exists in Oracle", "requisition_number": existing.get("Requisition")})
                return
        if not uniq_id:
            created = client.create_requisition(prepared["requisition"])
            uniq_id = _uniq_id_from_response(created)
            if not uniq_id:
                raise OracleError(f"Create succeeded but no requisition id in response: {json.dumps(created)[:500]}")
            requisition_number = created.get("Requisition")
            put_all(requisition_uniq_id=uniq_id, requisition_number=requisition_number, status="created", message=None)
            if prepared["suspense_reason"]:
                alerts.append(_add_alert(
                    conn, ref, "warning",
                    f"Requisition {requisition_number or uniq_id} charged to suspense cost center {prepared['cost_center']}: {prepared['suspense_reason']}.",
                ))

        # Each invoice's PDF goes on its own requisition line(s). A line that
        # already has the file (a run cut off part-way) isn't attached twice.
        pending = [(inv, info, attachment) for inv, ledger, info, attachment
                   in zip(invs, ledgers, prepared["invoices"], prepared["attachments"]) if not ledger["attached"]]
        if pending:
            line_ids = client.line_ids(uniq_id)
            for inv, info, attachment in pending:
                contents = _read_pdf_base64(inv)
                for line_number in info["lines"]:
                    line_id = line_ids.get(line_number)
                    if not line_id:
                        raise OracleError(f"Requisition {requisition_number or uniq_id} has no line {line_number} to attach {inv['filename']} to.")
                    if attachment["FileName"] in client.line_attachment_names(uniq_id, line_id):
                        continue
                    client.add_line_attachment(uniq_id, line_id, dict(attachment, FileContents=contents))
                    # Oracle answers 201 even when it drops the file (e.g. an
                    # unknown attachment category), so confirm it was kept.
                    if attachment["FileName"] not in client.line_attachment_names(uniq_id, line_id):
                        raise OracleError(
                            f"Oracle accepted {inv['filename']} for line {line_number} but did not keep it; check the "
                            f"attachment category '{attachment['CategoryName']}' is a valid category code "
                            "(Oracle Sync -> Settings -> Attachment category)."
                        )
                _ledger_put(conn, inv, attached=1, status="attached")

        if oracle_settings.auto_submit:
            client.submit_requisition(uniq_id)
            put_all(submitted=1, status="submitted")

        results.append({**summary, "result": "submitted" if oracle_settings.auto_submit else "attached", "requisition_number": requisition_number})
    except (OracleError, MappingError) as exc:
        put_all(status="failed", message=str(exc))
        alerts.append(_add_alert(conn, ref, "error", str(exc)))
        results.append({**summary, "result": "failed", "reason": str(exc), "requisition_number": requisition_number})


def run_scheduled_sync_if_due(today: Optional[datetime] = None) -> Optional[dict]:
    """
    The monthly trigger: once per calendar month, on or after
    ORACLE_SCHEDULE_DAY_OF_MONTH, syncs every Unpaid invoice not yet in
    Oracle. Returns the sync summary, or None if not due.
    """
    day = oracle_settings.schedule_day_of_month
    if day <= 0 or not oracle_settings.live_mode:
        return None
    today = today or datetime.now()
    if today.day < day:
        return None
    period = today.strftime("%Y-%m")
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM schedule_runs WHERE period = ?", (period,)).fetchone():
            return None
    finally:
        conn.close()

    summary = sync_invoices(status="Unpaid", source="schedule")
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO schedule_runs VALUES (?,?,?)", (period, _now(), json.dumps(summary["counts"])))
        conn.commit()
    finally:
        conn.close()
    return summary


# ---- Runs, stored Oracle responses, bulk create ------------------------

def _create_batch(conn, source: str, total: int) -> str:
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    conn.execute(
        "INSERT INTO sync_batches (id, source, started_at, status, total) VALUES (?,?,?,?,?)",
        (batch_id, source, _now(), "running", total),
    )
    conn.commit()
    return batch_id


def _finish_batch(conn, batch_id: str, counts: dict, status: str, message: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE sync_batches SET finished_at=?, status=?, created=?, failed=?, skipped=?, message=? WHERE id=?",
        (
            _now(), status, counts.get("submitted", 0) + counts.get("attached", 0),
            counts.get("failed", 0), counts.get("skipped", 0), message, batch_id,
        ),
    )
    conn.commit()


def _store_responses(conn, client: "OracleClient", batch_id: Optional[str], invs: list) -> None:
    """Moves the client's recorded calls into oracle_responses, tagged with the requisition's invoices."""
    if not client.calls:
        return
    calls, client.calls = client.calls, []
    ref = _unit_ref(invs)
    ledger = _ledger_get(conn, invs[0])
    for call in calls:
        response_text = call.get("response")
        header_id = None
        req_number = ledger["requisition_number"] if ledger else None
        if call["step"] == "create" and response_text:
            try:
                body = json.loads(response_text)
                header_id = body.get("RequisitionHeaderId")
                req_number = body.get("Requisition") or req_number
            except ValueError:
                pass
        conn.execute(
            """INSERT INTO oracle_responses
               (batch_id, created_at, entity, provider, account_number, month, step, method, path, http_status, ok,
                elapsed_ms, requisition_number, requisition_header_id, request_json, response_json, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                batch_id, _now(), ref["entity"], ref["provider"], ref["account_number"], ref["month"],
                call["step"], call["method"], call["path"], call.get("http_status"),
                0 if call.get("error") else 1, call.get("elapsed_ms"), req_number,
                str(header_id) if header_id is not None else (ledger["requisition_uniq_id"] if ledger else None),
                call.get("request"), response_text, call.get("error"),
            ),
        )
    conn.commit()


def list_batches(limit: int = 100) -> list:
    conn = _connect()
    try:
        # A run still marked "running" that this process isn't running was cut off by a restart.
        active = {bid for bid, state in _bulk_jobs.items() if state["status"] == "running"}
        for row in conn.execute("SELECT id FROM sync_batches WHERE status = 'running'").fetchall():
            if row["id"] not in active and not _sync_lock.locked():
                conn.execute("UPDATE sync_batches SET status='interrupted' WHERE id=?", (row["id"],))
        conn.commit()
        return [dict(r) for r in conn.execute("SELECT * FROM sync_batches ORDER BY started_at DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def list_responses(batch_id: Optional[str] = None, ok: Optional[bool] = None, step: Optional[str] = None,
                   search: Optional[str] = None, limit: int = 50, offset: int = 0) -> dict:
    conn = _connect()
    try:
        where, params = [], []
        if batch_id:
            where.append("batch_id = ?"); params.append(batch_id)
        if ok is not None:
            where.append("ok = ?"); params.append(1 if ok else 0)
        if step:
            where.append("step = ?"); params.append(step)
        if search:
            where.append("(account_number LIKE ? OR requisition_number LIKE ? OR error LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM oracle_responses {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, batch_id, created_at, entity, provider, account_number, month, step, method, path,
                       http_status, ok, elapsed_ms, requisition_number, requisition_header_id, error
                FROM oracle_responses {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total}
    finally:
        conn.close()


def get_response(response_id: int) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM oracle_responses WHERE id = ?", (response_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# In-memory progress of bulk runs started by this process (batch_id -> state).
_bulk_jobs: dict = {}


def start_bulk(refs: list, transport: Optional[httpx.BaseTransport] = None) -> str:
    """
    Creates requisitions for the selected invoices in a background thread -
    one requisition per carrier per month, covering every selected invoice
    of that carrier and month. Progress is read with `bulk_state`. Live mode only.
    """
    if not oracle_settings.live_mode:
        raise MappingError("Live mode is off (Oracle Sync -> Settings), so nothing can be created in Oracle.")
    if any(state["status"] == "running" for state in _bulk_jobs.values()):
        raise RuntimeError("A bulk run is already in progress.")
    # A selected requisition row stands for several accounts ("3502..., 3503...").
    refs = [{**r, "account_number": a, "month": m} for r in refs
            for a in (_split_list(r["account_number"]) or [r["account_number"]])
            for m in (_split_list(r["month"]) or [r["month"]])]
    if not refs:
        raise MappingError("Select at least one invoice.")
    conn = _connect()
    try:
        batch_id = _create_batch(conn, "bulk", len(_group_by_carrier_month(refs)))
    finally:
        conn.close()
    state = {
        "batch_id": batch_id, "status": "running", "started_at": _now(), "finished_at": None,
        "total": len(refs), "done": 0, "counts": {}, "current": None, "cancel": False,
        "items": [{**{k: r[k] for k in ("entity", "provider", "account_number", "month")}, "state": "queued"} for r in refs],
    }
    _bulk_jobs[batch_id] = state
    threading.Thread(target=_run_bulk, args=(state, transport), daemon=True).start()
    return batch_id


def _run_bulk(state: dict, transport) -> None:
    alerts: list = []
    try:
        requisitions: dict = {}
        for item in state["items"]:
            requisitions.setdefault((item["entity"], item["provider"], item["month"]), []).append(item)
        for (entity, provider, month), items in requisitions.items():
            if state["cancel"]:
                for item in items:
                    item["state"] = "cancelled"
                continue
            for item in items:
                item["state"] = "running"
            accounts = ", ".join(dict.fromkeys(item["account_number"] for item in items))
            state["current"] = {"entity": entity, "provider": provider, "account_number": accounts, "month": month}
            try:
                r = sync_invoices(
                    entity=entity, provider=provider, account_number=accounts, month=month,
                    status=None, batch_id=state["batch_id"], email_alerts=False,
                    wait_for_lock=300, transport=transport,
                )
                req_results = r["results"]
                alerts.extend(r["alerts"])
            except Exception as exc:  # noqa: BLE001 - one requisition failing must not stop the loop
                req_results = [{"result": "failed", "reason": f"{type(exc).__name__}: {exc}", "invoices": []}]
            for item in items:
                res = next((x for x in req_results if any(
                    (i["account_number"], i["month"]) == (item["account_number"], item["month"]) for i in x.get("invoices") or [])), None)
                if res is None:
                    res = req_results[0] if req_results and not req_results[0].get("invoices") else \
                        {"result": "failed", "reason": "Invoice PDF not found in the archive."}
                item.update(state="done", result=res["result"], reason=res.get("reason"), requisition_number=res.get("requisition_number"))
                state["counts"][res["result"]] = state["counts"].get(res["result"], 0) + 1
                state["done"] += 1
        state["status"] = "cancelled" if state["cancel"] else "finished"
    except Exception as exc:  # noqa: BLE001
        state["status"] = "error"
        state["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["current"] = None
        state["finished_at"] = _now()
        conn = _connect()
        try:
            _finish_batch(conn, state["batch_id"], state["counts"], state["status"], state.get("message"))
            email_error = _email_alerts(conn, alerts)
            if email_error:
                state["message"] = f"Alert email failed: {email_error}"
        finally:
            conn.close()


def bulk_state(batch_id: str) -> Optional[dict]:
    state = _bulk_jobs.get(batch_id)
    return {k: v for k, v in state.items() if k != "cancel"} if state else None


def active_bulk() -> Optional[dict]:
    for state in _bulk_jobs.values():
        if state["status"] == "running":
            return bulk_state(state["batch_id"])
    return None


def cancel_bulk(batch_id: str) -> bool:
    state = _bulk_jobs.get(batch_id)
    if not state or state["status"] != "running":
        return False
    state["cancel"] = True
    return True
