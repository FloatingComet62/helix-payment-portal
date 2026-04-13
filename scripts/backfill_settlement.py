from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

# Allow running as: python scripts/backfill_settlement.py ...
# by adding project root (Central_Payment_2) to sys.path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.easebuzz_fetch_service import fetch_and_update_actual_fee

TWOPLACES = Decimal("0.01")
PAID_STATUSES = ["SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"]


class BackfillSettings:
    def __init__(self) -> None:
        self._load_local_env(os.path.join(PROJECT_ROOT, ".env"))
        self.mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        self.mongodb_db_name = os.getenv("MONGODB_DB_NAME", "central_payment")
        self.easebuzz_key = os.getenv("EASEBUZZ_KEY", "").strip()
        self.easebuzz_salt = os.getenv("EASEBUZZ_SALT", "").strip()
        self.easebuzz_env = os.getenv("EASEBUZZ_ENV", "test").strip() or "test"

        missing = []
        if not self.easebuzz_key:
            missing.append("EASEBUZZ_KEY")
        if not self.easebuzz_salt:
            missing.append("EASEBUZZ_SALT")
        if missing:
            raise ValueError(
                "Missing required environment variables for backfill: "
                + ", ".join(missing)
                + ". Set them in shell or Central_Payment_2/.env"
            )

    @staticmethod
    def _load_local_env(path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value


def _parse_decimal(value: Any, field_name: str, tx_id: str) -> Decimal:
    try:
        return Decimal(str(value)).quantize(TWOPLACES, ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(
            f"Transaction {tx_id}: invalid decimal in {field_name!r}: {value!r}"
        ) from exc


def _derive_charged_amount(doc: dict[str, Any]) -> str:
    tx_id = str(doc.get("internal_tx_id", "UNKNOWN"))
    base = _parse_decimal(doc.get("base_amount", "0.00"), "base_amount", tx_id)
    coupon = _parse_decimal(
        doc.get("coupon_discount_applied", "0.00"), "coupon_discount_applied", tx_id
    )
    gst = _parse_decimal(doc.get("gst_amount", "0.00"), "gst_amount", tx_id)
    charged = (base - coupon + gst).quantize(TWOPLACES, ROUND_HALF_UP)
    return f"{charged:.2f}"


async def _run_backfill(args: argparse.Namespace) -> None:
    settings = BackfillSettings()
    client = AsyncIOMotorClient(
        settings.mongodb_uri,
        maxPoolSize=20,
        minPoolSize=1,
        maxIdleTimeMS=45_000,
        connectTimeoutMS=10_000,
        socketTimeoutMS=30_000,
        serverSelectionTimeoutMS=10_000,
        retryWrites=True,
    )
    db = client[settings.mongodb_db_name]
    tx_col = db["transactions"]

    try:
        statuses = list(PAID_STATUSES)
        if args.include_refunded:
            statuses.append("REFUNDED")

        query: dict[str, Any] = {"status": {"$in": statuses}}
        if args.fest_id:
            query["fest_id"] = args.fest_id
        if args.tx_id:
            query["internal_tx_id"] = args.tx_id

        projection = {
            "_id": 0,
            "internal_tx_id": 1,
            "fest_id": 1,
            "status": 1,
            "base_amount": 1,
            "coupon_discount_applied": 1,
            "gst_amount": 1,
            "easebuzz_transaction_fee": 1,
            "final_net_credited": 1,
            "initiated_at": 1,
        }

        cursor = tx_col.find(query, projection).sort("initiated_at", 1)
        if args.limit > 0:
            cursor = cursor.limit(args.limit)

        scanned = 0
        updated = 0
        skipped = 0
        failed = 0

        async for doc in cursor:
            scanned += 1
            tx_id = str(doc.get("internal_tx_id", "")).strip()
            if not tx_id:
                skipped += 1
                print(f"[SKIP] Missing internal_tx_id at row #{scanned}")
                continue

            try:
                charged_amount = _derive_charged_amount(doc)
            except ValueError as exc:
                failed += 1
                print(f"[FAIL] {exc}")
                continue

            current_fee = doc.get("easebuzz_transaction_fee", "0.00")
            current_net = doc.get("final_net_credited", "0.00")

            if args.dry_run:
                print(
                    f"[DRY-RUN] tx={tx_id} fest={doc.get('fest_id')} status={doc.get('status')} "
                    f"charged={charged_amount} current_fee={current_fee} current_net={current_net}"
                )
                updated += 1
                continue

            try:
                did_update = await fetch_and_update_actual_fee(
                    tx_col=tx_col,
                    internal_tx_id=tx_id,
                    charged_amount_str=charged_amount,
                    key=settings.easebuzz_key,
                    salt=settings.easebuzz_salt,
                    easebuzz_env=settings.easebuzz_env,
                )
                if did_update:
                    updated += 1
                    print(
                        f"[OK] tx={tx_id} charged={charged_amount} "
                        f"(prev fee={current_fee}, prev net={current_net})"
                    )
                else:
                    skipped += 1
                    print(
                        f"[WARN] tx={tx_id} no update from EaseBuzz response "
                        f"(prev fee={current_fee}, prev net={current_net})"
                    )
            except Exception as exc:
                failed += 1
                print(f"[FAIL] tx={tx_id}: {exc}")

        print(
            "\nSummary: "
            f"scanned={scanned} updated={updated} skipped={skipped} failed={failed} "
            f"mode={'dry-run' if args.dry_run else 'write'}"
        )
    finally:
        client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill transaction settlement fields from EaseBuzz fetch API. "
            "Updates easebuzz_transaction_fee and final_net_credited."
        )
    )
    parser.add_argument(
        "--fest-id",
        help="Backfill only for one fest_id",
    )
    parser.add_argument(
        "--tx-id",
        help="Backfill only one transaction by internal_tx_id",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of transactions to process (0 = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List transactions that would be processed without calling EaseBuzz/write updates",
    )
    parser.add_argument(
        "--include-refunded",
        action="store_true",
        help="Also include REFUNDED transactions (default: only paid statuses)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run_backfill(args))


if __name__ == "__main__":
    main()
