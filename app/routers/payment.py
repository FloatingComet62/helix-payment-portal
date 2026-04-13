from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.redis_client import get_redis
from app.models.transaction import PaymentInitiateResponse, TransactionCreate
from app.services import fest_service, payment_service

router = APIRouter(prefix="/payment", tags=["Payment"])


@router.post("/initiate", response_model=PaymentInitiateResponse, status_code=201)
async def initiate_payment(
    payload: TransactionCreate,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Entry point for the Fest portal frontend.

    1. Checks seat availability atomically in Redis (if fest has a cap).
    2. Calculates base, GST, fee, discount, and net amounts using Decimal arithmetic.
    3. Creates a PENDING transaction document in MongoDB.
    4. Returns the EaseBuzz payment URL + all required form fields (including hash).

    The frontend must POST these fields to the `easebuzz_payment_url` returned,
    which redirects the user to the EaseBuzz hosted payment page.
    """
    fests_col = get_fests_col(db)
    tx_col = get_transactions_col(db)
    redis = get_redis(request)

    fest = await fest_service.get_fest_db(fests_col, payload.fest_id)
    return await payment_service.initiate_payment(tx_col, fest, payload, redis)

