from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.exceptions import InvalidHashError
from app.core.redis_client import confirm_seat, get_redis, release_seat
from app.core.security import verify_easebuzz_response_hash
from app.models.transaction import TransactionDB, TxStatus
from app.services import fest_service
from app.services.webhook_dispatch import dispatch_to_fest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])


@router.post("/easebuzz")
async def easebuzz_s2s_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    EaseBuzz Server-to-Server (S2S) webhook endpoint.

    EaseBuzz POSTs payment result data as application/x-www-form-urlencoded.
    We MUST return HTTP 200 quickly — webhook dispatch to the fest is fired
    as a FastAPI BackgroundTask so we don't keep EaseBuzz waiting.

    IDEMPOTENCY: Uses find_one_and_update with {status: PENDING} condition.
    If EaseBuzz retries a callback we already processed, the update matches
    zero documents and we return 200 silently — no duplicate dispatch fires.

    SEAT MANAGEMENT:
      SUCCESS → confirm_seat (permanently consumes the Redis reservation)
      FAILURE → release_seat (INCR Redis counter if reservation still alive)

    FAILURE NOTIFICATION: The fest callback is fired for both SUCCESS and
    FAILED outcomes so the fest backend always knows the payment result.
    """
    settings = get_settings()
    redis = get_redis(request)

    form_data = await request.form()
    data = dict(form_data)

    logger.info("Received EaseBuzz S2S webhook", extra={"txnid": data.get("txnid")})

    # ----- Hash verification (tamper protection) -----
    import hashlib as _hl
    _hash_str = (
        f"{settings.EASEBUZZ_SALT}|{data.get('status', '')}||"
        f"{data.get('udf5', '')}|{data.get('udf4', '')}|"
        f"{data.get('udf3', '')}|{data.get('udf2', '')}|"
        f"{data.get('udf1', '')}|{data.get('email', '')}|"
        f"{data.get('firstname', '')}|{data.get('productinfo', '')}|"
        f"{data.get('amount', '')}|{data.get('txnid', '')}|"
        f"{data.get('key', '')}"
    )
    logger.warning("WEBHOOK_DEBUG received=%s", data)
    logger.warning("WEBHOOK_DEBUG hash_input=%s", repr(_hash_str))
    logger.warning("WEBHOOK_DEBUG computed=%s", _hl.sha512(_hash_str.encode()).hexdigest())
    logger.warning("WEBHOOK_DEBUG received_hash=%s", data.get("hash", ""))

    if not verify_easebuzz_response_hash(data, settings.EASEBUZZ_SALT):
        logger.warning("EaseBuzz webhook hash mismatch", extra={"txnid": data.get("txnid")})
        raise InvalidHashError()

    internal_tx_id = data.get("txnid", "")
    easebuzz_txid = data.get("easepayid", "")
    payment_status = data.get("status", "").lower()

    tx_col = get_transactions_col(db)
    now = datetime.now(tz=timezone.utc)

    if payment_status == "success":
        # Atomic: only updates if the transaction is still PENDING.
        # Returns None if already processed (idempotency guard).
        updated_doc = await tx_col.find_one_and_update(
            {"internal_tx_id": internal_tx_id, "status": TxStatus.PENDING.value},
            {"$set": {
                "status": TxStatus.SUCCESS.value,
                "easebuzz_txid": easebuzz_txid,
                "completed_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )

        if updated_doc is None:
            # Already processed — EaseBuzz retry. Return 200 silently.
            logger.info(
                "Duplicate SUCCESS webhook ignored (already processed)",
                extra={"internal_tx_id": internal_tx_id},
            )
            return {"status": "ok"}

        updated_doc.pop("_id", None)
        transaction = TransactionDB(**updated_doc)

        # Permanently consume the Redis seat reservation
        await confirm_seat(redis, internal_tx_id)

        fests_col = get_fests_col(db)
        fest = await fest_service.get_fest_db(fests_col, transaction.fest_id)

        background_tasks.add_task(dispatch_to_fest, transaction, fest, db)
        logger.info(
            "Transaction marked SUCCESS, dispatch queued",
            extra={"internal_tx_id": internal_tx_id},
        )

    else:
        # Atomic: only updates if still PENDING.
        updated_doc = await tx_col.find_one_and_update(
            {"internal_tx_id": internal_tx_id, "status": TxStatus.PENDING.value},
            {"$set": {
                "status": TxStatus.FAILED.value,
                "completed_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )

        if updated_doc is None:
            logger.info(
                "Duplicate FAILURE webhook ignored (already processed)",
                extra={"internal_tx_id": internal_tx_id},
            )
            return {"status": "ok"}

        updated_doc.pop("_id", None)
        transaction = TransactionDB(**updated_doc)

        # Release the seat back — only if the 15-min reservation hasn't already expired
        await release_seat(redis, transaction.fest_id, internal_tx_id)

        fests_col = get_fests_col(db)
        fest = await fest_service.get_fest_db(fests_col, transaction.fest_id)

        # Notify the fest backend of the failure — they need to know too
        background_tasks.add_task(dispatch_to_fest, transaction, fest, db)
        logger.info(
            "Transaction marked FAILED, failure dispatch queued",
            extra={"internal_tx_id": internal_tx_id, "easebuzz_status": payment_status},
        )

    return {"status": "ok"}
