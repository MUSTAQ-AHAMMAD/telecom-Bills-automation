import asyncio
import time
import uuid
from typing import Dict, Optional, List

from .models import Account

# If a job sits in "pending" (never advanced to "running") longer than this,
# treat it as dead rather than letting it block every future refresh forever.
# This is a safety net for bugs/crashes that leave a job stuck mid-startup.
STALE_PENDING_SECONDS = 60


class Job:
    def __init__(self, job_id: str, entity: str, provider: str, account_slot: int):
        self.job_id = job_id
        self.entity = entity
        self.provider = provider
        self.account_slot = account_slot
        self.status: str = "pending"  # pending|running|waiting_otp|completed|failed
        self.message: Optional[str] = None
        self.accounts: Optional[List[Account]] = None
        self.otp_event = asyncio.Event()
        self.otp_value: Optional[str] = None
        self.created_at = time.monotonic()

    def request_otp(self):
        self.status = "waiting_otp"
        self.message = "Waiting for OTP sent to your phone."

    async def wait_for_otp(self, timeout: int) -> str:
        self.request_otp()
        try:
            await asyncio.wait_for(self.otp_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.status = "failed"
            self.message = "Timed out waiting for OTP."
            raise
        self.otp_event.clear()
        otp = self.otp_value
        self.otp_value = None
        self.status = "running"
        self.message = "OTP received, continuing login..."
        return otp

    def submit_otp(self, otp: str):
        self.otp_value = otp
        self.otp_event.set()


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._active_job_id: Optional[str] = None

    def has_active_job(self) -> bool:
        if self._active_job_id is None:
            return False
        job = self._jobs.get(self._active_job_id)
        if job is None:
            return False
        if job.status == "pending" and time.monotonic() - job.created_at > STALE_PENDING_SECONDS:
            job.status = "failed"
            job.message = job.message or "Job never started (timed out before reaching the portal)."
            return False
        return job.status in ("pending", "running", "waiting_otp")

    def create_job(self, entity: str, provider: str, account_slot: int) -> Job:
        job_id = str(uuid.uuid4())
        job = Job(job_id, entity, provider, account_slot)
        self._jobs[job_id] = job
        self._active_job_id = job_id
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def clear_active_job(self) -> bool:
        """Force-clears whatever job is marked active. Returns True if one was cleared."""
        if self._active_job_id is None:
            return False
        job = self._jobs.get(self._active_job_id)
        if job and job.status in ("pending", "running", "waiting_otp"):
            job.status = "failed"
            job.message = "Cancelled manually."
        self._active_job_id = None
        return True


job_manager = JobManager()
