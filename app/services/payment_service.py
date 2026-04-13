from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorCollection
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.redis_client import acquire_seat
from app.core.security import generate_easebuzz_hash
from app.models.fest import FestDB, PricingConfig
from app.models.transaction import (
    BookingDetail,
    PaymentInitiateResponse,
    TransactionCreate,
    TransactionDB,
    TxStatus,
)
from app.services.coupon_service import validate_coupon

TWOPLACES = Decimal("0.01")

# ---------------------------------------------------------------------------
# Temporary debug log — last 20 payment initiations
# Remove after EaseBuzz credentials are confirmed working
# ---------------------------------------------------------------------------
_debug_log: deque = deque(maxlen=20)


# ---------------------------------------------------------------------------
# Pure financial calculation helpers — no floats, ever
# ---------------------------------------------------------------------------

def calculate_base_amount(pricing: PricingConfig, booking: BookingDetail) -> Decimal:
    """
    Derive base_amount from the fest's PricingConfig and the user's BookingDetail.
    Returns a Decimal quantized to 2 decimal places.
    """
    if pricing.mode == "fixed":
        if booking.accommodation_days > 0 and booking.food_days > 0:
            if pricing.bundled_price is not None:
                return Decimal(str(pricing.bundled_price)).quantize(TWOPLACES, ROUND_HALF_UP)
        elif booking.accommodation_days > 0:
            if pricing.accommodation_price is not None:
                return Decimal(str(pricing.accommodation_price)).quantize(TWOPLACES, ROUND_HALF_UP)
        elif booking.food_days > 0:
            if pricing.food_price is not None:
                return Decimal(str(pricing.food_price)).quantize(TWOPLACES, ROUND_HALF_UP)
        # Backward compatibility for older fests configured with a single fixed_rate.
        return Decimal(str(pricing.fixed_rate or "0.00")).quantize(TWOPLACES, ROUND_HALF_UP)

    if pricing.mode == "per_day":
        if pricing.addon:
            accommodation = (
                Decimal(str(pricing.addon.accommodation_per_day)) * Decimal(str(booking.accommodation_days))
            )
            food = (
                Decimal(str(pricing.addon.food_per_day)) * Decimal(str(booking.food_days))
            )
            return (accommodation + food).quantize(TWOPLACES, ROUND_HALF_UP)
        # Backward compatibility for older fests configured with a single per_day_rate.
        per_day = Decimal(str(pricing.per_day_rate or "0.00"))
        days = Decimal(str(pricing.num_days or 1))
        return (per_day * days).quantize(TWOPLACES, ROUND_HALF_UP)

    if pricing.mode == "bundled":
        addon = pricing.addon
        accommodation = (
            Decimal(str(addon.accommodation_per_day)) * Decimal(str(booking.accommodation_days))
        )
        food = (
            Decimal(str(addon.food_per_day)) * Decimal(str(booking.food_days))
        )
        subtotal = accommodation + food
        if booking.accommodation_days > 0 and booking.food_days > 0:
            discount_factor = (
                Decimal("100") - Decimal(str(addon.bundled_discount_percent))
            ) / Decimal("100")
            subtotal = subtotal * discount_factor
        return subtotal.quantize(TWOPLACES, ROUND_HALF_UP)

    raise ValueError(f"Unknown pricing mode: {pricing.mode!r}")


def calculate_gst(taxable_amount: Decimal, gst_percent: Decimal) -> Decimal:
    return (
        taxable_amount * gst_percent / Decimal("100")
    ).quantize(TWOPLACES, ROUND_HALF_UP)


def _amount_str(d: Decimal) -> str:
    return f"{d:.2f}"


# ---------------------------------------------------------------------------
# Booking detail subtotal builder
# ---------------------------------------------------------------------------

