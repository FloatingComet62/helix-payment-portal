#!/usr/bin/env python3
"""
fix_fees.py — Backfill actual EaseBuzz fee/settlement data for transactions
              where easebuzz_transaction_fee is still "0.00".

Usage:
  # Fix ALL success transactions with fee = "0.00" (bulk backfill via EaseBuzz API)
  python scripts/fix_fees.py

  # Manually set settlement amount + mode for a specific transaction (from dashboard)
  python scripts/fix_fees.py --tx-id <id> --settlement 506.99 --mode UPI

  # Dry run — show what would change, nothing written to DB
  python scripts/fix_fees.py --dry-run
  python scripts/fix_fees.py --tx-id <id> --settlement 506.99 --mode UPI --dry-run

  # Fix ALL success txns, even if fee != 0.00
  python scripts/fix_fees.py --all

  # Run from a different .env file
  python scripts/fix_fees.py --env /path/to/.env

Run from the Central_Payment_2 directory:
  cd Central_Payment_2
  python scripts/fix_fees.py
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from json import JSONDecodeError
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dependency check — give a clear message if packages are missing
# ---------------------------------------------------------------------------
try:
    import httpx
except ImportError:
    sys.exit("ERROR: httpx not installed. Run: pip install httpx")

try:
    import pymongo
    from pymongo import MongoClient
except ImportError:
    sys.exit("ERROR: pymongo not installed. Run: pip install pymongo")

try:
    from dotenv import dotenv_values
except ImportError:
    sys.exit("ERROR: python-dotenv not installed. Run: pip install python-dotenv")

# ---------------------------------------------------------------------------
# Helpers — fee derivation logic (mirrors easebuzz_fetch_service.py)
# ---------------------------------------------------------------------------

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
        val = tx_data.get(key)
        parsed = _parse_decimal(val)
        if parsed is not None:
            return parsed
    normalized_lookup = {
        _normalize_key(str(k)): v for k, v in tx_data.items() if isinstance(k, str)
    }
    for key in candidate_keys:
        val = normalized_lookup.get(_normalize_key(key))
        parsed = _parse_decimal(val)
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
    body = html.unescape(body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    sep = r"(?:\s*[:=]\s*|\s+)"
    patterns = {
        "settlement_amount": rf"settlement\s*amount{sep}([0-9]+(?:\.[0-9]+)?)",
        "service_charge": rf"service\s*charge{sep}([0-9]+(?:\.[0-9]+)?)",
        "service_charge_gst": rf"(?:service\s*charge\s*gst|gst\s*on\s*service|service\s*tax){sep}([0-9]+(?:\.[0-9]+)?)",
        "net_amount_debit": rf"(?:net\s*amount\s*debit|net_amount_debit|net\s*amount){sep}([0-9]+(?:\.[0-9]+)?)",
        "mode": rf"(?:payment\s*mode|mode){sep}([A-Za-z0-9_]+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, body, flags=re.IGNORECASE)
        if m:
            parsed[key] = m.group(1).strip()
    return parsed


def derive_fee_and_net(tx_data: dict[str, Any], charged_amount_str: str) -> tuple[str, str, str] | None:
    charged = _parse_decimal(charged_amount_str)
    if charged is None:
        return None

    settlement_amount = _extract_decimal(tx_data, "settlement_amount", "settled_amount", "amount_settled")
    service_charge = _extract_decimal(tx_data, "service_charge", "servicecharge", "gateway_charge", "charges")
    service_charge_gst = _extract_decimal(tx_data, "service_charge_gst", "gst_on_service_charge", "service_tax", "gst")
    net_amount = _extract_decimal(tx_data, "net_amount_debit", "net_amount", "merchant_net_amount")

    net: Decimal | None = None
    fee: Decimal | None = None
    source = ""

    if settlement_amount is not None and Decimal("0") <= settlement_amount <= charged:
        net = settlement_amount
        fee = (charged - net).quantize(TWOPLACES, ROUND_HALF_UP)
        source = "settlement_amount"
    elif service_charge is not None:
        fee_candidate = service_charge + (service_charge_gst or Decimal("0"))
        fee_candidate = fee_candidate.quantize(TWOPLACES, ROUND_HALF_UP)
        if Decimal("0") <= fee_candidate <= charged:
            fee = fee_candidate
            net = (charged - fee).quantize(TWOPLACES, ROUND_HALF_UP)
            source = f"service_charge({service_charge})+gst({service_charge_gst or 0})"

    if net is None and net_amount is not None and Decimal("0") <= net_amount <= charged:
        net = net_amount
        fee = (charged - net).quantize(TWOPLACES, ROUND_HALF_UP)
        source = "net_amount_debit"

    if net is None or fee is None:
        return None
    return (f"{fee:.2f}", f"{net:.2f}", source)


# ---------------------------------------------------------------------------
# EaseBuzz API call
# ---------------------------------------------------------------------------

def call_easebuzz_fetch(
    key: str,
    salt: str,
    transaction_url: str,
    internal_tx_id: str,
    show_raw: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """
    Call EaseBuzz transaction fetch API.
    Returns (tx_data_dict, raw_response_text).
    tx_data_dict is None if the call failed or could not be parsed.
    """
    hash_input = f"{key}|{internal_tx_id}|{salt}"
    tx_hash = hashlib.sha512(hash_input.encode()).hexdigest()

    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                transaction_url,
                data={"key": key, "txnid": internal_tx_id, "hash": tx_hash},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        return None, f"Network error: {exc}"

    raw = response.text

    if show_raw:
        print(f"\n  [RAW RESPONSE] status={response.status_code}")
        print(f"  {raw[:2000]}")

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {raw[:200]}"

    try:
        body = response.json()
    except JSONDecodeError:
        text_payload = _parse_text_payload(raw)
        if text_payload:
            return _unwrap_tx_data(text_payload), raw
        return None, f"Non-JSON response: {raw[:200]}"

    tx_data = _unwrap_tx_data(body)
    return tx_data, raw


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

SUCCESS_STATUSES = {"SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"}


def compute_charged_amount(tx: dict) -> str | None:
    """Re-derive the charge_amount from what's stored in the transaction."""
    # charge_amount = base_amount - coupon_discount_applied + gst_amount
    try:
        base = Decimal(str(tx.get("base_amount", "0")))
        coupon = Decimal(str(tx.get("coupon_discount_applied", "0")))
        gst = Decimal(str(tx.get("gst_amount", "0")))
        charge = (base - coupon + gst).quantize(TWOPLACES, ROUND_HALF_UP)
        return f"{charge:.2f}"
    except Exception:
        return None


