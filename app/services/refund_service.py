from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.database import get_fests_col, get_transactions_col
from app.core.exceptions import EaseBuzzRefundError, RefundNotAllowedError, TransactionNotFoundError
from app.core.redis_client import release_confirmed_seat
from app.models.transaction import TransactionDB, TxStatus

logger = logging.getLogger(__name__)

# Statuses that represent a confirmed successful payment — only these can be refunded
_REFUNDABLE_STATUSES = {
    TxStatus.SUCCESS,
    TxStatus.NOTIFIED_SUCCESS,
    TxStatus.NOTIFIED_FAILURE,
}


def _build_refund_hash(key: str, txnid: str, amount: str, salt: str) -> str:
    """
    EaseBuzz refund hash formula:
        sha512(key|txnid|amount|salt)
    """
    raw = f"{key}|{txnid}|{amount}|{salt}"
    return hashlib.sha512(raw.encode("utf-8")).hexdigest()


async def initiate_refund(
    internal_tx_id: str,
    db: AsyncIOMotorDatabase,
    redis: Redis,
    settings: Settings,
) -> dict:
    """
    Admin-only. Initiates a full refund for a completed transaction via the
    EaseBuzz refund API.

    Steps:
      1. Fetch the transaction and validate it is in a refundable state.
      2. Call EaseBuzz refund API.
      3. On success: mark transaction REFUNDED in MongoDB + release seat in Redis.

    Raises:
      TransactionNotFoundError   — transaction does not exist
      RefundNotAllowedError      — transaction not in a refundable state
      EaseBuzzRefundError        — EaseBuzz API call failed
    """
    tx_col = get_transactions_col(db)
    doc = await tx_col.find_one({"internal_tx_id": internal_tx_id})
    if not doc:
        raise TransactionNotFoundError(internal_tx_id)

    doc.pop("_id", None)
    transaction = TransactionDB(**doc)

    if transaction.status not in _REFUNDABLE_STATUSES:
        raise RefundNotAllowedError(internal_tx_id, transaction.status.value)

    if not transaction.easebuzz_txid:
        raise RefundNotAllowedError(internal_tx_id, "missing easebuzz_txid")

    # charge_amount = taxable + gst = what EaseBuzz charged the user
    # We refund the full charge_amount (base - coupon + gst)
    from decimal import Decimal, ROUND_HALF_UP
    TWOPLACES = Decimal("0.01")
    base = Decimal(transaction.base_amount)
    coupon = Decimal(transaction.coupon_discount_applied)
    gst = Decimal(transaction.gst_amount)
    charge_amount = (base - coupon + gst).quantize(TWOPLACES, ROUND_HALF_UP)
    refund_amount_str = f"{charge_amount:.2f}"

    refund_hash = _build_refund_hash(
        key=settings.EASEBUZZ_KEY,
        txnid=transaction.easebuzz_txid,
        amount=refund_amount_str,
        salt=settings.EASEBUZZ_SALT,
    )

    payload = {
        "key": settings.EASEBUZZ_KEY,
        "txnid": transaction.easebuzz_txid,
        "amount": refund_amount_str,
        "hash": refund_hash,
    }

    logger.info(
        "Initiating EaseBuzz refund",
        extra={"internal_tx_id": internal_tx_id, "easebuzz_txid": transaction.easebuzz_txid, "amount": refund_amount_str},
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(settings.easebuzz_refund_url, data=payload)
    except Exception as exc:
        raise EaseBuzzRefundError(str(exc))

    if resp.status_code != 200:
        raise EaseBuzzRefundError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    body = resp.json()
    # EaseBuzz returns status=1 for successful refund initiation
    if body.get("status") != 1:
        raise EaseBuzzRefundError(body.get("msg", resp.text[:500]))

    # Mark transaction as REFUNDED
    await tx_col.update_one(
        {"internal_tx_id": internal_tx_id},
        {"$set": {
            "status": TxStatus.REFUNDED.value,
            "completed_at": datetime.now(tz=timezone.utc),
        }},
    )

    # Release a previously confirmed seat back into Redis
    if transaction.booking.accommodation_days > 0:
        await release_confirmed_seat(redis, transaction.fest_id, internal_tx_id)

    logger.info(
        "Refund successful",
        extra={"internal_tx_id": internal_tx_id, "refund_amount": refund_amount_str},
    )

    return {
        "status": "refunded",
        "internal_tx_id": internal_tx_id,
        "easebuzz_txid": transaction.easebuzz_txid,
        "refund_amount": refund_amount_str,
    }
