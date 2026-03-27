from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.aggregations.balance_sheet import run_balance_sheet
from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.exceptions import AdminAuthError, FestHasTransactionsError
from app.core.redis_client import get_redis, sync_seats_for_fest
from app.models.fest import FestCreate, FestPublic, FestUpdate
from app.services.refund_service import initiate_refund
from app.services import fest_service

router = APIRouter(prefix="/admin", tags=["Admin"])


def _verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    if x_admin_key != get_settings().ADMIN_API_KEY:
        raise AdminAuthError()


AdminAuth = Depends(_verify_admin_key)


@router.post("/fests", response_model=FestPublic, status_code=201)
async def onboard_fest(
    payload: FestCreate,
    request: Request,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Onboard a new fest. Generates a secret_key server-side (never in the request).
    The secret_key is NOT returned here — use GET /admin/fests/{fest_id}/secret.

    If total_accommodation_seats is provided, the Redis seat counter is initialized
    immediately so payments can start without waiting for a pod restart.
    """
    col = get_fests_col(db)
    fest = await fest_service.create_fest(col, payload)

    if payload.total_accommodation_seats is not None:
        redis = get_redis(request)
        tx_col = get_transactions_col(db)
        await sync_seats_for_fest(redis, payload.fest_id, payload.total_accommodation_seats, tx_col)

    return fest


@router.get("/fests", response_model=list[FestPublic])
async def list_fests(
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    col = get_fests_col(db)
    return await fest_service.list_fests(col)


@router.get("/fests/{fest_id}", response_model=FestPublic)
async def get_fest(
    fest_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    col = get_fests_col(db)
    return await fest_service.get_fest_public(col, fest_id)


@router.patch("/fests/{fest_id}", response_model=FestPublic)
async def update_fest(
    fest_id: str,
    payload: FestUpdate,
    request: Request,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    col = get_fests_col(db)
    fest = await fest_service.update_fest(col, fest_id, payload)

    # If seat capacity was changed, re-sync the Redis counter immediately.
    # This accounts for any confirmed bookings already in MongoDB.
    if payload.total_accommodation_seats is not None:
        redis = get_redis(request)
        tx_col = get_transactions_col(db)
        await sync_seats_for_fest(redis, fest_id, payload.total_accommodation_seats, tx_col)

    return fest


@router.delete("/fests/{fest_id}", status_code=204)
async def delete_fest(
    fest_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Delete a fest by ID. Removes the fest document from MongoDB.
    Cannot delete if the fest has associated transactions.
    """
    transactions_col = get_transactions_col(db)
    has_txns = await fest_service.has_transactions(transactions_col, fest_id)
    if has_txns:
        raise FestHasTransactionsError(fest_id)
    
    col = get_fests_col(db)
    await fest_service.delete_fest(col, fest_id)
    return None


@router.post("/fests/check-transactions")
async def check_fest_transactions(
    payload: dict,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Check which fests have transactions. Returns a list of fest IDs that have at least one transaction.
    """
    fest_ids = payload.get("fest_ids", [])
    if not fest_ids:
        return {"fests_with_transactions": []}
    
    transactions_col = db["transactions"]
    fests_with_txns = []
    
    for fest_id in fest_ids:
        has_txns = await fest_service.has_transactions(transactions_col, fest_id)
        if has_txns:
            fests_with_txns.append(fest_id)
    
    return {"fests_with_transactions": fests_with_txns}


@router.get("/fests/{fest_id}/secret")
async def reveal_secret(
    fest_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns the fest's HMAC secret_key. Treat this endpoint like a credentials vault —
    restrict access to authorized admin staff only.
    """
    col = get_fests_col(db)
    secret = await fest_service.get_fest_secret(col, fest_id)
    return {"fest_id": fest_id, "secret_key": secret}


@router.get("/fests/{fest_id}/seats")
async def get_seat_availability(
    fest_id: str,
    request: Request,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns the live seat count from Redis for a fest.
    Useful for monitoring during a registration window.
    """
    col = get_fests_col(db)
    fest = await fest_service.get_fest_public(col, fest_id)

    if fest.total_accommodation_seats is None:
        return {"fest_id": fest_id, "total_accommodation_seats": None, "seats_available": "unlimited"}

    redis = get_redis(request)
    from app.core.redis_client import _seat_key
    raw = await redis.get(_seat_key(fest_id))
    available = int(raw) if raw is not None else fest.total_accommodation_seats

    return {
        "fest_id": fest_id,
        "total_accommodation_seats": fest.total_accommodation_seats,
        "seats_available": available,
        "seats_booked": fest.total_accommodation_seats - available,
    }


@router.get("/balance-sheet")
async def balance_sheet(
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Runs the MongoDB aggregation pipeline and returns a per-fest financial summary
    for the finance office. Only includes SUCCESS / NOTIFIED_* transactions.
    """
    return await run_balance_sheet(db)


@router.post("/refunds/{internal_tx_id}")
async def refund_transaction(
    internal_tx_id: str,
    request: Request,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Admin-only. Initiates a full refund for a confirmed payment via EaseBuzz.

    Only works on transactions in SUCCESS, NOTIFIED_SUCCESS, or NOTIFIED_FAILURE state.
    On success: marks transaction REFUNDED and releases the accommodation seat back.

    Use this only as a last resort — the system is designed to prevent the need
    for refunds through atomic seat management.
    """
    redis = get_redis(request)
    settings = get_settings()
    return await initiate_refund(internal_tx_id, db, redis, settings)