def _build_booking_with_amounts(
    pricing: PricingConfig,
    booking: BookingDetail,
) -> BookingDetail:
    if pricing.mode == "per_day" and pricing.addon:
        addon = pricing.addon
        acc_total = (
            Decimal(str(addon.accommodation_per_day)) * Decimal(str(booking.accommodation_days))
        ).quantize(TWOPLACES, ROUND_HALF_UP)
        food_total = (
            Decimal(str(addon.food_per_day)) * Decimal(str(booking.food_days))
        ).quantize(TWOPLACES, ROUND_HALF_UP)
        return BookingDetail(
            accommodation_days=booking.accommodation_days,
            food_days=booking.food_days,
            accommodation_amount=_amount_str(acc_total),
            food_amount=_amount_str(food_total),
        )

    if pricing.mode ==   "fixed":
        total = calculate_base_amount(pricing, booking)
        if booking.food_days > 0 and booking.accommodation_days == 0:
            return BookingDetail(
                accommodation_days=booking.accommodation_days,
                food_days=booking.food_days,
                accommodation_amount="0.00",
                food_amount=_amount_str(total),
            )
        return BookingDetail(
            accommodation_days=booking.accommodation_days,
            food_days=booking.food_days,
            accommodation_amount=_amount_str(total),
            food_amount="0.00",
        )

    if pricing.mode == "bundled" and pricing.addon:
        addon = pricing.addon
        acc_total = (
            Decimal(str(addon.accommodation_per_day)) * Decimal(str(booking.accommodation_days))
        ).quantize(TWOPLACES, ROUND_HALF_UP)
        food_total = (
            Decimal(str(addon.food_per_day)) * Decimal(str(booking.food_days))
        ).quantize(TWOPLACES, ROUND_HALF_UP)
        return BookingDetail(
            accommodation_days=booking.accommodation_days,
            food_days=booking.food_days,
            accommodation_amount=_amount_str(acc_total),
            food_amount=_amount_str(food_total),
        )

    base = calculate_base_amount(pricing, booking)
    return BookingDetail(
        accommodation_days=booking.accommodation_days,
        food_days=booking.food_days,
        accommodation_amount=_amount_str(base),
        food_amount="0.00",
    )


# ---------------------------------------------------------------------------
# Event selection validation
# ---------------------------------------------------------------------------

def validate_event_selections(fest: FestDB, selected_events: list[str]) -> None:
    """
    Validates the user's event selections against the fest's event_selection config.
    Raises HTTP 422 on any violation.
    """
    cfg = fest.event_selection
    if not cfg.enabled:
        return  # Event selection not enabled for this fest — ignore any selections

    valid_ids = {e.event_id for e in fest.events}

    # Check for unknown event IDs
    for eid in selected_events:
        if eid not in valid_ids:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown event_id: {eid!r}. Valid IDs: {sorted(valid_ids)}",
            )

    # Enforce required
    if cfg.required and not selected_events:
        raise HTTPException(
            status_code=422,
            detail="Event selection is required for this fest. Please select at least one event.",
        )

    # Enforce max_selections
    if len(selected_events) > cfg.max_selections:
        raise HTTPException(
            status_code=422,
            detail=f"You may select at most {cfg.max_selections} event(s). Got {len(selected_events)}.",
        )


# ---------------------------------------------------------------------------
# Main service function
# ---------------------------------------------------------------------------

