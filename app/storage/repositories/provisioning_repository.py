"""Tenant/project provisioning.

Tenant and project identities are platform-independent, provisioned once and
reused across all future executions. They are stored in the ``provisioning``
collection under a single context document. Record ids never embed these values.
"""

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
        """Return the {tenant_id, project_id} context, provisioning it on first
        use. Platform-independent and stable across executions."""
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
            # Another execution provisioned it first; use theirs.
            return await self._col.find_one({"_id": _CONTEXT_ID})
