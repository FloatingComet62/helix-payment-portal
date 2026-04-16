"""
One-shot migration: add checked_in = False to every document in the
transactions collection that does not already have the field.

Usage:
    MONGODB_URI=mongodb+srv://... python scripts/init_checkin.py
    # or if .env is present in the project root:
    python scripts/init_checkin.py
"""
import os
import asyncio
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGODB_URI    = os.environ["MONGODB_URI"]
MONGODB_DB     = os.environ.get("MONGODB_DB_NAME", "central_payment")
COLLECTION     = "transactions"

def main():
    client = MongoClient(MONGODB_URI)
    col = client[MONGODB_DB][COLLECTION]

    result = col.update_many(
        {"checked_in": {"$exists": False}},
        {"$set": {"checked_in": False}},
    )

    print(f"Matched:  {result.matched_count}")
    print(f"Modified: {result.modified_count}")
    client.close()

if __name__ == "__main__":
    main()
