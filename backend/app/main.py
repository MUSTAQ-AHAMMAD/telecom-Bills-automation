import asyncio
import os
import shutil
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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings, ENTITY_NAMES, CARRIER_NAMES, ENTITY_COLORS, CARRIER_COLORS, list_account_slots
from .data_store import load_accounts, clear_accounts
from .file_organizer import list_archived_invoices, resolve_archived_invoice_path, clear_all_invoices
from .jobs import job_manager
from . import live_control
from .master_data import (
    import_master_excel,
    import_store_sheet,
    build_location_index,
    list_master_accounts,
    create_master_account,
    update_master_account,
    delete_master_account,
)
from .models import (
    Analytics, JobStatus, OtpSubmission, CarrierAnalytics, EntityAnalytics, LocationAnalytics,
    BranchAnalytics, RefreshRequest, MasterAccountInput,
)

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
    for inv in invoices:
        location, branch, _ = _location_for(index, inv["provider"], inv["account_number"])
        inv["location"] = location
        inv["branch"] = branch

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
    master_accounts rows, matched by account number — see
    `master_data.import_store_sheet` for the exact matching rules.
    """
    try:
        result = import_store_sheet(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


# ---- Master Data CRUD ----
# The Location/Branch/Department inventory (data/master.db) is normally
# populated by importing the spreadsheet above, but can also be browsed and
# edited by hand from the dashboard's Master Data section. Re-running
# POST /api/master/import no longer wipes manual edits — see
# `master_data.import_master_excel`.

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
    if status and status not in ("Paid", "Unpaid"):
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'.")
    if entity and entity not in ENTITY_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown entity '{entity}'.")
    if provider and provider not in CARRIER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown carrier '{provider}'.")

    tmp_dir = tempfile.mkdtemp()
    name_parts = [p for p in (entity, provider, status) if p]
    filename = "_".join(part.lower() for part in name_parts + ["invoices"]) + ".zip"

    if status:
        # Status filters a specific bucket, so build the zip file-by-file
        # instead of zipping a whole folder tree (which would include every
        # status).
        invoices = list_archived_invoices(entity=entity, provider=provider)
        invoices = [inv for inv in invoices if inv["status"] == status]
        if not invoices:
            raise HTTPException(status_code=404, detail=f"No {status.lower()} invoices found.")

        archive_path = os.path.join(tmp_dir, filename)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for inv in invoices:
                src = resolve_archived_invoice_path(
                    inv["entity"], inv["provider"], inv["account_number"], inv["month"], inv["status"], inv["filename"]
                )
                if not src:
                    continue
                arcname = os.path.join(
                    inv["entity"], inv["provider"], inv["account_number"], inv["month"], "Bills", inv["status"], inv["filename"]
                )
                zf.write(src, arcname)
        return FileResponse(archive_path, filename=filename, media_type="application/zip")

    root = os.path.abspath(settings.download_root)
    if entity:
        root = os.path.join(root, entity)
        if provider:
            root = os.path.join(root, provider)
    elif provider:
        # A carrier filter with no entity spans every entity's copy of that
        # carrier, so build the zip file-by-file instead of a single folder.
        invoices = list_archived_invoices(provider=provider)
        if not invoices:
            raise HTTPException(status_code=404, detail="No downloaded invoices yet.")
        archive_path = os.path.join(tmp_dir, filename)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for inv in invoices:
                src = resolve_archived_invoice_path(
                    inv["entity"], inv["provider"], inv["account_number"], inv["month"], inv["status"], inv["filename"]
                )
                if not src:
                    continue
                arcname = os.path.join(
                    inv["entity"], inv["provider"], inv["account_number"], inv["month"], "Bills", inv["status"], inv["filename"]
                )
                zf.write(src, arcname)
        return FileResponse(archive_path, filename=filename, media_type="application/zip")

    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="No downloaded invoices yet.")

    archive_base = os.path.join(tmp_dir, "invoices")
    archive_path = shutil.make_archive(archive_base, "zip", root)
    return FileResponse(archive_path, filename=filename, media_type="application/zip")


# Serve the single-page frontend
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
