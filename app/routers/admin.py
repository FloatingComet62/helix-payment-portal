from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.aggregations.balance_sheet import run_balance_sheet
from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col
from app.core.exceptions import AdminAuthError, FestHasTransactionsError
from app.models.fest import FestCreate, FestPublic, FestUpdate
from app.services import fest_service

router = APIRouter(prefix="/admin", tags=["Admin"])


def _verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    if x_admin_key != get_settings().ADMIN_API_KEY:
        raise AdminAuthError()


AdminAuth = Depends(_verify_admin_key)


@router.post("/fests", response_model=FestPublic, status_code=201)
async def onboard_fest(
    payload: FestCreate,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Onboard a new fest. Generates a secret_key server-side (never in the request).
    The secret_key is NOT returned here — use GET /admin/fests/{fest_id}/secret.
    """
    col = get_fests_col(db)
    return await fest_service.create_fest(col, payload)


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
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    col = get_fests_col(db)
    return await fest_service.update_fest(col, fest_id, payload)


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
