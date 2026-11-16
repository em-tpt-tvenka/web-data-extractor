from __future__ import annotations

from pymongo.errors import DuplicateKeyError

from app.core import identifiers
from app.storage.database import get_db

_CONTEXT_ID = "default"

class ProvisioningRepository:
    @property
    def _col(self):
        return get_db().provisioning

    async def get_or_create_context(self) -> dict:
        existing = await self._col.find_one({"_id": _CONTEXT_ID})
        if existing:
            return existing

        doc = {
            "_id": _CONTEXT_ID,
            "tenant_id": identifiers.tenant_id(),
            "project_id": identifiers.project_id(),
        }
        try:
            await self._col.insert_one(doc)
            return doc
        except DuplicateKeyError:
            return await self._col.find_one({"_id": _CONTEXT_ID})
