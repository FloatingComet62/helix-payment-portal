from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import get_db, get_fests_col, get_transactions_col

router = APIRouter(prefix="/lhs", tags=["LHS"])

FEST_ID = "HELIX_2026"


def _verify_lhs_key(x_lhs_key: str = Header(..., alias="X-LHS-Key")) -> None:
    if x_lhs_key != get_settings().LHS_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid LHS API key")


LHSAuth = Depends(_verify_lhs_key)


def _f(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


@router.get("")
async def get_lhs_transactions(
    _: None = LHSAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns all transactions for HELIX_2026 with:
    - user_name, user_phone, user_email
    - events: list of event names (resolved from event IDs)
    - amount_paid: base_amount - coupon_discount_applied + gst_amount
    """
    # Build event_id -> name map from the fest document
    fests_col = get_fests_col(db)
    fest_doc = await fests_col.find_one({"fest_id": FEST_ID})
    event_map: dict[str, str] = {}
    if fest_doc:
        for ev in fest_doc.get("events", []):
            event_map[ev["event_id"]] = ev["name"]

    tx_col = get_transactions_col(db)
    docs = await tx_col.find(
        {"fest_id": FEST_ID, "status": "NOTIFIED_SUCCESS"}
    ).to_list(length=10_000)

    results = [
        {
            "uid": doc.get("origin_user_id"),
            "user_name": doc.get("user_name"),
            "user_phone": doc.get("user_phone"),
            "user_email": doc.get("user_email"),
            "city": doc.get("user_district"),
            "events": [event_map[eid] for eid in (doc.get("selected_events") or []) if eid in event_map],
            "amount_paid": (
                _f(doc.get("base_amount"))
                - _f(doc.get("coupon_discount_applied"))
                + _f(doc.get("gst_amount"))
            ),
            "checked_in": doc.get("checked_in", False),
        }
        for doc in docs
    ]

    return {"fest_id": FEST_ID, "count": len(results), "transactions": results}


class CheckInRequest(BaseModel):
    uid: str


@router.post("/checkin")
async def checkin_user(
    body: CheckInRequest,
    _: None = LHSAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Marks a user as checked in by setting checked_in = true on their
    HELIX_2026 transaction document, looked up by origin_user_id.
    """
    tx_col = get_transactions_col(db)
    result = await tx_col.find_one_and_update(
        {"fest_id": FEST_ID, "origin_user_id": body.uid},
        {"$set": {"checked_in": True}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"No transaction found for uid '{body.uid}'")

    return {"uid": body.uid, "checked_in": True}


@router.post("/checkout")
async def checkout_user(
    body: CheckInRequest,
    _: None = LHSAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Reverts a check-in by setting checked_in = false.
    """
    tx_col = get_transactions_col(db)
    result = await tx_col.update_one(
        {"fest_id": FEST_ID, "origin_user_id": body.uid},
        {"$set": {"checked_in": False}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"No transaction found for uid '{body.uid}'")

    return {"uid": body.uid, "checked_in": False}


@router.get("/checkpassword")
async def check_password(x_lhs_key: str = Header(..., alias="X-LHS-Key")):
    """
    Returns whether the provided X-LHS-Key is correct.
    Always responds 200 — never raises 401 — so the frontend can
    use the boolean to gate the UI rather than catching errors.
    """
    valid = x_lhs_key == get_settings().LHS_API_KEY
    return {"valid": valid}
