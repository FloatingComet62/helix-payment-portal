from __future__ import annotations

from decimal import Decimal

import httpx

from app.core.config import get_settings
from app.core.exceptions import CouponValidationError


async def validate_coupon(
    coupon_code: str,
    amount: Decimal,
    merchant_key: str,
) -> Decimal:
    """
    Validate a coupon code against the EaseBuzz Coupon API.

    Returns the discount amount as a Decimal (e.g. Decimal("200.00")).
    Raises CouponValidationError if the coupon is invalid or the API call fails.

    EaseBuzz coupon validation endpoint (test/prod):
        POST https://testpay.easebuzz.in/coupon/validate   (test)
        POST https://pay.easebuzz.in/coupon/validate       (prod)

    Required payload fields: key, coupon_code, amount
    """
    settings = get_settings()
    if settings.EASEBUZZ_ENV == "prod":
        url = "https://pay.easebuzz.in/coupon/validate"
    else:
        url = "https://testpay.easebuzz.in/coupon/validate"

    payload = {
        "key": merchant_key,
        "coupon_code": coupon_code,
        "amount": f"{amount:.2f}",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=payload)
    except httpx.RequestError as exc:
        raise CouponValidationError(f"Network error contacting EaseBuzz: {exc}")

    if resp.status_code != 200:
        raise CouponValidationError(
            f"EaseBuzz coupon API returned HTTP {resp.status_code}"
        )

    try:
        body = resp.json()
    except Exception:
        raise CouponValidationError("Invalid JSON response from EaseBuzz coupon API")

    # EaseBuzz returns status=1 on success with a `discount` field
    if body.get("status") != 1:
        error_msg = body.get("error_desc") or body.get("msg") or "Invalid coupon"
        raise CouponValidationError(error_msg)

    raw_discount = body.get("discount", "0")
    try:
        return Decimal(str(raw_discount))
    except Exception:
        raise CouponValidationError(
            f"Unreadable discount value from EaseBuzz: {raw_discount!r}"
        )
