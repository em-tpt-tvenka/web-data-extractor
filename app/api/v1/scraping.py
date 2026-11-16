from typing import Annotated

from fastapi import APIRouter, Body, status

from app.orchestrator.job_manager import JobManager
from app.schemas.scraping import (
    _AMAZON_REQUEST_EXAMPLE,
    _REQUEST_EXAMPLE,
    JobCreatedResponse,
    ScrapeJobRequest,
)

router = APIRouter(prefix="/scraping", tags=["scraping"])
_manager = JobManager()

_EXAMPLES = {
    "alibaba": {
        "summary": "Alibaba product + review scrape",
        "value": _REQUEST_EXAMPLE,
    },
    "amazon": {
        "summary": "Amazon product + review scrape",
        "value": _AMAZON_REQUEST_EXAMPLE,
    },
}

@router.post("/jobs", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_scrape_job(
    request: Annotated[ScrapeJobRequest, Body(openapi_examples=_EXAMPLES)],
) -> JobCreatedResponse:
    return await _manager.create_job(request)