async def initiate_payment(
    tx_col: AsyncIOMotorCollection,
    fest: FestDB,
    request: TransactionCreate,
    redis: Redis,
) -> PaymentInitiateResponse:
    """
    1. Validate event selections against fest config.
    2. Check seat availability atomically in Redis (if fest has a seat cap).
    3. Calculate all financial figures using Decimal arithmetic.
    4. Create a PENDING transaction in MongoDB.
    5. Generate the EaseBuzz hash using the real user email.
    6. Return the payload the frontend needs to redirect to EaseBuzz.

    NOTE: easebuzz_transaction_fee and final_net_credited are stored as "0.00"
    at initiation because the actual fee depends on the payment mode (UPI, CC, DC, NB)
    which is only known after EaseBuzz confirms the payment. The webhook handler
    updates these fields with real values from EaseBuzz's net_amount_debit field.

    Seat reservation is created BEFORE the MongoDB insert so that if the
    insert fails, the reservation TTL will auto-expire and release the seat.
    """
    settings = get_settings()
    pricing = fest.pricing

    # ── Validate event selections ──────────────────────────────────────────
    validate_event_selections(fest, request.selected_events)

    booking_with_amounts = _build_booking_with_amounts(pricing, request.booking)
    base_amount = calculate_base_amount(pricing, request.booking)

    # ── Financial calculations ──────────────────────────────────────────────
    coupon_discount = Decimal("0.00")
    if request.coupon_code:
        coupon_discount = await validate_coupon(
            request.coupon_code,
            base_amount,
            settings.EASEBUZZ_KEY,
        )
        coupon_discount = min(coupon_discount, base_amount).quantize(TWOPLACES, ROUND_HALF_UP)

    taxable_amount = (base_amount - coupon_discount).quantize(TWOPLACES, ROUND_HALF_UP)
    gst_percent = Decimal(str(pricing.gst_percent))
    gst_amount = calculate_gst(taxable_amount, gst_percent)
    charge_amount = (taxable_amount + gst_amount).quantize(TWOPLACES, ROUND_HALF_UP)
    # Fee and net credited are unknown at initiation — depends on payment mode (UPI/CC/DC/NB).
    # Stored as "0.00" and updated by the webhook handler with actual values from EaseBuzz.

    internal_tx_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)

    # ── Seat check (atomic Redis DECR) ─────────────────────────────────────
    # Must happen BEFORE MongoDB insert. Raises SeatUnavailableError (409) if full.
    # Reservation has a 15-min TTL — auto-released if user never completes payment.
    if fest.total_accommodation_seats is not None and request.booking.accommodation_days > 0:
        await acquire_seat(redis, fest.fest_id, internal_tx_id)

    # ── Persist PENDING transaction ────────────────────────────────────────
    transaction = TransactionDB(
        internal_tx_id=internal_tx_id,
        fest_id=request.fest_id,
        origin_user_id=request.origin_user_id,
        user_name=request.user_name,
        user_email=str(request.user_email),
        user_phone=request.user_phone,
        user_state=request.user_state,
        user_district=request.user_district,
        booking=booking_with_amounts,
        base_amount=_amount_str(base_amount),
        gst_amount=_amount_str(gst_amount),
        easebuzz_transaction_fee="0.00",   # Updated after payment with actual value
        coupon_discount_applied=_amount_str(coupon_discount),
        final_net_credited="0.00",          # Updated after payment with actual value
        coupon_code=request.coupon_code,
        selected_events=request.selected_events,
        status=TxStatus.PENDING,
        initiated_at=now,
    )
    await tx_col.insert_one(transaction.model_dump(mode="json"))

    # ── Build EaseBuzz payload ─────────────────────────────────────────────
    amount_str = _amount_str(charge_amount)
    productinfo = f"{fest.fest_id.replace('_', ' ')} booking"
    user_email_str = str(request.user_email)

    eb_hash = generate_easebuzz_hash(
        key=settings.EASEBUZZ_KEY,
        txnid=internal_tx_id,
        amount=amount_str,
        productinfo=productinfo,
        firstname=request.user_name,
        email=user_email_str,
        salt=settings.EASEBUZZ_SALT,
        udf1=request.origin_user_id,
        udf2=request.fest_id,
    )

    _debug_log.append({
        "ts": now.isoformat(),
        "internal_tx_id": internal_tx_id,
        "easebuzz_key": settings.EASEBUZZ_KEY,
        "easebuzz_salt": settings.EASEBUZZ_SALT,
        "easebuzz_env": settings.EASEBUZZ_ENV,
        "payment_url": settings.easebuzz_payment_url,
        "hash_input": (
            f"{settings.EASEBUZZ_KEY}|{internal_tx_id}|{amount_str}|{productinfo}"
            f"|{request.user_name}|{user_email_str}"
            f"|{request.origin_user_id}|{request.fest_id}|||||||||{settings.EASEBUZZ_SALT}"
        ),
        "hash": eb_hash,
        "amount": amount_str,
        "productinfo": productinfo,
    })

    # EaseBuzz expects a 10-digit mobile number without +91 prefix.
    gateway_phone = request.user_phone[-10:]

    return PaymentInitiateResponse(
        internal_tx_id=internal_tx_id,
        easebuzz_payment_url=settings.easebuzz_payment_url,
        txnid=internal_tx_id,
        key=settings.EASEBUZZ_KEY,
        hash=eb_hash,
        amount=amount_str,
        productinfo=productinfo,
        firstname=request.user_name,
        email=user_email_str,
        phone=gateway_phone,
        udf1=request.origin_user_id,
        udf2=request.fest_id,
        udf3="",
        udf4="",
        udf5="",
        surl=settings.PORTAL_CALLBACK_URL,
        furl=settings.PORTAL_CALLBACK_URL,
    )