def process_transaction_manual(
    tx: dict,
    settlement: str,
    mode: str,
    col: pymongo.collection.Collection,
    dry_run: bool,
) -> dict:
    """
    Apply manually-specified settlement amount + mode for a transaction.
    fee = charged_amount - settlement_amount.
    """
    internal_tx_id = tx.get("internal_tx_id", "?")
    user_name = tx.get("user_name", "?")

    charged_amount = compute_charged_amount(tx)
    if not charged_amount:
        print(f"  SKIP — could not compute charged amount from DB fields")
        return {"tx_id": internal_tx_id, "status": "skip", "reason": "bad_db_fields"}

    charged = _parse_decimal(charged_amount)
    net = _parse_decimal(settlement)

    if charged is None or net is None:
        print(f"  SKIP — invalid charged={charged_amount} or settlement={settlement}")
        return {"tx_id": internal_tx_id, "status": "skip", "reason": "bad_amounts"}

    if net > charged:
        print(f"  ERROR — settlement ({net}) > charged ({charged}). Check values.")
        return {"tx_id": internal_tx_id, "status": "error", "reason": "settlement>charged"}

    fee = (charged - net).quantize(TWOPLACES, ROUND_HALF_UP)
    fee_str = f"{fee:.2f}"
    net_str = f"{net:.2f}"

    print(f"  {internal_tx_id[:8]}… user={user_name!r} charged={charged_amount}")
    print(f"  => settlement={net_str} fee={fee_str} mode={mode!r} (manual)")

    if dry_run:
        print(f"  [DRY RUN] would write fee={fee_str} net={net_str} mode={mode}")
        return {"tx_id": internal_tx_id, "status": "dry_run", "fee": fee_str, "net": net_str, "mode": mode}

    update: dict = {
        "easebuzz_transaction_fee": fee_str,
        "final_net_credited": net_str,
    }
    if mode:
        update["payment_mode"] = mode.upper()

    col.update_one({"internal_tx_id": internal_tx_id}, {"$set": update})
    print(f"  UPDATED in DB")
    return {"tx_id": internal_tx_id, "status": "updated", "fee": fee_str, "net": net_str, "mode": mode}


