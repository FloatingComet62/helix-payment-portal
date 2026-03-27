from __future__ import annotations

import secrets
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import FestAlreadyExistsError, FestNotFoundError
from app.models.fest import FestCreate, FestDB, FestPublic, FestUpdate


def _doc_to_fest_db(doc: dict) -> FestDB:
    doc.pop("_id", None)
    return FestDB(**doc)


def _doc_to_fest_public(doc: dict) -> FestPublic:
    doc.pop("_id", None)
    doc.pop("secret_key", None)
    return FestPublic(**doc)


async def create_fest(col: AsyncIOMotorCollection, data: FestCreate) -> FestPublic:
    secret_key = secrets.token_hex(32)  # 64-char hex string
    fest_doc = {
        **data.model_dump(mode="json"),
        "secret_key": secret_key,
        "created_at": datetime.now(tz=timezone.utc),
    }
    try:
        await col.insert_one(fest_doc)
    except DuplicateKeyError:
        raise FestAlreadyExistsError(data.fest_id)

    return _doc_to_fest_public(fest_doc)


async def get_fest_public(col: AsyncIOMotorCollection, fest_id: str) -> FestPublic:
    doc = await col.find_one({"fest_id": fest_id})
    if not doc:
        raise FestNotFoundError(fest_id)
    return _doc_to_fest_public(doc)


async def get_fest_db(col: AsyncIOMotorCollection, fest_id: str) -> FestDB:
    """Returns full FestDB including secret_key. Used internally by services only."""
    doc = await col.find_one({"fest_id": fest_id})
    if not doc:
        raise FestNotFoundError(fest_id)
    return _doc_to_fest_db(doc)


async def list_fests(col: AsyncIOMotorCollection) -> list[FestPublic]:
    cursor = col.find({}, {"secret_key": 0})
    return [_doc_to_fest_public(doc) async for doc in cursor]


async def update_fest(
    col: AsyncIOMotorCollection,
    fest_id: str,
    update: FestUpdate,
) -> FestPublic:
    update_data = update.model_dump(mode="json", exclude_none=True)
    if not update_data:
        doc = await col.find_one({"fest_id": fest_id})
        if not doc:
            raise FestNotFoundError(fest_id)
        return _doc_to_fest_public(doc)

    result = await col.find_one_and_update(
        {"fest_id": fest_id},
        {"$set": update_data},
        return_document=True,
    )
    if not result:
        raise FestNotFoundError(fest_id)
    return _doc_to_fest_public(result)


async def delete_fest(col: AsyncIOMotorCollection, fest_id: str) -> None:
    """Delete a fest by ID. Raises FestNotFoundError if fest doesn't exist."""
    result = await col.delete_one({"fest_id": fest_id})
    if result.deleted_count == 0:
        raise FestNotFoundError(fest_id)


async def has_transactions(transactions_col: AsyncIOMotorCollection, fest_id: str) -> bool:
    """Check if a fest has completed transactions (successful payments)."""
    count = await transactions_col.count_documents(
        {
            "fest_id": fest_id,
            "status": {"$in": ["SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"]},
        }
    )
    return count > 0


async def get_fest_secret(col: AsyncIOMotorCollection, fest_id: str) -> str:
    doc = await col.find_one({"fest_id": fest_id}, {"secret_key": 1})
    if not doc:
        raise FestNotFoundError(fest_id)
    return doc["secret_key"]
