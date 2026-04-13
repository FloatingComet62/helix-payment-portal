from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.aggregations.balance_sheet import run_balance_sheet
from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.exceptions import AdminAuthError, FestHasTransactionsError
from app.core.redis_client import get_redis, sync_seats_for_fest
from app.models.fest import FestCreate, FestPublic, FestUpdate, EventSelectionConfig
from app.services.refund_service import initiate_refund
from app.services import fest_service
from app.services.payment_service import _debug_log
from app.models.transaction import TransactionDB, TxStatus
from app.services.webhook_dispatch import dispatch_to_fest

router = APIRouter(prefix="/admin", tags=["Admin"])


def _verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    if x_admin_key != get_settings().ADMIN_API_KEY:
        raise AdminAuthError()


AdminAuth = Depends(_verify_admin_key)


def _status_group_to_db_values(status: str | None) -> list[str] | None:
    if not status:
        return None
    normalized = status.lower()
    if normalized == "pending":
        return [TxStatus.PENDING.value]
    if normalized == "failed":
        return [TxStatus.FAILED.value]
    if normalized == "success":
        return [
            TxStatus.SUCCESS.value,
            TxStatus.NOTIFIED_SUCCESS.value,
            TxStatus.NOTIFIED_FAILURE.value,
            TxStatus.REFUNDED.value,
        ]
    return None


def _callback_group_to_db_values(callback_status: str | None) -> list[str] | None:
    if not callback_status:
        return None
    normalized = callback_status.lower()
    if normalized == "sent":
        return [TxStatus.NOTIFIED_SUCCESS.value]
    if normalized == "failed":
        return [TxStatus.NOTIFIED_FAILURE.value]
    if normalized == "pending":
        return [TxStatus.PENDING.value, TxStatus.SUCCESS.value]
    if normalized == "skipped":
        return [TxStatus.FAILED.value, TxStatus.REFUNDED.value]
    return None


def _to_portal_status(db_status: str) -> str:
    if db_status in (TxStatus.PENDING.value,):
        return "pending"
    if db_status in (TxStatus.FAILED.value,):
        return "failed"
    return "success"


def _to_callback_status(db_status: str) -> str:
    if db_status == TxStatus.NOTIFIED_SUCCESS.value:
        return "sent"
    if db_status == TxStatus.NOTIFIED_FAILURE.value:
        return "failed"
    if db_status == TxStatus.SUCCESS.value:
        return "pending"
    if db_status == TxStatus.PENDING.value:
        return "pending"
    return "skipped"


def _to_portal_transaction(doc: dict) -> dict:
    booking = doc.get("booking", {}) or {}

    def _to_amount(value: str | float | int | None) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    db_status = str(doc.get("status", TxStatus.PENDING.value))
    initiated_at = doc.get("initiated_at")
    completed_at = doc.get("completed_at")

    return {
        "merchantOrderId": doc.get("internal_tx_id"),
        "easebuzzTxid": doc.get("easebuzz_txid"),
        "festId": doc.get("fest_id"),
        "eventId": None,
        "teamId": None,
        "teamName": None,
        "userName": doc.get("user_name"),
        "userEmail": doc.get("user_email"),
        "userPhone": doc.get("user_phone"),
        "userState": doc.get("user_state"),
        "userDistrict": doc.get("user_district"),
        "collegeName": None,
        "paymentType": "accommodation",
        "paymentMode": doc.get("payment_mode"),
        "selectedEvents": doc.get("selected_events", []),
        "status": _to_portal_status(db_status),
        "callbackStatus": _to_callback_status(db_status),
        "originalAmount": _to_amount(doc.get("base_amount")),
        "gstAmount": _to_amount(doc.get("gst_amount")),
        "easebuzzFee": _to_amount(doc.get("easebuzz_transaction_fee")),
        "finalAmount": _to_amount(doc.get("final_net_credited")),
        "couponDiscount": _to_amount(doc.get("coupon_discount_applied")),
        "couponCode": doc.get("coupon_code"),
        "createdAt": initiated_at.isoformat() if isinstance(initiated_at, datetime) else None,
        "completedAt": completed_at.isoformat() if isinstance(completed_at, datetime) else None,
        "callbackAttempts": len(doc.get("notification_logs", []) or []),
        "callbackLastError": (doc.get("notification_logs", []) or [{}])[-1].get("error") if (doc.get("notification_logs", []) or []) else None,
        "notificationLogs": doc.get("notification_logs", []),
        "paymentIntentions": {
            "eventRegistration": None,
            "accommodation": {
                "totalAmount": _to_amount(booking.get("accommodation_amount")),
                "days": booking.get("accommodation_days", 0),
            },
            "food": {
                "totalAmount": _to_amount(booking.get("food_amount")),
                "days": booking.get("food_days", 0),
            },
        },
    }


