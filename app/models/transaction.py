from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, EmailStr, field_validator


class TxStatus(str, Enum):
    PENDING          = "PENDING"
    SUCCESS          = "SUCCESS"
    FAILED           = "FAILED"
    NOTIFIED_SUCCESS = "NOTIFIED_SUCCESS"
    NOTIFIED_FAILURE = "NOTIFIED_FAILURE"
    REFUNDED         = "REFUNDED"


class BookingDetail(BaseModel):
    """
    Exact breakdown of what the user booked.
    All monetary amounts stored as Decimal-formatted strings (e.g. "1500.00").
    """
    accommodation_days: int = 0
    food_days: int = 0
    accommodation_amount: str  # subtotal for accommodation
    food_amount: str           # subtotal for food


class NotificationLog(BaseModel):
    """
    Records a single dispatch attempt to the fest's callback_url.
    Stored in the notification_logs array on the transaction document.
    """
    attempt: int
    timestamp: datetime
    http_status: Optional[int] = None
    response_body: Optional[str] = None  # capped at 2000 chars
    error: Optional[str] = None          # populated on network/timeout exceptions


class TransactionCreate(BaseModel):
    """
    Request body for POST /payment/initiate.

    All users arrive via fest redirect — origin_user_id is always
    provided by the fest frontend as part of the redirect parameters.
    """
    fest_id: str
    origin_user_id: str   # Always required — provided by fest frontend in redirect
    user_name: str
    user_email: EmailStr  # Collected from payment form; used in EaseBuzz hash
    user_phone: str
    user_state: str
    user_district: str
    booking: BookingDetail
    selected_events: List[str] = []  # List of event_ids the user selected
    coupon_code: Optional[str] = None

    @field_validator("user_phone", mode="before")
    @classmethod
    def validate_user_phone(cls, value):
        if not isinstance(value, str):
            raise ValueError("user_phone is required")

        normalized = value.strip().replace(" ", "")
        if not normalized.startswith("+91"):
            raise ValueError("user_phone must start with +91")

        local = normalized[3:]
        if len(local) != 10 or not local.isdigit():
            raise ValueError("user_phone must be in +91XXXXXXXXXX format")

        return normalized


class TransactionDB(BaseModel):
    """
    Canonical transaction document stored in MongoDB.

    Financial fields are stored as strings to preserve Decimal precision.
    The balance sheet aggregation pipeline converts them via $toDecimal before summing.

    Ledger:
        base_amount              = raw booking cost before any discount/GST
        coupon_discount_applied  = discount from coupon (0.00 if no coupon)
        gst_amount               = GST on (base_amount - coupon_discount) — computed BEFORE gateway
        easebuzz_transaction_fee = actual platform fee (updated after webhook with real value from EaseBuzz)
        final_net_credited       = actual net credited to fest (updated after webhook)
    """
    internal_tx_id: str
    easebuzz_txid: Optional[str] = None
    fest_id: str
    origin_user_id: str
    user_name: str
    user_email: str           # Real email collected from payment form
    user_phone: str
    user_state: str
    user_district: str
    booking: BookingDetail
    base_amount: str
    gst_amount: str
    easebuzz_transaction_fee: str   # "0.00" at initiation; updated with actual after webhook
    coupon_discount_applied: str
    final_net_credited: str         # "0.00" at initiation; updated with actual after webhook
    coupon_code: Optional[str] = None
    selected_events: List[str] = [] # event_ids chosen by user
    payment_mode: Optional[str] = None  # "UPI", "CC", "DC", "NB" etc — set by webhook handler
    status: TxStatus = TxStatus.PENDING
    initiated_at: datetime
    completed_at: Optional[datetime] = None
    notification_logs: List[NotificationLog] = []


class PaymentInitiateResponse(BaseModel):
    """Returned to the portal frontend so it can redirect to EaseBuzz."""
    internal_tx_id: str
    easebuzz_payment_url: str
    txnid: str
    key: str
    hash: str
    amount: str          # "1770.00" — what EaseBuzz will charge the user
    productinfo: str
    firstname: str
    email: str
    phone: str
    udf1: str            # carries origin_user_id
    udf2: str            # carries fest_id
    udf3: str
    udf4: str
    udf5: str
    surl: str            # EaseBuzz success redirect URL (our callback)
    furl: str            # EaseBuzz failure redirect URL (our callback)
