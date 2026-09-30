from typing import Optional, List
from pydantic import BaseModel


class Account(BaseModel):
    account_number: str
    entity: str = "Ibraq"  # which business entity (Ibraq/Match/Salfa/Feelin) this login belongs to
    provider: str = "STC"  # carrier: STC / Mobily / Zain
    account_slot: int = 1  # which configured login/account under the entity+carrier this came from
    status: str  # "Paid" | "Unpaid" (Overdue is merged into Unpaid — status_detail still conveys it)
    status_detail: Optional[str] = None  # e.g. "Issued 21 days ago"
    amount: float
    due_date: str
    invoice_path: Optional[str] = None


class Analytics(BaseModel):
    paid_count: int = 0
    paid_amount: float = 0
    unpaid_count: int = 0
    unpaid_amount: float = 0


class CarrierAnalytics(Analytics):
    provider: str
    total_count: int = 0
    total_amount: float = 0


class EntityAnalytics(Analytics):
    entity: str
    total_count: int = 0
    total_amount: float = 0


class LocationAnalytics(Analytics):
    location: str  # a real city, or "Unspecified" / "Multiple Locations" / "Unmapped" (see master_data.py)
    total_count: int = 0
    total_amount: float = 0


class BranchAnalytics(Analytics):
    branch: str  # store/branch name from the master spreadsheet, or "Unmapped"
    total_count: int = 0
    total_amount: float = 0


class AccountSlot(BaseModel):
    slot: int
    label: str


class JobStatus(BaseModel):
    job_id: str
    entity: str
    provider: str
    account_slot: int
    status: str  # pending | running | waiting_otp | completed | failed
    message: Optional[str] = None
    accounts: Optional[List[Account]] = None


class OtpSubmission(BaseModel):
    otp: str


class RefreshRequest(BaseModel):
    entity: str
    provider: str
    account_slot: int = 1


class MasterAccountInput(BaseModel):
    """Fields editable via the Master Data CRUD UI — a row in master_accounts."""
    entity: Optional[str] = None
    carrier: Optional[str] = None
    location: Optional[str] = None
    cr: Optional[str] = None
    branch: Optional[str] = None
    department: Optional[str] = None
    connection: Optional[str] = None
    account_number: Optional[str] = None
    service_number: Optional[str] = None
    serial_number: Optional[str] = None
    status: Optional[str] = None
    package: Optional[str] = None
    notes: Optional[str] = None
    cost_center: Optional[str] = None
