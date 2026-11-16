from fastapi import APIRouter

from app.orchestrator.job_manager import JobManager
from app.schemas.scraping import JobStatusResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])
_manager = JobManager()


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str) -> JobStatusResponse:
    return await _manager.get_job(job_id)
