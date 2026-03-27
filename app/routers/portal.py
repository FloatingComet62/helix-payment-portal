from __future__ import annotations

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db, get_fests_col
from app.core.exceptions import FestNotFoundError
from app.models.fest import FestListing

router = APIRouter(prefix="/portal", tags=["Portal"])


@router.get("/fests", response_model=list[FestListing])
async def list_fests_public(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Public endpoint — no authentication required.

    Returns a minimal listing of all registered fests for the portal's
    fest-selection UI. Only exposes public fest metadata and pricing config.
    callback_url, authorized_url, and secret_key are never included.
    """
    col = get_fests_col(db)
    cursor = col.find(
        {},
        {
            "_id": 0,
            "fest_id": 1,
            "legal_name": 1,
            "event_dates": 1,
            "short_description": 1,
            "long_description": 1,
            "pricing": 1,
        },
    )
    return [FestListing(**doc) async for doc in cursor]


@router.get("/fests/{fest_id}", response_model=FestListing)
async def get_fest_public(
    fest_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Public endpoint — no authentication required.

    Returns public info for a single fest. Used by the portal frontend
    to display pricing details on the booking confirmation screen.
    """
    col = get_fests_col(db)
    doc = await col.find_one(
        {"fest_id": fest_id},
        {
            "_id": 0,
            "fest_id": 1,
            "legal_name": 1,
            "event_dates": 1,
            "short_description": 1,
            "long_description": 1,
            "pricing": 1,
        },
    )
    if not doc:
        raise FestNotFoundError(fest_id)
    return FestListing(**doc)