def _safe_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


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


@router.post("/fests/{fest_id}/events", response_model=FestPublic, status_code=201)
async def add_event(
    fest_id: str,
    payload: dict,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Add a new event to a fest. Body: { "name": "...", "description": "..." (optional) }
    Returns the updated fest with all events.
    """
    name = payload.get("name", "").strip()
    if not name:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Event name is required")
    description = payload.get("description", None)
    col = get_fests_col(db)
    return await fest_service.add_event(col, fest_id, name, description)


@router.patch("/fests/{fest_id}/events/{event_id}", response_model=FestPublic)
async def update_event(
    fest_id: str,
    event_id: str,
    payload: dict,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update an event's name and/or description.
    Body: { "name": "..." (optional), "description": "..." (optional) }
    """
    col = get_fests_col(db)
    return await fest_service.update_event(
        col,
        fest_id,
        event_id,
        name=payload.get("name"),
        description=payload.get("description"),
    )


@router.delete("/fests/{fest_id}/events/{event_id}", response_model=FestPublic)
async def delete_event(
    fest_id: str,
    event_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Remove an event from a fest.
    Blocked (HTTP 409) if any paid transactions reference this event_id.
    """
    col = get_fests_col(db)
    tx_col = get_transactions_col(db)
    return await fest_service.delete_event(col, tx_col, fest_id, event_id)


@router.patch("/fests/{fest_id}/event-selection", response_model=FestPublic)
async def update_event_selection_config(
    fest_id: str,
    payload: EventSelectionConfig,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update the event_selection config for a fest (enabled, required, max_selections).
    This is also achievable via PATCH /admin/fests/{fest_id} with event_selection in the body.
    """
    col = get_fests_col(db)
    update = FestUpdate(event_selection=payload)
    return await fest_service.update_fest(col, fest_id, update)


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


@router.get("/transactions")
async def list_transactions(
    fest_id: str | None = Query(None, alias="festId"),
    status: str | None = None,
    callback_status: str | None = Query(None, alias="callbackStatus"),
    event_id: str | None = Query(None, alias="eventId"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # realbackend currently has no event-level transaction field.
    if event_id:
        return {
            "transactions": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
        }

    query: dict = {}
    if fest_id:
        query["fest_id"] = fest_id

    status_values = _status_group_to_db_values(status)
    callback_values = _callback_group_to_db_values(callback_status)

    if status_values and callback_values:
        query["status"] = {"$in": [value for value in status_values if value in callback_values]}
    elif status_values:
        query["status"] = {"$in": status_values}
    elif callback_values:
        query["status"] = {"$in": callback_values}

    tx_col = get_transactions_col(db)
    skip = (page - 1) * page_size

    total = await tx_col.count_documents(query)
    docs = (
        await tx_col.find(query)
        .sort("initiated_at", -1)
        .skip(skip)
        .limit(page_size)
        .to_list(length=page_size)
    )

    return {
        "transactions": [_to_portal_transaction(doc) for doc in docs],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "hasMore": (skip + page_size) < total,
    }


@router.get("/transactions/stats/{fest_id}")
async def get_transaction_stats(
    fest_id: str,
    event_id: str | None = Query(None, alias="eventId"),
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if event_id:
        return {
            "festId": fest_id,
            "totalTransactions": 0,
            "successfulTransactions": 0,
            "failedTransactions": 0,
            "pendingTransactions": 0,
            "totalCollected": 0,
            "accommodationTotal": 0,
            "foodTotal": 0,
            "eventTotal": 0,
        }

    tx_col = get_transactions_col(db)
    query = {"fest_id": fest_id}

    total = await tx_col.count_documents(query)
    pending = await tx_col.count_documents({**query, "status": TxStatus.PENDING.value})
    failed = await tx_col.count_documents({**query, "status": TxStatus.FAILED.value})
    successful = await tx_col.count_documents(
        {
            **query,
            "status": {
                "$in": [
                    TxStatus.SUCCESS.value,
                    TxStatus.NOTIFIED_SUCCESS.value,
                    TxStatus.NOTIFIED_FAILURE.value,
                    TxStatus.REFUNDED.value,
                ]
            },
        }
    )

    docs = await tx_col.find(query).to_list(length=5000)

    def _sum_amount(field: str) -> float:
        total_value = 0.0
        for doc in docs:
            value = doc.get(field)
            try:
                total_value += float(value)
            except (TypeError, ValueError):
                continue
        return total_value

    return {
        "festId": fest_id,
        "totalTransactions": total,
        "successfulTransactions": successful,
        "failedTransactions": failed,
        "pendingTransactions": pending,
        "totalCollected": _sum_amount("final_net_credited"),
        "accommodationTotal": sum(
            _safe_float((doc.get("booking", {}) or {}).get("accommodation_amount", 0)) for doc in docs
        ),
        "foodTotal": sum(
            _safe_float((doc.get("booking", {}) or {}).get("food_amount", 0)) for doc in docs
        ),
        "eventTotal": 0,
    }


@router.get("/transactions/{merchant_order_id}")
async def get_transaction(
    merchant_order_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tx_col = get_transactions_col(db)
    doc = await tx_col.find_one({"internal_tx_id": merchant_order_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _to_portal_transaction(doc)


@router.post("/transactions/{merchant_order_id}/retry-callback")
async def retry_callback(
    merchant_order_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tx_col = get_transactions_col(db)
    doc = await tx_col.find_one({"internal_tx_id": merchant_order_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Transaction not found")

    tx = TransactionDB(**{k: v for k, v in doc.items() if k != "_id"})
    if tx.status not in (TxStatus.SUCCESS, TxStatus.NOTIFIED_FAILURE):
        raise HTTPException(
            status_code=400,
            detail="Can only retry callbacks for successful transactions",
        )

    fests_col = get_fests_col(db)
    fest = await fest_service.get_fest_db(fests_col, tx.fest_id)

    await tx_col.update_one(
        {"internal_tx_id": merchant_order_id},
        {
            "$set": {
                "status": TxStatus.SUCCESS.value,
                "completed_at": datetime.now(tz=timezone.utc),
            }
        },
    )

    latest = await tx_col.find_one({"internal_tx_id": merchant_order_id})
    if not latest:
        raise HTTPException(status_code=404, detail="Transaction not found")

    latest_tx = TransactionDB(**{k: v for k, v in latest.items() if k != "_id"})
    await dispatch_to_fest(latest_tx, fest, db)

    updated = await tx_col.find_one({"internal_tx_id": merchant_order_id})
    if not updated:
        raise HTTPException(status_code=404, detail="Transaction not found")

    normalized = _to_portal_transaction(updated)
    return {
        "message": "Callback retried",
        "callbackStatus": normalized.get("callbackStatus"),
        "callbackAttempts": normalized.get("callbackAttempts", 0),
        "callbackLastError": normalized.get("callbackLastError"),
    }


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


@router.get("/debug/easebuzz")
async def debug_easebuzz_log(_: None = AdminAuth):
    """
    Temporary debug endpoint — returns the last 20 payment initiation attempts
    with full EaseBuzz credentials, hash input, and payment URL.
    Remove once credentials are confirmed working.
    """
    return {"count": len(_debug_log), "entries": list(_debug_log)}


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
