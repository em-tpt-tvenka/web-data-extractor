

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.core.logging import get_logger
from app.platforms.base import PlatformAdapter, ScrapeResult
from app.processing import normalizer
from app.schemas.record import SocialMentionRecord
from app.schemas.scraping import ScrapeJobRequest

_log = get_logger("incremental_store")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IncrementalStore:
    def __init__(
        self,
        adapter: PlatformAdapter,
        request: ScrapeJobRequest,
        run_id: str,
        *,
        db,
        records_repo,
        collections_repo,
        assign_identity: Callable[[list[SocialMentionRecord], ScrapeJobRequest], Awaitable[None]],
        export_dir: Path | None = None,
        batch_size: int = 500,
        resume_hours: float = 24.0,
    ) -> None:
        self.adapter, self.request, self.run_id = adapter, request, run_id
        self.db = db
        self.records_repo, self.collections_repo = records_repo, collections_repo
        self.assign_identity = assign_identity
        self.batch_size = max(1, batch_size)
        self.resume_hours = resume_hours
        self.jsonl: Path | None = None
        if export_dir is not None:
            export_dir.mkdir(parents=True, exist_ok=True)
            self.jsonl = export_dir / f"{request.platform.value}_{run_id}.partial.jsonl"
        self.flushed: set[str] = set()  # source_item_ids already written by this run
        self.mapped: dict[int, list[SocialMentionRecord]] = {}  # id(model) -> its records
        self.unique_stored: dict[str, int] = {}  # product_id -> review docs in MongoDB
        self.write_errors: list[str] = []
        self.batches_written = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer: asyncio.Task | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.collections_repo.ensure_indexes()
        self._writer = asyncio.create_task(self._write_loop())

    async def close(self) -> dict:
        """Flush everything queued and stop the writer."""
        if self._writer is not None:
            await self._queue.put(None)
            await self._writer
            self._writer = None
        return {"products_flushed": len(self.flushed), "batches_written": self.batches_written,
                "unique_reviews_stored": sum(self.unique_stored.values()),
                "write_errors": self.write_errors[:20]}

    def discard_partial_file(self) -> None:
        """The final export replaces the crash-safe partial file on success."""
        if self.jsonl is not None:
            try:
                self.jsonl.unlink(missing_ok=True)
            except Exception:
                pass

    # ── engine hook: resume ─────────────────────────────────────────────

    async def product_completed(self, platform: str, product_id: str) -> bool:
        doc = await self.db["products"].find_one({"_id": f"{platform}:{product_id}"}, {"last_seen_at": 1})
        if not doc or not doc.get("last_seen_at"):
            return False
        try:
            seen = datetime.fromisoformat(str(doc["last_seen_at"]))
        except ValueError:
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return _now() - seen <= timedelta(hours=self.resume_hours)

    # ── engine hook: incremental storage ────────────────────────────────

    async def product_scraped(self, platform: str, model: dict) -> None:
        records = await asyncio.to_thread(
            self.adapter.to_records, ScrapeResult(raw_products=[model]), self.request, self.run_id)
        self.mapped[id(model)] = records  # reused by the orchestrator instead of mapping twice
        await self._queue.put((platform, model, records))

    def records_for(self, models: list[dict]) -> tuple[list[SocialMentionRecord], list[dict]]:
        """Records already mapped for ``models``, and the models still to map."""
        done, todo = [], []
        for m in models:
            recs = self.mapped.get(id(m))
            if recs is None:
                todo.append(m)
            else:
                done.extend(recs)
        return done, todo

    async def _write_loop(self) -> None:
        pending: list[tuple[str, dict, list[SocialMentionRecord]]] = []
        pending_reviews = 0
        while True:
            item = await self._queue.get()
            if item is not None:
                pending.append(item)
                pending_reviews += sum(len(r.reviews or []) for r in item[2]) or 1
            if pending and (item is None or pending_reviews >= self.batch_size):
                await self._flush(pending)
                pending, pending_reviews = [], 0
            if item is None:
                return

    async def _flush(self, batch: list[tuple[str, dict, list[SocialMentionRecord]]]) -> None:
        records = [r for _, _, recs in batch for r in recs]
        try:
            await self.assign_identity(records, self.request)
            normalizer.finalize(records)
            await self.records_repo.upsert_many(records)
            await self.collections_repo.save_scraped_data(records)
        except Exception as exc:  # leave these for the end-of-run write; keep scraping
            msg = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            self.write_errors.append(msg)
            _log.error("event=incremental_flush_failed products=%d error=%s", len(batch), msg)
            return
        self.batches_written += 1
        for platform, model, recs in batch:
            pid = str(model.get("product_id") or (model.get("product_details") or {}).get("product_id"))
            self.flushed.update(r.source.source_item_id for r in recs)
            self.unique_stored[pid] = await self.db["reviews"].count_documents(
                {"platform": platform, "product_id": pid})
        self._append_jsonl(records)
        _log.info("event=incremental_flush products=%d reviews=%d batch=%d",
                  len(batch), sum(len(r.reviews or []) for r in records), self.batches_written)

    def _append_jsonl(self, records: list[SocialMentionRecord]) -> None:
        if self.jsonl is None:
            return
        try:
            with self.jsonl.open("a", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r.to_mongo(), ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            _log.warning("event=partial_jsonl_failed error=%s", exc)
