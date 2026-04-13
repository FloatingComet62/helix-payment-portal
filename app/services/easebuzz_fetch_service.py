"""
EaseBuzz Transaction Fetch Service

Calls EaseBuzz's transaction retrieve API (v2) after a successful webhook
to get the actual settlement amount and payment mode. These values are more
accurate than the S2S webhook body (which does not include net_amount_debit).

API contract:
  POST https://pay.easebuzz.in/transaction/v2
  Body: key={key}&txnid={txnid}&hash={hash}
  Hash: sha512(key|txnid|salt)

Returns a JSON list; we take the first element.
"""
from __future__ import annotations

import hashlib
import logging
import re
from json import JSONDecodeError
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)

TWOPLACES = Decimal("0.01")


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(TWOPLACES, ROUND_HALF_UP)
    except Exception:
        return None


def _extract_decimal(tx_data: dict[str, Any], *candidate_keys: str) -> Decimal | None:
    for key in candidate_keys:
        value = tx_data.get(key)
        parsed = _parse_decimal(value)
        if parsed is not None:
            return parsed

    normalized_lookup = {
        _normalize_key(str(k)): v
        for k, v in tx_data.items()
        if isinstance(k, str)
    }
    for key in candidate_keys:
        value = normalized_lookup.get(_normalize_key(key))
        parsed = _parse_decimal(value)
        if parsed is not None:
            return parsed

    return None


def _unwrap_tx_data(body: Any) -> dict[str, Any] | None:
    candidate: Any = None
    if isinstance(body, list):
        if not body:
            return None
        candidate = body[0]
    elif isinstance(body, dict):
        candidate = body
    else:
        return None

    if not isinstance(candidate, dict):
        return None

    # EaseBuzz responses can be either the tx payload itself or wrapped under "data".
    data = candidate.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return candidate