def process_transaction(
    tx: dict,
    key: str,
    salt: str,
    transaction_url: str,
    col: pymongo.collection.Collection,
    dry_run: bool,
    show_raw: bool,
    index: int,
    total: int,
) -> dict:
    """
    Fetch actual fee via EaseBuzz API for one transaction and optionally update DB.
    Returns a result dict for the summary report.
    """
    internal_tx_id = tx.get("internal_tx_id", "?")
    fest_id = tx.get("fest_id", "?")
    user_name = tx.get("user_name", "?")
    current_fee = tx.get("easebuzz_transaction_fee", "?")

    prefix = f"[{index}/{total}] {internal_tx_id[:8]}…"

    charged_amount = compute_charged_amount(tx)
    if not charged_amount:
        print(f"{prefix} SKIP — could not compute charged amount from DB fields")
        return {"tx_id": internal_tx_id, "status": "skip", "reason": "bad_db_fields"}

    print(f"{prefix} fest={fest_id} user={user_name!r} charged={charged_amount} current_fee={current_fee}")

    tx_data, raw = call_easebuzz_fetch(key, salt, transaction_url, internal_tx_id, show_raw)

    if tx_data is None:
        print(f"  ERROR contacting EaseBuzz API: {raw[:200]}")
        print(f"  TIP: Use --settlement <amount> --mode <UPI|CC|DC|NB> to set manually from dashboard")
        return {"tx_id": internal_tx_id, "status": "error", "reason": raw[:100]}

    mode = str(tx_data.get("mode") or tx_data.get("payment_mode") or "").strip()
    derived = derive_fee_and_net(tx_data, charged_amount)

    if not derived:
        available_keys = list(tx_data.keys())
        print(f"  WARN — could not derive settlement. EaseBuzz returned fields: {available_keys}")
        print(f"  TIP: Use --settlement <amount> --mode <UPI|CC|DC|NB> to set manually from dashboard")
        if mode and not dry_run:
            col.update_one({"internal_tx_id": internal_tx_id}, {"$set": {"payment_mode": mode}})
            print(f"  Updated payment_mode={mode} (fee derivation failed)")
        return {"tx_id": internal_tx_id, "status": "partial", "mode": mode, "fields": available_keys}

    fee_str, net_str, source = derived
    print(f"  => fee={fee_str} net={net_str} mode={mode!r} (source: {source})")

    if dry_run:
        print(f"  [DRY RUN] would write fee={fee_str} net={net_str} mode={mode}")
        return {"tx_id": internal_tx_id, "status": "dry_run", "fee": fee_str, "net": net_str, "mode": mode}

    update: dict = {
        "easebuzz_transaction_fee": fee_str,
        "final_net_credited": net_str,
    }
    if mode:
        update["payment_mode"] = mode

    col.update_one({"internal_tx_id": internal_tx_id}, {"$set": update})
    print(f"  UPDATED in DB")
    return {"tx_id": internal_tx_id, "status": "updated", "fee": fee_str, "net": net_str, "mode": mode}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill actual EaseBuzz fee data into MongoDB")
    parser.add_argument("--tx-id", help="Fix a single transaction by internal_tx_id")
    parser.add_argument("--settlement", help="Manual settlement amount (from EaseBuzz dashboard). Requires --tx-id")
    parser.add_argument("--mode", help="Payment mode (UPI, CC, DC, NB). Used with --settlement")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    parser.add_argument("--show-raw", action="store_true", help="Print raw EaseBuzz API response")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument("--all", action="store_true", help="Fix ALL success txns, even if fee != 0.00")
    args = parser.parse_args()

    if args.settlement and not args.tx_id:
        sys.exit("ERROR: --settlement requires --tx-id")

    # Load config from .env
    env_path = Path(args.env)
    if not env_path.exists():
        sys.exit(f"ERROR: .env file not found at {env_path.resolve()}\nRun from Central_Payment_2 directory or use --env /path/to/.env")

    cfg = dotenv_values(env_path)

    key = cfg.get("EASEBUZZ_KEY") or ""
    salt = cfg.get("EASEBUZZ_SALT") or ""
    eb_env = cfg.get("EASEBUZZ_ENV", "test")
    mongo_uri = cfg.get("MONGODB_URI") or "mongodb://localhost:27017"
    db_name = cfg.get("MONGODB_DB_NAME") or "central_payment"

    if not key or not salt:
        sys.exit("ERROR: EASEBUZZ_KEY and EASEBUZZ_SALT must be set in .env")

    base_url = "https://pay.easebuzz.in" if eb_env == "prod" else "https://testpay.easebuzz.in"
    transaction_url = f"{base_url}/transaction/v2"

    print(f"EaseBuzz env: {eb_env}")
    print(f"Transaction fetch URL: {transaction_url}")
    print(f"MongoDB: {mongo_uri[:40]}… db={db_name}")
    if args.dry_run:
        print("*** DRY RUN — no DB writes ***\n")

    # Connect to MongoDB
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to MongoDB: {exc}")

    col = client[db_name]["transactions"]

    # Build query
    if args.tx_id:
        query: dict = {"internal_tx_id": args.tx_id}
    elif args.all:
        query = {"status": {"$in": list(SUCCESS_STATUSES)}}
    else:
        # Default: only transactions where fee is still the placeholder
        query = {
            "status": {"$in": list(SUCCESS_STATUSES)},
            "easebuzz_transaction_fee": "0.00",
        }

    transactions = list(col.find(query, {"_id": 0}))
    total = len(transactions)

    if total == 0:
        print("No transactions match the criteria. Nothing to do.")
        client.close()
        return

    print(f"Found {total} transaction(s) to process.\n")

    results = []
    for i, tx in enumerate(transactions, start=1):
        if args.settlement:
            # Manual mode — user supplies settlement amount from EaseBuzz dashboard
            result = process_transaction_manual(
                tx=tx,
                settlement=args.settlement,
                mode=args.mode or "",
                col=col,
                dry_run=args.dry_run,
            )
        else:
            result = process_transaction(
                tx=tx,
                key=key,
                salt=salt,
                transaction_url=transaction_url,
                col=col,
                dry_run=args.dry_run,
                show_raw=args.show_raw,
                index=i,
                total=total,
            )
            if i < total:
                time.sleep(0.3)  # be polite to EaseBuzz API
        results.append(result)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    for status, count in sorted(counts.items()):
        print(f"  {status:12s}: {count}")

    updated = [r for r in results if r["status"] == "updated"]
    if updated:
        print(f"\nUpdated {len(updated)} transaction(s):")
        for r in updated:
            print(f"  {r['tx_id']}  fee={r['fee']}  net={r['net']}  mode={r.get('mode','?')}")

    errors = [r for r in results if r["status"] in ("error", "partial")]
    if errors:
        print(f"\nFailed/partial ({len(errors)}):")
        for r in errors:
            print(f"  {r['tx_id']}  reason={r.get('reason', r.get('fields', '?'))}")

    client.close()


if __name__ == "__main__":
    main()
