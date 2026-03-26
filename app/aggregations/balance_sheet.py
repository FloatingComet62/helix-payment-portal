from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_transactions_col

# ---------------------------------------------------------------------------
# Balance Sheet Aggregation Pipeline
#
# Groups completed transactions by fest_id and sums all financial fields.
#
# KEY DESIGN DECISIONS:
#   - Amounts are stored as strings in MongoDB (e.g. "1500.00") to preserve
#     Decimal precision in the application layer.
#   - The pipeline converts them with $toDecimal before summing, so MongoDB's
#     arithmetic operates on exact 128-bit decimal types — not IEEE-754 doubles.
#   - The final $addFields converts aggregated Decimals back to strings so the
#     JSON response doesn't suffer from floating-point serialisation drift.
#   - Only SUCCESS / NOTIFIED_SUCCESS / NOTIFIED_FAILURE statuses are included
#     (NOTIFIED_FAILURE means money was received but the fest wasn't notified yet —
#     the finance office still needs to account for those funds).
# ---------------------------------------------------------------------------

BALANCE_SHEET_PIPELINE: list[dict] = [
    # Step 1: Only include transactions where payment was actually received
    {
        "$match": {
            "status": {
                "$in": [
                    "SUCCESS",
                    "NOTIFIED_SUCCESS",
                    "NOTIFIED_FAILURE",
                ]
            }
        }
    },
    # Step 2: Convert string amounts to MongoDB Decimal128 for exact arithmetic
    {
        "$addFields": {
            "base_dec":     {"$toDecimal": "$base_amount"},
            "gst_dec":      {"$toDecimal": "$gst_amount"},
            "fee_dec":      {"$toDecimal": "$easebuzz_transaction_fee"},
            "discount_dec": {"$toDecimal": "$coupon_discount_applied"},
            "net_dec":      {"$toDecimal": "$final_net_credited"},
        }
    },
    # Step 3: Group by fest and sum each financial column
    {
        "$group": {
            "_id": "$fest_id",
            "total_transactions":    {"$sum": 1},
            "total_base_revenue":    {"$sum": "$base_dec"},
            "total_gst_collected":   {"$sum": "$gst_dec"},
            "total_easebuzz_fees":   {"$sum": "$fee_dec"},
            "total_discounts_given": {"$sum": "$discount_dec"},
            "total_net_creditable":  {"$sum": "$net_dec"},
        }
    },
    # Step 4: Convert Decimal128 sums back to strings for safe JSON serialisation
    {
        "$addFields": {
            "fest_id":               "$_id",
            "total_base_revenue":    {"$toString": "$total_base_revenue"},
            "total_gst_collected":   {"$toString": "$total_gst_collected"},
            "total_easebuzz_fees":   {"$toString": "$total_easebuzz_fees"},
            "total_discounts_given": {"$toString": "$total_discounts_given"},
            "total_net_creditable":  {"$toString": "$total_net_creditable"},
        }
    },
    # Step 5: Drop internal _id, keep fest_id as a named field
    {"$project": {"_id": 0}},
    # Step 6: Alphabetical order by fest_id for consistent report ordering
    {"$sort": {"fest_id": 1}},
]


async def run_balance_sheet(db: AsyncIOMotorDatabase) -> list[dict]:
    """
    Execute the balance sheet aggregation and return results as a list of dicts.
    Each dict represents one fest with all financial totals as strings.

    Example result entry:
    {
        "fest_id": "HackTU_2026",
        "total_transactions": 142,
        "total_base_revenue": "213000.00",
        "total_gst_collected": "38340.00",
        "total_easebuzz_fees": "5026.80",
        "total_discounts_given": "3200.00",
        "total_net_creditable": "204633.20"
    }
    """
    tx_col = get_transactions_col(db)
    cursor = tx_col.aggregate(BALANCE_SHEET_PIPELINE)
    return [doc async for doc in cursor]