def _parse_text_payload(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    body = text.strip()
    if not body:
        return parsed

    # Explicit patterns first
    patterns = {
        "settlement_amount": r"settlement\s*amount\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        "service_charge": r"service\s*charge\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        "service_charge_gst": r"(?:service\s*charge\s*gst|gst\s*on\s*service\s*charge|service\s*tax)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        "net_amount_debit": r"(?:net\s*amount\s*debit|net_amount_debit|net\s*amount)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        "mode": r"(?:payment\s*mode|mode)\s*[:=]\s*([A-Za-z0-9_]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            parsed[key] = match.group(1).strip()

    # Generic key=value / key:value tokens
    for match in re.finditer(r"([A-Za-z_ ]{2,40})\s*[:=]\s*([A-Za-z0-9._-]+)", body):
        raw_key = match.group(1).strip()
        raw_value = match.group(2).strip()
        normalized = _normalize_key(raw_key)
        if normalized and raw_value:
            parsed.setdefault(normalized, raw_value)

    return parsed


def derive_fee_and_net_from_payload(
    payload: dict[str, Any],
    charged_amount_str: str,
) -> tuple[str, str, str] | None:
    charged = _parse_decimal(charged_amount_str)
    if charged is None:
        return None

    settlement_amount = _extract_decimal(
        payload,
        "settlement_amount",
        "settled_amount",
        "amount_settled",
        "settlement_amt",
    )
    service_charge = _extract_decimal(
        payload,
        "service_charge",
        "servicecharge",
        "gateway_charge",
        "pg_charge",
        "charges",
    )
    service_charge_gst = _extract_decimal(
        payload,
        "service_charge_gst",
        "gst_on_service_charge",
        "service_tax",
        "tax_on_service_charge",
        "gst",
    )
    net_amount = _extract_decimal(
        payload,
        "net_amount_debit",
        "net_amount",
        "merchant_net_amount",
        "amount_to_merchant",
    )

    net: Decimal | None = None
    fee: Decimal | None = None
    source = ""

    if settlement_amount is not None and Decimal("0.00") <= settlement_amount <= charged:
        net = settlement_amount
        fee = (charged - net).quantize(TWOPLACES, ROUND_HALF_UP)
        source = "settlement_amount"
    elif service_charge is not None:
        fee_candidate = service_charge + (service_charge_gst or Decimal("0.00"))
        fee_candidate = fee_candidate.quantize(TWOPLACES, ROUND_HALF_UP)
        if Decimal("0.00") <= fee_candidate <= charged:
            fee = fee_candidate
            net = (charged - fee).quantize(TWOPLACES, ROUND_HALF_UP)
            source = "service_charge(+gst)"

    if net is None and net_amount is not None and Decimal("0.00") <= net_amount <= charged:
        net = net_amount
        fee = (charged - net).quantize(TWOPLACES, ROUND_HALF_UP)
        source = "net_amount_debit"

    if net is None or fee is None:
        return None
    return (f"{fee:.2f}", f"{net:.2f}", source)


async def fetch_and_update_actual_fee(
    tx_col: AsyncIOMotorCollection,
    internal_tx_id: str,
    charged_amount_str: str,
    key: str,
    salt: str,
    easebuzz_env: str,
) -> bool:
    """
    Background task: call EaseBuzz transaction fetch API, then update DB with
    actual easebuzz_transaction_fee, final_net_credited, and payment_mode.

    If anything fails (network, unexpected response), we log and return False — the
    transaction stays with "0.00" fee values which admins can see are unfilled.
    """
    try:
        # EaseBuzz fetch hash = sha512(key|txnid|salt)
        hash_input = f"{key}|{internal_tx_id}|{salt}"
        tx_hash = hashlib.sha512(hash_input.encode()).hexdigest()

        base_url = (
            "https://pay.easebuzz.in"
            if easebuzz_env == "prod"
            else "https://testpay.easebuzz.in"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{base_url}/transaction/v2",
                data={"key": key, "txnid": internal_tx_id, "hash": tx_hash},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.warning(
                "EaseBuzz fetch API non-200 for %s: status=%s body=%s",
                internal_tx_id,
                response.status_code,
                response.text[:300],
            )
            return False

        try:
            body = response.json()
        except JSONDecodeError:
            text_payload = _parse_text_payload(response.text)
            if text_payload:
                logger.warning(
                    "EaseBuzz fetch API non-JSON for %s; parsed text payload keys=%s",
                    internal_tx_id,
                    list(text_payload.keys()),
                )
                body = text_payload
            else:
                logger.warning(
                    "EaseBuzz fetch API non-JSON for %s: body=%r",
                    internal_tx_id,
                    response.text[:300],
                )
                return False

        tx_data = _unwrap_tx_data(body)
        if tx_data is None:
            logger.warning("Unexpected EaseBuzz fetch response type for %s: %r", internal_tx_id, body)
            return False

        logger.info("EaseBuzz fetch response for %s: %s", internal_tx_id, tx_data)

        charged = _parse_decimal(charged_amount_str)
        if charged is None:
            logger.warning(
                "Invalid charged amount for %s: charged_amount_str=%r",
                internal_tx_id,
                charged_amount_str,
            )
            return False

        mode = str(tx_data.get("mode") or tx_data.get("payment_mode") or "").strip()

        derived = derive_fee_and_net_from_payload(tx_data, charged_amount_str)
        if not derived:
            logger.warning(
                "Could not derive settlement for %s. fields=%s",
                internal_tx_id,
                list(tx_data.keys()),
            )
            # Still update mode if available
            if mode:
                await tx_col.update_one(
                    {"internal_tx_id": internal_tx_id},
                    {"$set": {"payment_mode": mode}},
                )
            return False

        fee_str, net_str, source = derived

        update: dict = {
            "easebuzz_transaction_fee": fee_str,
            "final_net_credited": net_str,
        }
        if mode:
            update["payment_mode"] = mode

        await tx_col.update_one(
            {"internal_tx_id": internal_tx_id},
            {"$set": update},
        )

        logger.info(
            "Actual fee updated for %s: charged=%s net=%s fee=%s mode=%s source=%s",
            internal_tx_id,
            charged_amount_str,
            net_str,
            fee_str,
            mode,
            source,
        )
        return True

    except Exception:
        logger.exception("Failed to fetch/update EaseBuzz fee for %s", internal_tx_id)
        return False
