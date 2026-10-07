import asyncio
import os
import re
import tempfile
import zipfile
from typing import Optional

# NOTE: on Windows, do NOT run this app with `--reload` (or workers > 1).
# uvicorn picks SelectorEventLoop instead of ProactorEventLoop whenever
# reload/multi-worker mode is on (needed for its own file-watcher), and
# SelectorEventLoop cannot spawn subprocesses — which is exactly how
# Playwright launches the browser, so every refresh fails with
# "NotImplementedError". Plain `uvicorn app.main:app --port 8000` (no
# --reload) gets ProactorEventLoop automatically and works correctly.
# Setting the event loop policy manually here does NOT help: uvicorn 0.46+
# selects the loop via an explicit factory function, bypassing the global
# asyncio policy entirely.

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from pydantic import ValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings, ENTITY_NAMES, CARRIER_NAMES, ENTITY_COLORS, CARRIER_COLORS, list_account_slots
from .data_store import load_accounts, clear_accounts, account_slot_index, lookup_account_slot
from .file_organizer import list_archived_invoices, resolve_archived_invoice_path, clear_all_invoices
from .jobs import job_manager
from . import live_control
from .master_data import (
    import_master_excel,
    import_store_sheet,
    list_bill_months,
    build_location_index,
    list_master_accounts,
    create_master_account,
    update_master_account,
    delete_master_account,
)
from .models import (
    Analytics, JobStatus, OtpSubmission, CarrierAnalytics, EntityAnalytics, LocationAnalytics,
    BranchAnalytics, RefreshRequest, MasterAccountInput, OracleSyncRequest, InvoiceRef, InvoiceAmountInput, BulkSyncRequest,
)
from .config import oracle_settings, save_oracle_settings
from . import oracle_sync

UNMAPPED_LOCATION = "Unmapped"  # account number doesn't appear in the master spreadsheet at all
UNMAPPED_BRANCH = "Unmapped"  # account maps to a location but the sheet left Branch/Store blank

