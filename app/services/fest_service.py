from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import FestAlreadyExistsError, FestNotFoundError
from app.models.fest import FestCreate, FestDB, FestEvent, FestPublic, FestUpdate


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


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------

async def add_event(
    col: AsyncIOMotorCollection,
    fest_id: str,
    name: str,
    description: Optional[str] = None,
) -> FestPublic:
    """
    Add a new event to a fest's events list.
    Generates a UUID for the event_id server-side.
    """
    event = FestEvent(
        event_id=str(uuid.uuid4()),
        name=name,
        description=description,
    )
    result = await col.find_one_and_update(
        {"fest_id": fest_id},
        {"$push": {"events": event.model_dump(mode="json")}},
        return_document=True,
    )
    if not result:
        raise FestNotFoundError(fest_id)
    return _doc_to_fest_public(result)


async def update_event(
    col: AsyncIOMotorCollection,
    fest_id: str,
    event_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> FestPublic:
    """
    Update an existing event's name and/or description.
    Uses positional $ operator to target the matching array element.
    """
    set_fields: dict = {}
    if name is not None:
        set_fields["events.$.name"] = name
    if description is not None:
        set_fields["events.$.description"] = description

    if not set_fields:
        return await get_fest_public(col, fest_id)

    result = await col.find_one_and_update(
        {"fest_id": fest_id, "events.event_id": event_id},
        {"$set": set_fields},
        return_document=True,
    )
    if not result:
        # Could be fest not found or event_id not found
        fest_doc = await col.find_one({"fest_id": fest_id})
        if not fest_doc:
            raise FestNotFoundError(fest_id)
        raise HTTPException(status_code=404, detail=f"Event {event_id!r} not found in fest {fest_id!r}")
    return _doc_to_fest_public(result)


async def delete_event(
    col: AsyncIOMotorCollection,
    transactions_col: AsyncIOMotorCollection,
    fest_id: str,
    event_id: str,
) -> FestPublic:
    """
    Remove an event from a fest's events list.
    Blocked if any paid transaction references this event_id — cannot delete
    events that are part of the historical record.
    """
    # Guard: check for paid transactions that selected this event
    paid_count = await transactions_col.count_documents(
        {
            "fest_id": fest_id,
            "selected_events": event_id,
            "status": {"$in": ["SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"]},
        }
    )
    if paid_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete event {event_id!r}: {paid_count} paid transaction(s) "
                f"reference this event. Delete the event from the fest config is blocked "
                f"to preserve the audit trail."
            ),
        )

    result = await col.find_one_and_update(
        {"fest_id": fest_id},
        {"$pull": {"events": {"event_id": event_id}}},
        return_document=True,
    )
    if not result:
        raise FestNotFoundError(fest_id)
    return _doc_to_fest_public(result)
