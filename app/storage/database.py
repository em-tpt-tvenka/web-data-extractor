from __future__ import annotations

import asyncio

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings

_clients: dict[int, AsyncIOMotorClient] = {}

def get_client() -> AsyncIOMotorClient:
    loop = asyncio.get_event_loop()
    key = id(loop)
    client = _clients.get(key)
    if client is None:
        client = AsyncIOMotorClient(
            settings.mongo_uri, serverSelectionTimeoutMS=settings.mongo_timeout_ms
        )
        _clients[key] = client
    return client

def get_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.mongo_db]

async def ensure_indexes() -> None:
    db = get_db()
    await db.jobs.create_index("job_id", unique=True)
    await db.records.create_index("tenant.tenant_id")
    await db.records.create_index("source.platform")
    await db.records.create_index("deduplication.content_hash")
    await db.records.create_index("provenance.collection_run_id")
    await db.records.create_index(
        [("tenant.tenant_id", 1), ("source.platform", 1), ("source.source_item_id", 1)]
    )