app = FastAPI(title="Billing Automation Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _compute_analytics(accounts) -> Analytics:
    analytics = Analytics()
    for acc in accounts:
        # Unpaid and Overdue are merged into one bucket everywhere in the
        # dashboard — status_detail (e.g. "Issued 21 days ago") still
        # conveys how overdue something is, it's just not a separate total.
        if acc.status == "Paid":
            analytics.paid_count += 1
            analytics.paid_amount += acc.amount
        else:
            analytics.unpaid_count += 1
            analytics.unpaid_amount += acc.amount
    return analytics


def _compute_entity_breakdown(accounts) -> list[EntityAnalytics]:
    breakdown = []
    for entity in ENTITY_NAMES:
        entity_accounts = [a for a in accounts if a.entity == entity]
        base = _compute_analytics(entity_accounts)
        breakdown.append(
            EntityAnalytics(
                entity=entity,
                **base.model_dump(),
                total_count=len(entity_accounts),
                total_amount=sum(a.amount for a in entity_accounts),
            )
        )
    return breakdown


def _location_for(index: dict, provider: str, account_number: str):
    """Returns (location, branch, department) for one account, using the master.db-backed index."""
    entry = index.get((provider, account_number))
    if not entry:
        return UNMAPPED_LOCATION, None, None
    return entry["location"], entry.get("branch"), entry.get("department")


def _annotate_accounts_with_location(accounts) -> list[dict]:
    index = build_location_index()
    annotated = []
    for a in accounts:
        location, branch, department = _location_for(index, a.provider, a.account_number)
        record = a.model_dump()
        record["location"] = location
        record["branch"] = branch
        record["department"] = department
        annotated.append(record)
    return annotated


def _compute_location_breakdown(accounts) -> list[LocationAnalytics]:
    index = build_location_index()
    buckets: dict = {}
    for a in accounts:
        location, _, _ = _location_for(index, a.provider, a.account_number)
        buckets.setdefault(location, []).append(a)

    breakdown = []
    for location, bucket_accounts in buckets.items():
        base = _compute_analytics(bucket_accounts)
        breakdown.append(
            LocationAnalytics(
                location=location,
                **base.model_dump(),
                total_count=len(bucket_accounts),
                total_amount=sum(a.amount for a in bucket_accounts),
            )
        )
    breakdown.sort(key=lambda x: x.total_amount, reverse=True)
    return breakdown


def _compute_branch_breakdown(accounts) -> list[BranchAnalytics]:
    index = build_location_index()
    buckets: dict = {}
    for a in accounts:
        _, branch, _ = _location_for(index, a.provider, a.account_number)
        buckets.setdefault(branch or UNMAPPED_BRANCH, []).append(a)

    breakdown = []
    for branch, bucket_accounts in buckets.items():
        base = _compute_analytics(bucket_accounts)
        breakdown.append(
            BranchAnalytics(
                branch=branch,
                **base.model_dump(),
                total_count=len(bucket_accounts),
                total_amount=sum(a.amount for a in bucket_accounts),
            )
        )
    breakdown.sort(key=lambda x: x.total_amount, reverse=True)
    return breakdown


def _compute_carrier_breakdown(accounts) -> list[CarrierAnalytics]:
    breakdown = []
    for carrier in CARRIER_NAMES:
        carrier_accounts = [a for a in accounts if a.provider == carrier]
        base = _compute_analytics(carrier_accounts)
        breakdown.append(
            CarrierAnalytics(
                provider=carrier,
                **base.model_dump(),
                total_count=len(carrier_accounts),
                total_amount=sum(a.amount for a in carrier_accounts),
            )
        )
    return breakdown


@app.get("/api/entities")
def get_entities():
    """
    Full nested config for the frontend's three-level nav: Entity (Ibraq /
    Match / Salfa / Feelin) -> Carrier (STC / Mobily / Zain) -> account
    slots (individual logins configured for that entity+carrier pair).
    """
    return {
        "entities": ENTITY_NAMES,
        "carriers": CARRIER_NAMES,
        "entity_colors": ENTITY_COLORS,
        "carrier_colors": CARRIER_COLORS,
        "accounts": {
            entity: {carrier: list_account_slots(entity, carrier) for carrier in CARRIER_NAMES}
            for entity in ENTITY_NAMES
        },
    }


@app.get("/api/accounts")
def get_accounts(entity: Optional[str] = None, provider: Optional[str] = None, account_slot: Optional[int] = None):
    accounts = load_accounts()
    entity_scoped = [a for a in accounts if entity is None or a.entity == entity]
    filtered = [
        a for a in entity_scoped
        if (provider is None or a.provider == provider)
        and (account_slot is None or a.account_slot == account_slot)
    ]
    return {
        "accounts": _annotate_accounts_with_location(filtered),
        "analytics": _compute_analytics(filtered),
        "by_provider": _compute_carrier_breakdown(entity_scoped),
        "by_entity": _compute_entity_breakdown(entity_scoped),
        "by_location": _compute_location_breakdown(entity_scoped),
        "by_branch": _compute_branch_breakdown(entity_scoped),
    }


@app.post("/api/refresh")
async def start_refresh(payload: RefreshRequest):
    if payload.entity not in ENTITY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown entity '{payload.entity}'.")
    if payload.provider not in CARRIER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown carrier '{payload.provider}'.")
    if job_manager.has_active_job():
        raise HTTPException(status_code=409, detail="A refresh job is already running.")

    import asyncio

    from .automation import run_scrape_job  # imported lazily: pulls in Playwright

    # Only mark a job "active" once we know the automation coroutine can
    # actually be scheduled — otherwise a startup failure (e.g. Playwright
    # not installed) would leave has_active_job() permanently true, since
    # the job would be created but never progress past "pending".
    job = job_manager.create_job(payload.entity, payload.provider, payload.account_slot)
    try:
        asyncio.create_task(run_scrape_job(job, payload.entity, payload.provider, payload.account_slot))
    except Exception as exc:  # noqa: BLE001 - never leave the job stuck "pending"
        job.status = "failed"
        job.message = f"Failed to start automation: {exc}"
        raise HTTPException(status_code=500, detail=job.message)

    return {"job_id": job.job_id, "entity": job.entity, "provider": job.provider, "account_slot": job.account_slot}


@app.post("/api/jobs/reset")
def reset_stuck_job():
    """
    Safety valve: clears whatever job JobManager considers "active" without
    restarting the server. Use this if a job gets stuck (e.g. the backend
    process was interrupted mid-automation) and refresh keeps returning
    "A refresh job is already running."
    """
    cleared = job_manager.clear_active_job()
    return {"cleared": cleared}


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job_status(job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobStatus(
        job_id=job.job_id,
        entity=job.entity,
        provider=job.provider,
        account_slot=job.account_slot,
        status=job.status,
        message=job.message,
        accounts=job.accounts,
    )


@app.post("/api/jobs/{job_id}/otp")
def submit_otp(job_id: str, payload: OtpSubmission):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "waiting_otp":
        raise HTTPException(status_code=400, detail="Job is not waiting for an OTP.")
    job.submit_otp(payload.otp)
    return {"ok": True}


@app.websocket("/ws/jobs/{job_id}/live")
async def job_live_control(websocket: WebSocket, job_id: str):
    """
    Streams the automation browser's screen (via CDP screencast) to a remote
    viewer and relays their mouse/keyboard input back into it — lets someone
    on a different machine than the server directly see and drive the
    browser doing a portal login, instead of only relaying OTP codes through
    the normal form. Only one viewer at a time per job.
    """
    await websocket.accept()
    session = live_control.attach_viewer(job_id, websocket)
    if not session:
        await websocket.send_json({
            "type": "error",
            "message": "No active browser for this job (it may have finished), or someone else is already viewing it.",
        })
        await websocket.close()
        return

    cdp = session["cdp"]

    def on_frame(params):
        async def relay():
            try:
                await websocket.send_json({
                    "type": "frame",
                    "data": params["data"],
                    "width": params["metadata"].get("deviceWidth"),
                    "height": params["metadata"].get("deviceHeight"),
                })
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass
        asyncio.create_task(relay())

    cdp.on("Page.screencastFrame", on_frame)
    session["streaming"] = True
    try:
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "everyNthFrame": 1})
        while True:
            msg = await websocket.receive_json()
            await live_control.dispatch_input(cdp, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        session["streaming"] = False
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        cdp.remove_listener("Page.screencastFrame", on_frame)
        live_control.detach_viewer(job_id)


@app.get("/api/invoice/{account_number}")
def download_invoice(account_number: str):
    accounts = load_accounts()
    match = next((a for a in accounts if a.account_number == account_number), None)
    if not match or not match.invoice_path or not os.path.exists(match.invoice_path):
        raise HTTPException(status_code=404, detail="Invoice not found for this account.")
    return FileResponse(match.invoice_path, filename=os.path.basename(match.invoice_path))


@app.get("/api/invoices")
def get_invoices(
    entity: Optional[str] = None,
    provider: Optional[str] = None,
    account_number: Optional[str] = None,
    month: Optional[str] = None,
    account_slot: Optional[int] = None,
):
    """
    Lists every downloaded invoice file found on disk under DOWNLOAD_ROOT —
    this is independent of accounts.json, so it survives server restarts
    and shows every previously downloaded month (the "archive"), not just
    whatever the most recent scrape's current bill happened to be. Status
    is always Paid/Unpaid (legacy Overdue folders are merged into Unpaid).
    """
    invoices = list_archived_invoices(entity=entity, provider=provider, account_number=account_number, month=month)

    index = build_location_index()
    slots = account_slot_index()
    for inv in invoices:
        location, branch, _ = _location_for(index, inv["provider"], inv["account_number"])
        inv["location"] = location
        inv["branch"] = branch
        # Which login (e.g. STC "Account 2") the number belongs to; None if
        # it was never seen in a scrape since slot tracking was added.
        inv["account_slot"] = lookup_account_slot(slots, inv["entity"], inv["provider"], inv["account_number"])
    if account_slot is not None:
        invoices = [inv for inv in invoices if inv["account_slot"] == account_slot]

    months = sorted({inv["month"] for inv in invoices}, reverse=True)
    return {"invoices": invoices, "months": months}


@app.post("/api/master/import")
def import_master(path: Optional[str] = None):
    """
    Refreshes data/master.db from the TELECOMMUNICATION MASTER spreadsheet
    — the source of Location/Branch info joined onto accounts and invoices
    above. Uses MASTER_EXCEL_PATH from .env unless a `path` override is
    given. Rows added via the Master Data CRUD screen, and any spreadsheet
    row a person has since hand-edited there, are preserved (see
    `import_master_excel`) — only untouched spreadsheet-sourced rows are
    replaced.
    """
    excel_path = path or settings.master_excel_path
    if not excel_path:
        raise HTTPException(
            status_code=400,
            detail="No master spreadsheet path configured (set MASTER_EXCEL_PATH in .env, or pass ?path=).",
        )
    try:
        summary = import_master_excel(excel_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"summary": summary}


@app.post("/api/master/import-store-sheet")
def import_store_sheet_endpoint(path: str):
    """
    Applies a supplementary flat "store sheet" (CR/Store/Service/Acc/Amount/
    Cost center/Dept columns, no Entity/Carrier of its own) onto existing
    master_accounts rows, matched by service number / account number — see
    `master_data.import_store_sheet` for the exact matching rules.
    """
    try:
        result = import_store_sheet(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


def _save_upload(upload: UploadFile) -> str:
    if not (upload.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an Excel .xlsx file.")
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    with os.fdopen(fd, "wb") as f:
        f.write(upload.file.read())
    return tmp_path


@app.post("/api/master/import-upload")
def import_master_upload(file: UploadFile = File(...)):
    """Same as POST /api/master/import, but with the TELECOMMUNICATION MASTER workbook uploaded from the dashboard."""
    tmp_path = _save_upload(file)
    try:
        return {"summary": import_master_excel(tmp_path)}
    finally:
        os.remove(tmp_path)


@app.post("/api/master/import-store-sheet-upload")
def import_store_sheet_upload(file: UploadFile = File(...), bill_month: Optional[str] = Form(None)):
    """
    Same as POST /api/master/import-store-sheet, but with the sheet uploaded
    from the dashboard. With `bill_month` (e.g. "September_2026") its Amount
    column is also stored as that month's per-line bill, used to split
    requisitions by cost center.
    """
    if bill_month and not re.fullmatch(r"[A-Z][a-z]+_\d{4}", bill_month):
        raise HTTPException(status_code=400, detail=f"Bill month must look like September_2026, got '{bill_month}'.")
    tmp_path = _save_upload(file)
    try:
        return import_store_sheet(tmp_path, bill_month=bill_month or None)
    finally:
        os.remove(tmp_path)


# ---- Master Data CRUD ----
# The Location/Branch/Department inventory (data/master.db) is normally
# populated by importing the spreadsheet above, but can also be browsed and
# edited by hand from the dashboard's Master Data section. Re-running
# POST /api/master/import no longer wipes manual edits — see
# `master_data.import_master_excel`.

@app.get("/api/master/bill-months")
def get_bill_months():
    """Months whose per-line bill amounts have been imported."""
    return {"months": list_bill_months()}


@app.get("/api/master/accounts")
def get_master_accounts(
    entity: Optional[str] = None,
    provider: Optional[str] = None,
    location: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    result = list_master_accounts(entity=entity, carrier=provider, location=location, search=search, limit=limit, offset=offset)
    return result


@app.post("/api/master/accounts")
def post_master_account(payload: MasterAccountInput):
    new_id = create_master_account(payload.model_dump())
    return {"id": new_id}


@app.put("/api/master/accounts/{row_id}")
def put_master_account(row_id: int, payload: MasterAccountInput):
    updated = update_master_account(row_id, payload.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Master account row not found.")
    return {"ok": True}


@app.delete("/api/master/accounts/{row_id}")
def delete_master_account_endpoint(row_id: int):
    deleted = delete_master_account(row_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Master account row not found.")
    return {"ok": True}


@app.get("/api/invoices/file")
def download_archived_invoice(entity: str, provider: str, account_number: str, month: str, status: str, filename: str):
    path = resolve_archived_invoice_path(entity, provider, account_number, month, status, filename)
    if not path:
        raise HTTPException(status_code=404, detail="Invoice file not found.")
    return FileResponse(path, filename=filename)


@app.post("/api/data/clear")
def clear_data():
    """
    Permanently wipes both the downloaded invoice archive on disk (every
    entity/carrier/account/month) and the latest-scrape snapshot
    (accounts.json, the Dashboard's cards/table) back to empty — an
    irreversible, destructive action the frontend must confirm before
    calling. Leaves master.db (Location/Branch/Cost Center directory)
    untouched; the Dashboard repopulates on the next Refresh.
    """
    result = clear_all_invoices()
    clear_accounts()
    return result


@app.get("/api/download-all")
def download_all(entity: Optional[str] = None, provider: Optional[str] = None, status: Optional[str] = None):
    """
    Zips every matching invoice PDF flat — just the files, no Entity/Carrier/
    Account/Month/Bills/Status folder structure — since that's what people
    actually want out of a bulk download. Filenames are already unique
    (<account_number>_<month>.pdf), but a numeric suffix is added on the
    rare chance two matching invoices would otherwise collide.
    """
    if status and status not in ("Paid", "Unpaid"):
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'.")
    if entity and entity not in ENTITY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown entity '{entity}'.")
    if provider and provider not in CARRIER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown carrier '{provider}'.")

    invoices = list_archived_invoices(entity=entity, provider=provider)
    if status:
        invoices = [inv for inv in invoices if inv["status"] == status]
    if not invoices:
        raise HTTPException(status_code=404, detail="No downloaded invoices match that selection.")

    name_parts = [p for p in (entity, provider, status) if p]
    filename = "_".join(part.lower() for part in name_parts + ["invoices"]) + ".zip"

    tmp_dir = tempfile.mkdtemp()
    archive_path = os.path.join(tmp_dir, filename)
    used_names: set = set()
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for inv in invoices:
            src = resolve_archived_invoice_path(
                inv["entity"], inv["provider"], inv["account_number"], inv["month"], inv["status"], inv["filename"]
            )
            if not src:
                continue
            arcname = inv["filename"]
            if arcname in used_names:
                base, ext = os.path.splitext(arcname)
                suffix = 2
                while f"{base} ({suffix}){ext}" in used_names:
                    suffix += 1
                arcname = f"{base} ({suffix}){ext}"
            used_names.add(arcname)
            zf.write(src, arcname)

    return FileResponse(archive_path, filename=filename, media_type="application/zip")


# ---- Oracle Fusion Procurement sync ----
# Pushes archived invoices to Oracle as Purchase Requisitions — see
# app/oracle_sync.py. Nothing reaches Oracle unless ORACLE_LIVE_MODE=true.

ORACLE_SCHEDULE_CHECK_SECONDS = 3600


@app.on_event("startup")
async def start_oracle_schedule():
    async def loop():
        while True:
            try:
                await asyncio.to_thread(oracle_sync.run_scheduled_sync_if_due)
            except Exception as exc:  # noqa: BLE001 - a failed run must not kill the schedule
                print(f"[oracle-sync] scheduled run failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(ORACLE_SCHEDULE_CHECK_SECONDS)

    # Always started: the schedule day / live mode can be changed from the
    # dashboard at any time, and run_scheduled_sync_if_due checks both.
    asyncio.create_task(loop())


@app.get("/api/oracle/status")
def oracle_status():
    try:
        oracle_sync.load_mappings()
        mappings_error = None
    except oracle_sync.MappingError as exc:
        mappings_error = str(exc)
    return {
        "live_mode": oracle_settings.live_mode,
        "auto_submit": oracle_settings.auto_submit,
        "connection_configured": bool(oracle_settings.base_url and oracle_settings.username and oracle_settings.password),
        "preparer_configured": bool(oracle_settings.preparer_email or oracle_settings.preparer_id),
        "mappings_error": mappings_error,
        "schedule_day_of_month": oracle_settings.schedule_day_of_month,
        "alert_email_configured": bool(oracle_settings.smtp_host and oracle_settings.admin_email),
    }


@app.post("/api/oracle/sync")
def oracle_sync_endpoint(payload: OracleSyncRequest):
    if payload.entity and payload.entity not in ENTITY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown entity '{payload.entity}'.")
    if payload.provider and payload.provider not in CARRIER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown carrier '{payload.provider}'.")
    if payload.status and payload.status not in ("Paid", "Unpaid"):
        raise HTTPException(status_code=400, detail=f"Unknown status '{payload.status}'.")
    try:
        return oracle_sync.sync_invoices(
            entity=payload.entity, provider=payload.provider, month=payload.month,
            status=payload.status, dry_run=payload.dry_run, account_number=payload.account_number,
            source="single" if payload.account_number else "manual",
        )
    except oracle_sync.MappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/oracle/bulk")
def oracle_bulk_start(payload: BulkSyncRequest):
    """Starts creating the selected invoices in Oracle, one at a time, in the background."""
    try:
        batch_id = oracle_sync.start_bulk([inv.model_dump() for inv in payload.invoices])
    except oracle_sync.MappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return oracle_sync.bulk_state(batch_id)


@app.get("/api/oracle/bulk/active")
def oracle_bulk_active():
    return {"job": oracle_sync.active_bulk()}


@app.get("/api/oracle/bulk/{batch_id}")
def oracle_bulk_status(batch_id: str):
    state = oracle_sync.bulk_state(batch_id)
    if not state:
        raise HTTPException(status_code=404, detail="That bulk run isn't running in this server process (see Oracle Responses).")
    return state


@app.post("/api/oracle/bulk/{batch_id}/cancel")
def oracle_bulk_cancel(batch_id: str):
    if not oracle_sync.cancel_bulk(batch_id):
        raise HTTPException(status_code=404, detail="No running bulk run with that id.")
    return {"ok": True}


@app.get("/api/oracle/batches")
def oracle_batches(limit: int = 100):
    return {"batches": oracle_sync.list_batches(limit=limit)}


@app.get("/api/oracle/responses")
def oracle_responses(batch_id: Optional[str] = None, outcome: Optional[str] = None, step: Optional[str] = None,
                     search: Optional[str] = None, limit: int = 50, offset: int = 0):
    ok = True if outcome == "ok" else False if outcome == "error" else None
    return oracle_sync.list_responses(batch_id=batch_id, ok=ok, step=step, search=search, limit=limit, offset=offset)


@app.get("/api/oracle/responses/{response_id}")
def oracle_response_detail(response_id: int):
    row = oracle_sync.get_response(response_id)
    if not row:
        raise HTTPException(status_code=404, detail="Response not found.")
    return row


@app.get("/api/oracle/sync-log")
def oracle_sync_log(entity: Optional[str] = None, status: Optional[str] = None):
    return {"rows": oracle_sync.list_sync_log(entity=entity, status=status)}


@app.get("/api/oracle/alerts")
def oracle_alerts(limit: int = 200):
    return {"alerts": oracle_sync.list_alerts(limit=limit)}


ORACLE_SECRET_FIELDS = {"password", "smtp_password"}


@app.get("/api/oracle/settings")
def get_oracle_settings():
    """Current Oracle settings for the dashboard form; passwords are never returned, only whether they're set."""
    data = oracle_settings.model_dump(exclude={"mappings_path"})
    for field in ORACLE_SECRET_FIELDS:
        data[f"{field}_set"] = bool(data.pop(field))
    return data


@app.put("/api/oracle/settings")
def put_oracle_settings(changes: dict):
    # A blank password field in the form means "keep the current one".
    for field in ORACLE_SECRET_FIELDS:
        if not changes.get(field):
            changes.pop(field, None)
    try:
        save_oracle_settings(changes)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
    return get_oracle_settings()


@app.get("/api/oracle/mappings")
def get_oracle_mappings():
    return oracle_sync.get_mappings_for_editing()


@app.put("/api/oracle/mappings")
def put_oracle_mappings(mappings: dict):
    try:
        oracle_sync.save_mappings(mappings)
    except oracle_sync.MappingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/oracle/test-connection")
def oracle_test_connection():
    try:
        return oracle_sync.test_connection()
    except (oracle_sync.MappingError, oracle_sync.OracleError) as exc:
        return {"ok": False, "message": str(exc)}


@app.get("/api/oracle/invoice-states")
def oracle_invoice_states():
    return {"rows": oracle_sync.invoice_states()}


@app.put("/api/oracle/invoice-amount")
def oracle_set_invoice_amount(payload: InvoiceAmountInput):
    if payload.amount < 0:
        raise HTTPException(status_code=400, detail="Amount cannot be negative.")
    oracle_sync.set_invoice_amount(payload.entity, payload.provider, payload.account_number, payload.month, payload.amount)
    return {"ok": True}


@app.post("/api/oracle/sync-log/reset")
def oracle_reset_ledger_entry(payload: InvoiceRef):
    if not oracle_sync.reset_ledger_entry(payload.entity, payload.provider, payload.account_number, payload.month):
        raise HTTPException(status_code=404, detail="No sync record for that invoice.")
    return {"ok": True}


@app.delete("/api/oracle/alerts")
def oracle_clear_alerts():
    return {"deleted": oracle_sync.clear_alerts()}


# Serve the single-page frontend
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
