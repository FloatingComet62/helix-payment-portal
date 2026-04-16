"""
Backfill script: reads trasactions.csv and updates the transactions_copy
collection with user_email, user_phone, and selected_events for each
origin_user_id.

CSV columns used:
  origin_user_id  → match key
  email           → user_email
  phoneNumber     → user_phone  (normalised to +91XXXXXXXXXX)
  Event Name      → one UUID per row; grouped into selected_events list

Usage:
    MONGODB_URI=mongodb+srv://... python scripts/update_issue.py
    # or with .env in project root:
    python scripts/update_issue.py
"""

import os
import re
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URI     = os.environ["MONGODB_URI"]
MONGODB_DB      = os.environ.get("MONGODB_DB_NAME", "central_payment")
COLLECTION_NAME = "transactions"
CSV_PATH        = "trasactions.csv"


def normalise_phone(raw) -> str:
    """
    Convert a raw phone value to +91XXXXXXXXXX.
    Accepts: 9876543210 / '9876543210' / '+919876543210' / '919876543210'
    Returns empty string if it can't be normalised.
    """
    if pd.isna(raw) or str(raw).strip() == "":
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) == 13 and digits.startswith("091"):
        return f"+91{digits[3:]}"
    return f"+{digits}"  # best-effort


def main():
    df = pd.read_csv(CSV_PATH)

    # Group by origin_user_id — one user may have multiple event rows
    grouped: dict[str, dict] = defaultdict(lambda: {
        "user_email": "",
        "user_phone": "",
        "selected_events": [],
    })

    for _, row in df.iterrows():
        uid = str(row.get("origin_user_id", "")).strip()
        if not uid:
            continue

        g = grouped[uid]

        # Take first non-empty email / phone seen for this user
        if not g["user_email"]:
            g["user_email"] = str(row.get("email", "")).strip()

        if not g["user_phone"]:
            g["user_phone"] = normalise_phone(row.get("phoneNumber"))

        event_id = str(row.get("Event Name", "")).strip()
        if event_id and event_id not in g["selected_events"]:
            g["selected_events"].append(event_id)

    # Update MongoDB
    client = MongoClient(MONGODB_URI)
    col = client[MONGODB_DB][COLLECTION_NAME]

    updated   = 0
    not_found = 0

    for uid, fields in grouped.items():
        result = col.update_one(
            {"origin_user_id": uid},
            {"$set": {
                "user_email":      fields["user_email"],
                "user_phone":      fields["user_phone"],
                "selected_events": fields["selected_events"],
            }},
        )
        if result.matched_count == 0:
            print(f"NOT FOUND: {uid}")
            not_found += 1
        else:
            updated += 1

    client.close()

    print("\n===== SUMMARY =====")
    print(f"Updated:   {updated}")
    print(f"Not found: {not_found}")
    print(f"Total UIDs in CSV: {len(grouped)}")


if __name__ == "__main__":
    main()