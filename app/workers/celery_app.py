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
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
)
