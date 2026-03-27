from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorCollection

from app.core.config import get_settings
from app.core.security import generate_easebuzz_hash
from app.models.fest import FestDB, PricingConfig
from app.models.transaction import (
    BookingDetail,
    TransactionCreate,
    TransactionDB,
    TxSource,
    TxStatus,
    PaymentInitiateResponse,
)
from app.services.coupon_service import validate_coupon

TWOPLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# Pure financial calculation helpers — no floats, ever
# ---------------------------------------------------------------------------

def calculate_base_amount(pricing: PricingConfig, booking: BookingDetail) -> Decimal:
    """
    Derive base_amount from the fest's PricingConfig and the user's BookingDetail.
    Returns a Decimal quantized to 2 decimal places.
    """
    if pricing.mode == "fixed":
        # New fixed behavior: option-specific prices.
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
        # New per-day behavior: separate accommodation and food/day rates.
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

        # Apply bundle discount only when both accommodation and food are booked
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


def calculate_easebuzz_fee(amount: Decimal, fee_percent: Decimal) -> Decimal:
    return (
        amount * fee_percent / Decimal("100")
    ).quantize(TWOPLACES, ROUND_HALF_UP)


def _amount_str(d: Decimal) -> str:
    """Format Decimal as a 2-decimal-place string for storage and EaseBuzz."""
    return f"{d:.2f}"


# ---------------------------------------------------------------------------
# Booking detail subtotal builder
# ---------------------------------------------------------------------------

def _build_booking_with_amounts(
    pricing: PricingConfig,
    booking: BookingDetail,
) -> BookingDetail:
    """
    Attach computed accommodation_amount and food_amount strings to BookingDetail.
    """
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

    if pricing.mode == "fixed":
        total = calculate_base_amount(pricing, booking)
        if booking.accommodation_days > 0 and booking.food_days > 0:
            return BookingDetail(
                accommodation_days=booking.accommodation_days,
                food_days=booking.food_days,
                accommodation_amount=_amount_str(total),
                food_amount="0.00",
            )
        if booking.accommodation_days > 0:
            return BookingDetail(
                accommodation_days=booking.accommodation_days,
                food_days=booking.food_days,
                accommodation_amount=_amount_str(total),
                food_amount="0.00",
            )
        return BookingDetail(
            accommodation_days=booking.accommodation_days,
            food_days=booking.food_days,
            accommodation_amount="0.00",
            food_amount=_amount_str(total),
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
# Main service function
# ---------------------------------------------------------------------------

async def initiate_payment(
    tx_col: AsyncIOMotorCollection,
    fest: FestDB,
    request: TransactionCreate,
    user_email: str = "noreply@portal.local",
    user_phone: str = "9999999999",
) -> PaymentInitiateResponse:
    """
    1. Calculate all financial figures using Decimal arithmetic.
    2. Create a PENDING transaction in MongoDB.
    3. Generate the EaseBuzz hash.
    4. Return the payload the frontend needs to redirect to EaseBuzz.
    """
    settings = get_settings()
    pricing = fest.pricing

    booking_with_amounts = _build_booking_with_amounts(pricing, request.booking)
    base_amount = calculate_base_amount(pricing, request.booking)

    # Coupon discount (validated against EaseBuzz API)
    coupon_discount = Decimal("0.00")
    if request.coupon_code:
        coupon_discount = await validate_coupon(
            request.coupon_code,
            base_amount,
            settings.EASEBUZZ_KEY,
        )
        # Discount must not exceed base amount
        coupon_discount = min(coupon_discount, base_amount).quantize(
            TWOPLACES, ROUND_HALF_UP
        )

    taxable_amount = (base_amount - coupon_discount).quantize(TWOPLACES, ROUND_HALF_UP)

    # GST computed strictly BEFORE handing off to gateway
    gst_percent = Decimal(str(pricing.gst_percent))
    gst_amount = calculate_gst(taxable_amount, gst_percent)

    # Amount EaseBuzz will charge the user
    charge_amount = (taxable_amount + gst_amount).quantize(TWOPLACES, ROUND_HALF_UP)

    # EaseBuzz platform fee (taken from the charged amount)
    fee_percent = settings.EASEBUZZ_FEE_PERCENT
    easebuzz_fee = calculate_easebuzz_fee(charge_amount, fee_percent)

    # Net amount the fest receives after all deductions
    final_net_credited = (charge_amount - easebuzz_fee).quantize(TWOPLACES, ROUND_HALF_UP)

    # Resolve origin_user_id: use provided value for fest-redirect, generate for direct visits
    resolved_origin_user_id = request.origin_user_id or f"portal_{uuid.uuid4()}"

    internal_tx_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)

    transaction = TransactionDB(
        internal_tx_id=internal_tx_id,
        fest_id=request.fest_id,
        origin_user_id=resolved_origin_user_id,
        source=request.source,
        user_name=request.user_name,
        user_state=request.user_state,
        user_district=request.user_district,
        booking=booking_with_amounts,
        base_amount=_amount_str(base_amount),
        gst_amount=_amount_str(gst_amount),
        easebuzz_transaction_fee=_amount_str(easebuzz_fee),
        coupon_discount_applied=_amount_str(coupon_discount),
        final_net_credited=_amount_str(final_net_credited),
        coupon_code=request.coupon_code,
        status=TxStatus.PENDING,
        initiated_at=now,
    )
    await tx_col.insert_one(transaction.model_dump(mode="json"))

    # EaseBuzz requires amount as a 2-dp string
    amount_str = _amount_str(charge_amount)
    productinfo = f"{fest.fest_id}_booking"

    eb_hash = generate_easebuzz_hash(
        key=settings.EASEBUZZ_KEY,
        txnid=internal_tx_id,
        amount=amount_str,
        productinfo=productinfo,
        firstname=request.user_name,
        email=user_email,
        salt=settings.EASEBUZZ_SALT,
        udf1=resolved_origin_user_id,
        udf2=request.fest_id,
    )

    return PaymentInitiateResponse(
        internal_tx_id=internal_tx_id,
        easebuzz_payment_url=settings.easebuzz_payment_url,
        txnid=internal_tx_id,
        key=settings.EASEBUZZ_KEY,
        hash=eb_hash,
        amount=amount_str,
        productinfo=productinfo,
        firstname=request.user_name,
        email=user_email,
        phone=user_phone,
        udf1=resolved_origin_user_id,
        udf2=request.fest_id,
        udf3="",
        udf4="",
        udf5="",
        surl=settings.PORTAL_CALLBACK_URL,
        furl=settings.PORTAL_CALLBACK_URL,
    )
