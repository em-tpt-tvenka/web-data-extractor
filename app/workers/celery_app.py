from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "web_data_extractor",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Let the app's own logging config (app.core.logging) own the handlers so
    # engine/orchestrator logs (e.g. alibaba.engine events) show in the worker
    # console instead of being swallowed by Celery's root-logger hijack.
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
)
