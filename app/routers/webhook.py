from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.exceptions import InvalidHashError, TransactionNotFoundError
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

    Security: All incoming payloads are hash-verified before any DB write.
    """
    settings = get_settings()

    # EaseBuzz sends form-encoded data
    form_data = await request.form()
    data = dict(form_data)

    logger.info("Received EaseBuzz S2S webhook", extra={"txnid": data.get("txnid")})

    # ----- Hash verification (tamper protection) -----
    if not verify_easebuzz_response_hash(data, settings.EASEBUZZ_SALT):
        logger.warning(
            "EaseBuzz webhook hash mismatch",
            extra={"txnid": data.get("txnid")},
        )
        raise InvalidHashError()

    # txnid in EaseBuzz response = our internal_tx_id (we set it during initiation)
    internal_tx_id = data.get("txnid", "")
    easebuzz_txid = data.get("easepayid", "")
    payment_status = data.get("status", "").lower()

    tx_col = get_transactions_col(db)
    doc = await tx_col.find_one({"internal_tx_id": internal_tx_id})
    if not doc:
        raise TransactionNotFoundError(internal_tx_id)

    now = datetime.now(tz=timezone.utc)

    if payment_status == "success":
        await tx_col.update_one(
            {"internal_tx_id": internal_tx_id},
            {
                "$set": {
                    "status": TxStatus.SUCCESS.value,
                    "easebuzz_txid": easebuzz_txid,
                    "completed_at": now,
                }
            },
        )

        # Reload updated document for dispatch
        doc = await tx_col.find_one({"internal_tx_id": internal_tx_id})
        doc.pop("_id", None)
        transaction = TransactionDB(**doc)

        fests_col = get_fests_col(db)
        fest = await fest_service.get_fest_db(fests_col, transaction.fest_id)

        # Fire dispatch asynchronously — do NOT await here (keeps 200 response fast)
        background_tasks.add_task(dispatch_to_fest, transaction, fest, db)

        logger.info(
            "Transaction marked SUCCESS, dispatch queued",
            extra={"internal_tx_id": internal_tx_id},
        )

    else:
        await tx_col.update_one(
            {"internal_tx_id": internal_tx_id},
            {
                "$set": {
                    "status": TxStatus.FAILED.value,
                    "completed_at": now,
                }
            },
        )
        logger.info(
            "Transaction marked FAILED",
            extra={"internal_tx_id": internal_tx_id, "easebuzz_status": payment_status},
        )

    # EaseBuzz requires a fast 200 with a plain body
    return {"status": "ok"}
