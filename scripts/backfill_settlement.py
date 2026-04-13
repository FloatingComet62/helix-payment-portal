from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

# Allow running as: python scripts/backfill_settlement.py ...
# by adding project root (Central_Payment_2) to sys.path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.services.easebuzz_fetch_service import fetch_and_update_actual_fee

TWOPLACES = Decimal("0.01")
DEFAULT_STATUSES = ["NOTIFIED_SUCCESS"]
SUCCESS_CSV_STATUSES = {"success", "captured", "settled"}
DEFAULT_CSV_PATH = Path(PROJECT_ROOT) / "Transactions_EB__2026-04-13.csv"


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
    text = str(value).strip().replace(",", "")
    try:
        return Decimal(text).quantize(TWOPLACES, ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(
            f"Transaction {tx_id}: invalid decimal in {field_name!r}: {value!r}"
        ) from exc


def _safe_decimal(value: Any) -> Decimal | None:
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(TWOPLACES, ROUND_HALF_UP)
    except Exception:
        return None


def _derive_charged_amount_from_db(doc: dict[str, Any]) -> Decimal:
    tx_id = str(doc.get("internal_tx_id", "UNKNOWN"))
    base = _parse_decimal(doc.get("base_amount", "0.00"), "base_amount", tx_id)
    coupon = _parse_decimal(
        doc.get("coupon_discount_applied", "0.00"), "coupon_discount_applied", tx_id
    )
    gst = _parse_decimal(doc.get("gst_amount", "0.00"), "gst_amount", tx_id)
    return (base - coupon + gst).quantize(TWOPLACES, ROUND_HALF_UP)


def _norm_key(key: str) -> str:
    return " ".join(key.strip().lower().replace("_", " ").split())


def _load_csv_index(csv_path: Path) -> dict[str, dict[str, Any]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    index: dict[str, dict[str, Any]] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalized = {_norm_key(k): (v or "").strip() for k, v in row.items() if k}

            easebuzz_id = (
                normalized.get("easebuzz id")
                or normalized.get("easebuzz txid")
                or normalized.get("easebuzz_txid")
                or normalized.get("easebuzzid")
            )
            if not easebuzz_id:
                continue

            amount_raw = normalized.get("amount", "")
            amount = _safe_decimal(amount_raw)
            if amount is None:
                continue

            settlement_amount = _safe_decimal(
                normalized.get("settlement amount")
                or normalized.get("settlement_amount")
                or normalized.get("settled amount")
            )
            service_charge = _safe_decimal(
                normalized.get("service charge")
                or normalized.get("service_charge")
                or normalized.get("gateway charge")
            )
            service_gst = _safe_decimal(
                normalized.get("gst")
                or normalized.get("service charge gst")
                or normalized.get("gst on service charge")
                or normalized.get("service tax")
            )

            status = (normalized.get("transaction status", "") or "").strip().lower()
            index[easebuzz_id] = {
                "amount": amount,
                "status": status,
                "settlement_amount": settlement_amount,
                "service_charge": service_charge,
                "service_gst": service_gst,
            }

    return index


def _mode_fee_percent(mode: str | None, args: argparse.Namespace) -> Decimal:
    m = (mode or "").strip().upper()
    if m.startswith("UPI"):
        return Decimal(str(args.upi_fee_percent)).quantize(TWOPLACES, ROUND_HALF_UP)
    if m in {"CC", "DC", "CARD", "CREDITCARD", "DEBITCARD"}:
        return Decimal(str(args.card_fee_percent)).quantize(TWOPLACES, ROUND_HALF_UP)
    if m in {"NB", "NETBANKING"}:
        return Decimal(str(args.nb_fee_percent)).quantize(TWOPLACES, ROUND_HALF_UP)
    return Decimal(str(args.default_fee_percent)).quantize(TWOPLACES, ROUND_HALF_UP)


def _formula_fee_and_net(
    charged_amount: Decimal,
    payment_mode: str | None,
    args: argparse.Namespace,
) -> tuple[str, str, str]:
    # Gateway usually applies mode fee + GST on that fee.
    fee_percent = _mode_fee_percent(payment_mode, args)
    gst_percent = Decimal(str(args.gateway_gst_percent)).quantize(TWOPLACES, ROUND_HALF_UP)

    service_charge = (charged_amount * fee_percent / Decimal("100")).quantize(TWOPLACES, ROUND_HALF_UP)
    service_charge_gst = (service_charge * gst_percent / Decimal("100")).quantize(TWOPLACES, ROUND_HALF_UP)
    fee = (service_charge + service_charge_gst).quantize(TWOPLACES, ROUND_HALF_UP)
    net = (charged_amount - fee).quantize(TWOPLACES, ROUND_HALF_UP)

    source = f"formula(mode={payment_mode or 'unknown'},fee%={fee_percent},gst%={gst_percent})"
    return f"{fee:.2f}", f"{net:.2f}", source


def _fee_and_net_from_csv_row(
    charged_amount: Decimal,
    csv_row: dict[str, Any],
) -> tuple[str, str, str] | None:
    settlement_amount = csv_row.get("settlement_amount")
    if isinstance(settlement_amount, Decimal) and Decimal("0.00") <= settlement_amount <= charged_amount:
        fee = (charged_amount - settlement_amount).quantize(TWOPLACES, ROUND_HALF_UP)
        return f"{fee:.2f}", f"{settlement_amount:.2f}", "csv(settlement_amount)"

    service_charge = csv_row.get("service_charge")
    service_gst = csv_row.get("service_gst")
    if isinstance(service_charge, Decimal):
        gst = service_gst if isinstance(service_gst, Decimal) else Decimal("0.00")
        fee = (service_charge + gst).quantize(TWOPLACES, ROUND_HALF_UP)
        if Decimal("0.00") <= fee <= charged_amount:
            net = (charged_amount - fee).quantize(TWOPLACES, ROUND_HALF_UP)
            return f"{fee:.2f}", f"{net:.2f}", "csv(service_charge+gst)"

    return None


def _parse_statuses(raw: str) -> list[str]:
    statuses = [part.strip().upper() for part in raw.split(",") if part.strip()]
    return statuses or list(DEFAULT_STATUSES)


async def _run_backfill(args: argparse.Namespace) -> None:
    settings = BackfillSettings()

    csv_index: dict[str, dict[str, Any]] = {}
    if args.csv_path:
        csv_path = Path(args.csv_path)
        csv_index = _load_csv_index(csv_path)
        print(f"[INFO] Loaded {len(csv_index)} rows from CSV: {csv_path}")

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
        statuses = _parse_statuses(args.statuses)
        query: dict[str, Any] = {"status": {"$in": statuses}}
        if args.fest_id:
            query["fest_id"] = args.fest_id
        if args.tx_id:
            query["internal_tx_id"] = args.tx_id

        projection = {
            "_id": 0,
            "internal_tx_id": 1,
            "easebuzz_txid": 1,
            "fest_id": 1,
            "status": 1,
            "payment_mode": 1,
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
        updated_fetch = 0
        updated_formula = 0
        skipped = 0
        failed = 0

        async for doc in cursor:
            scanned += 1
            tx_id = str(doc.get("internal_tx_id", "")).strip()
            easebuzz_txid = str(doc.get("easebuzz_txid", "")).strip()
            mode = str(doc.get("payment_mode", "")).strip() or None

            if not tx_id:
                skipped += 1
                print(f"[SKIP] Missing internal_tx_id at row #{scanned}")
                continue

            try:
                charged = _derive_charged_amount_from_db(doc)
                charged_source = "db"

                if easebuzz_txid and easebuzz_txid in csv_index:
                    csv_row = csv_index[easebuzz_txid]
                    csv_status = str(csv_row.get("status", "")).lower()
                    if csv_status in SUCCESS_CSV_STATUSES:
                        charged = csv_row["amount"]
                        charged_source = "csv"
                    elif csv_status:
                        print(
                            f"[WARN] tx={tx_id} easebuzz={easebuzz_txid} "
                            f"CSV status={csv_status!r}; using DB amount"
                        )

                charged_str = f"{charged:.2f}"
            except ValueError as exc:
                failed += 1
                print(f"[FAIL] {exc}")
                continue

            current_fee = str(doc.get("easebuzz_transaction_fee", "0.00"))
            current_net = str(doc.get("final_net_credited", "0.00"))

            if args.only_zero:
                fee_dec = _safe_decimal(current_fee) or Decimal("0.00")
                net_dec = _safe_decimal(current_net) or Decimal("0.00")
                if fee_dec > Decimal("0.00") and net_dec > Decimal("0.00"):
                    skipped += 1
                    print(f"[SKIP] tx={tx_id} already has fee/net (fee={current_fee}, net={current_net})")
                    continue

            if args.dry_run:
                print(
                    f"[DRY-RUN] tx={tx_id} easebuzz={easebuzz_txid or '-'} "
                    f"status={doc.get('status')} mode={mode or '-'} "
                    f"charged={charged_str} source={charged_source} "
                    f"current_fee={current_fee} current_net={current_net}"
                )
                continue

            did_update_fetch = False
            if not args.skip_fetch:
                try:
                    did_update_fetch = await fetch_and_update_actual_fee(
                        tx_col=tx_col,
                        internal_tx_id=tx_id,
                        charged_amount_str=charged_str,
                        key=settings.easebuzz_key,
                        salt=settings.easebuzz_salt,
                        easebuzz_env=settings.easebuzz_env,
                    )
                except Exception as exc:
                    print(f"[WARN] tx={tx_id} fetch failed hard: {exc}")
                    did_update_fetch = False

            if did_update_fetch:
                updated_fetch += 1
                print(
                    f"[OK:FETCH] tx={tx_id} charged={charged_str} source={charged_source} "
                    f"(prev fee={current_fee}, prev net={current_net})"
                )
                continue

            csv_exact = None
            if easebuzz_txid and easebuzz_txid in csv_index:
                csv_exact = _fee_and_net_from_csv_row(charged, csv_index[easebuzz_txid])

            if csv_exact:
                fee_str, net_str, formula_source = csv_exact
                await tx_col.update_one(
                    {"internal_tx_id": tx_id},
                    {"$set": {"easebuzz_transaction_fee": fee_str, "final_net_credited": net_str}},
                )
                updated_formula += 1
                print(
                    f"[OK:CSV] tx={tx_id} charged={charged_str} fee={fee_str} net={net_str} "
                    f"source={formula_source} (prev fee={current_fee}, prev net={current_net})"
                )
                continue

            if not args.allow_formula:
                skipped += 1
                print(f"[WARN] tx={tx_id} fetch did not update; formula fallback not allowed")
                continue

            fee_str, net_str, formula_source = _formula_fee_and_net(charged, mode, args)
            await tx_col.update_one(
                {"internal_tx_id": tx_id},
                {"$set": {"easebuzz_transaction_fee": fee_str, "final_net_credited": net_str}},
            )
            updated_formula += 1
            print(
                f"[OK:FORMULA] tx={tx_id} charged={charged_str} fee={fee_str} net={net_str} "
                f"source={formula_source} (prev fee={current_fee}, prev net={current_net})"
            )

        print(
            "\nSummary: "
            f"scanned={scanned} fetch_updates={updated_fetch} formula_updates={updated_formula} "
            f"skipped={skipped} failed={failed} mode={'dry-run' if args.dry_run else 'write'}"
        )
    finally:
        client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill transaction settlement fields. "
            "Default scope is NOTIFIED_SUCCESS transactions."
        )
    )
    parser.add_argument(
        "--fest-id",
        help="Backfill only one fest_id",
    )
    parser.add_argument(
        "--tx-id",
        help="Backfill only one transaction by internal_tx_id",
    )
    parser.add_argument(
        "--statuses",
        default=",".join(DEFAULT_STATUSES),
        help="Comma-separated DB statuses to process (default: NOTIFIED_SUCCESS)",
    )
    parser.add_argument(
        "--csv-path",
        default=str(DEFAULT_CSV_PATH) if DEFAULT_CSV_PATH.exists() else "",
        help="CSV path for charged amount reference (default: Transactions_EB__2026-04-13.csv if present)",
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
        help="Print what would be processed without writing updates",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip EaseBuzz fetch API and directly apply formula fallback",
    )
    parser.add_argument(
        "--allow-formula",
        action="store_true",
        help="Allow formula fallback when fetch API cannot provide settlement values (disabled by default)",
    )
    parser.add_argument(
        "--only-zero",
        action="store_true",
        help="Only update rows where fee/net are still zero or empty",
    )
    parser.add_argument(
        "--upi-fee-percent",
        type=str,
        default="0.50",
        help="Gateway service charge percent for UPI mode before GST (default: 0.50)",
    )
    parser.add_argument(
        "--card-fee-percent",
        type=str,
        default="2.00",
        help="Gateway service charge percent for card modes before GST (default: 2.00)",
    )
    parser.add_argument(
        "--nb-fee-percent",
        type=str,
        default="2.00",
        help="Gateway service charge percent for netbanking before GST (default: 2.00)",
    )
    parser.add_argument(
        "--default-fee-percent",
        type=str,
        default="2.00",
        help="Fallback gateway service charge percent when mode unknown (default: 2.00)",
    )
    parser.add_argument(
        "--gateway-gst-percent",
        type=str,
        default="18.00",
        help="GST percent applied on gateway service charge (default: 18.00)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run_backfill(args))


if __name__ == "__main__":
    main()
