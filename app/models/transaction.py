from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class TxStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOTIFIED_SUCCESS = "NOTIFIED_SUCCESS"
    NOTIFIED_FAILURE = "NOTIFIED_FAILURE"


class TxSource(str, Enum):
    FEST_REDIRECT = "fest_redirect"   # User redirected from an external fest frontend
    DIRECT_PORTAL = "direct_portal"   # User visited the portal independently


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

    For fest-redirect flow: provide origin_user_id + source="fest_redirect".
    For direct portal flow: omit origin_user_id (backend generates "portal_<uuid>")
                            and omit source (defaults to "direct_portal").
    """
    fest_id: str
    origin_user_id: Optional[str] = None   # Fest's own user ID; auto-generated if absent
    source: TxSource = TxSource.DIRECT_PORTAL
    user_name: str
    user_state: str
    user_district: str
    booking: BookingDetail
    coupon_code: Optional[str] = None


class TransactionDB(BaseModel):
    """
    Canonical transaction document stored in MongoDB.

    Financial fields are stored as strings to preserve Decimal precision.
    The balance sheet aggregation pipeline converts them via $toDecimal before summing.

    Ledger:
        base_amount              = raw booking cost before any discount/GST
        coupon_discount_applied  = discount from coupon (0.00 if no coupon)
        gst_amount               = GST on (base_amount - coupon_discount) — computed BEFORE gateway
        easebuzz_transaction_fee = platform fee on the taxable amount
        final_net_credited       = base_amount - coupon_discount - gst_amount - easebuzz_fee
                                   (what the fest actually receives)
    """
    internal_tx_id: str
    easebuzz_txid: Optional[str] = None
    fest_id: str
    origin_user_id: str        # resolved value (never None in DB)
    source: TxSource           # fest_redirect | direct_portal
    user_name: str
    user_state: str
    user_district: str
    booking: BookingDetail
    base_amount: str
    gst_amount: str
    easebuzz_transaction_fee: str
    coupon_discount_applied: str
    final_net_credited: str
    coupon_code: Optional[str] = None
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
