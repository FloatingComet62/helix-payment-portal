from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from app.core import security
from app.core.database import get_transactions_col
from app.models.fest import FestDB
from app.models.transaction import NotificationLog, TransactionDB, TxStatus

logger = logging.getLogger(__name__)


def build_callback_payload(transaction: TransactionDB) -> dict:
    """
    Construct the signed payload that gets POSTed to the fest's callback_url.
    Contains everything the fest backend needs to fulfill the booking.
    """
    return {
        "internal_tx_id": transaction.internal_tx_id,
        "easebuzz_txid": transaction.easebuzz_txid,
        "fest_id": transaction.fest_id,
        "origin_user_id": transaction.origin_user_id,
        "user_name": transaction.user_name,
        "user_state": transaction.user_state,
        "user_district": transaction.user_district,
        "booking": {
            "accommodation_days": transaction.booking.accommodation_days,
            "food_days": transaction.booking.food_days,
            "accommodation_amount": transaction.booking.accommodation_amount,
            "food_amount": transaction.booking.food_amount,
        },
        "financials": {
            "base_amount": transaction.base_amount,
            "coupon_discount_applied": transaction.coupon_discount_applied,
            "gst_amount": transaction.gst_amount,
            "easebuzz_transaction_fee": transaction.easebuzz_transaction_fee,
            "final_net_credited": transaction.final_net_credited,
        },
        "payment_timestamp": transaction.completed_at.isoformat()
        if transaction.completed_at
        else None,
        "status": transaction.status.value,
    }


async def _append_notification_log(
    db,
    internal_tx_id: str,
    log: NotificationLog,
) -> None:
    tx_col = get_transactions_col(db)
    await tx_col.update_one(
        {"internal_tx_id": internal_tx_id},
        {"$push": {"notification_logs": log.model_dump(mode="json")}},
    )


async def _update_tx_status(
    db,
    internal_tx_id: str,
    status: TxStatus,
) -> None:
    tx_col = get_transactions_col(db)
    await tx_col.update_one(
        {"internal_tx_id": internal_tx_id},
        {"$set": {"status": status.value}},
    )


async def dispatch_to_fest(
    transaction: TransactionDB,
    fest: FestDB,
    db,
) -> None:
    """
    Sign and POST the booking confirmation to the fest's registered callback_url.

    Retry policy: up to 3 attempts with exponential backoff (2s, 4s).
    All attempt results (success or failure) are logged in notification_logs
    on the transaction document so the finance team has a complete audit trail.

    Final status:
        NOTIFIED_SUCCESS — at least one attempt returned HTTP 200
        NOTIFIED_FAILURE — all 3 attempts failed
    """
    payload = build_callback_payload(transaction)
    # Deterministic serialisation: sort_keys ensures the HMAC is reproducible
    payload_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    signature = security.generate_hmac_signature(payload_str, fest.secret_key)

    headers = {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Fest-ID": fest.fest_id,
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        log = NotificationLog(
            attempt=attempt,
            timestamp=datetime.now(tz=timezone.utc),
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    str(fest.callback_url),
                    content=payload_str,
                    headers=headers,
                )
            log.http_status = resp.status_code
            log.response_body = resp.text[:2000]  # cap to avoid bloating the document

            await _append_notification_log(db, transaction.internal_tx_id, log)

            if resp.status_code == 200:
                await _update_tx_status(
                    db, transaction.internal_tx_id, TxStatus.NOTIFIED_SUCCESS
                )
                logger.info(
                    "Fest callback succeeded",
                    extra={
                        "internal_tx_id": transaction.internal_tx_id,
                        "fest_id": fest.fest_id,
                        "attempt": attempt,
                    },
                )
                return

            logger.warning(
                "Fest callback returned non-200",
                extra={
                    "internal_tx_id": transaction.internal_tx_id,
                    "http_status": resp.status_code,
                    "attempt": attempt,
                },
            )

        except Exception as exc:
            log.error = str(exc)
            await _append_notification_log(db, transaction.internal_tx_id, log)
            logger.error(
                "Fest callback attempt raised exception",
                extra={
                    "internal_tx_id": transaction.internal_tx_id,
                    "attempt": attempt,
                    "error": str(exc),
                },
            )

        if attempt < max_attempts:
            await asyncio.sleep(2**attempt)  # 2s after attempt 1, 4s after attempt 2

    # All attempts exhausted
    await _update_tx_status(db, transaction.internal_tx_id, TxStatus.NOTIFIED_FAILURE)
    logger.error(
        "Fest callback failed after all retries",
        extra={
            "internal_tx_id": transaction.internal_tx_id,
            "fest_id": fest.fest_id,
        },
    )
