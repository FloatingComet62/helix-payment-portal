from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.aggregations.balance_sheet import run_balance_sheet
from app.core.config import get_settings
from app.core.database import get_db, get_transactions_col
from app.core.exceptions import AdminAuthError

router = APIRouter(prefix="/admin/export", tags=["Export"])

# Ordered column headers for the transactions CSV
_TX_HEADERS = [
    "internal_tx_id",
    "easebuzz_txid",
    "fest_id",
    "origin_user_id",
    "source",
    "user_name",
    "user_state",
    "user_district",
    "accommodation_days",
    "food_days",
    "accommodation_amount",
    "food_amount",
    "base_amount",
    "coupon_code",
    "coupon_discount_applied",
    "gst_amount",
    "easebuzz_transaction_fee",
    "final_net_credited",
    "status",
    "initiated_at",
    "completed_at",
]

_BS_HEADERS = [
    "fest_id",
    "total_transactions",
    "total_base_revenue",
    "total_gst_collected",
    "total_easebuzz_fees",
    "total_discounts_given",
    "total_net_creditable",
]


def _verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    if x_admin_key != get_settings().ADMIN_API_KEY:
        raise AdminAuthError()


AdminAuth = Depends(_verify_admin_key)


def _to_csv_bytes(headers: list[str], rows: list[list]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _streaming_csv(content: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/transactions/{fest_id}")
async def download_transactions(
    fest_id: str,
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Download all transactions for a given fest as a CSV file.
    Includes every status (PENDING through NOTIFIED_*).
    Monetary columns are 2-decimal-place strings — safe to open in Excel/Sheets.
    """
    tx_col = get_transactions_col(db)
    cursor = tx_col.find({"fest_id": fest_id}, {"_id": 0, "notification_logs": 0})

    rows: list[list] = []
    async for doc in cursor:
        booking = doc.get("booking", {})
        rows.append([
            doc.get("internal_tx_id", ""),
            doc.get("easebuzz_txid", ""),
            doc.get("fest_id", ""),
            doc.get("origin_user_id", ""),
            doc.get("source", ""),
            doc.get("user_name", ""),
            doc.get("user_state", ""),
            doc.get("user_district", ""),
            booking.get("accommodation_days", 0),
            booking.get("food_days", 0),
            booking.get("accommodation_amount", "0.00"),
            booking.get("food_amount", "0.00"),
            doc.get("base_amount", ""),
            doc.get("coupon_code", ""),
            doc.get("coupon_discount_applied", ""),
            doc.get("gst_amount", ""),
            doc.get("easebuzz_transaction_fee", ""),
            doc.get("final_net_credited", ""),
            doc.get("status", ""),
            doc.get("initiated_at", ""),
            doc.get("completed_at", ""),
        ])

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{fest_id}_transactions_{timestamp}.csv"
    return _streaming_csv(_to_csv_bytes(_TX_HEADERS, rows), filename)


@router.get("/balance-sheet")
async def download_balance_sheet(
    _: None = AdminAuth,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Download the full balance sheet as a CSV file.
    One row per fest. Only includes SUCCESS / NOTIFIED_* transactions.
    All monetary columns are strings with 2 decimal places.
    """
    results = await run_balance_sheet(db)

    rows: list[list] = [
        [
            r.get("fest_id", ""),
            r.get("total_transactions", 0),
            r.get("total_base_revenue", "0.00"),
            r.get("total_gst_collected", "0.00"),
            r.get("total_easebuzz_fees", "0.00"),
            r.get("total_discounts_given", "0.00"),
            r.get("total_net_creditable", "0.00"),
        ]
        for r in results
    ]

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"balance_sheet_{timestamp}.csv"
    return _streaming_csv(_to_csv_bytes(_BS_HEADERS, rows), filename)
