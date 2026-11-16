from typing import Annotated

from fastapi import APIRouter, Body, status

from app.orchestrator.job_manager import JobManager
from app.schemas.scraping import _REQUEST_EXAMPLE, JobCreatedResponse, ScrapeJobRequest

router = APIRouter(prefix="/scraping", tags=["scraping"])
_manager = JobManager()

# Route-level example: FastAPI emits openapi_examples verbatim, so the key order
# (platform first, then search, filters, scraping_options, platform_options) is
# preserved in the docs instead of being alphabetized by the schema generator.
_EXAMPLES = {
    "alibaba": {
        "summary": "Alibaba product + review scrape",
        "value": _REQUEST_EXAMPLE,
    }
}


@router.post("/jobs", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_scrape_job(
    request: Annotated[ScrapeJobRequest, Body(openapi_examples=_EXAMPLES)],
) -> JobCreatedResponse:
    return await _manager.create_job(request)
