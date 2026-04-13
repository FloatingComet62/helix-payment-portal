#!/usr/bin/env python3
"""
sync_from_csv.py — Sync actual EaseBuzz fee/settlement data from a downloaded
                   EaseBuzz transactions CSV into MongoDB.

Only updates transactions with status NOTIFIED_SUCCESS.
Also verifies that what our portal charged matches EaseBuzz's recorded amount.

Key CSV learnings:
  - "Settlement Amount" column is authoritative (verified = Amount - SC - GST for all rows)
  - fee = Service Charge + GST (EaseBuzz's own fee breakdown)
  - net_credited = Settlement Amount (use directly, don't recompute)
  - Payment modes in CSV: "UPI", "Credit Card", "Debit Card", "Netbanking", "Mobile Wallet"
  - bounced / dropped / userCancelled / failure rows never have mode=NA on success
  - Settlement ID + Settlement Date available for settled rows

Usage:
  # Dry run — full report, nothing written
  python scripts/sync_from_csv.py --csv Transactions_EB__2026-04-13.csv --dry-run

  # Apply to all NOTIFIED_SUCCESS transactions
  python scripts/sync_from_csv.py --csv Transactions_EB__2026-04-13.csv

  # Verify only — report discrepancies without writing
  python scripts/sync_from_csv.py --csv Transactions_EB__2026-04-13.csv --verify-only

Run from Central_Payment_2/:
  cd Central_Payment_2
  python scripts/sync_from_csv.py --csv Transactions_EB__2026-04-13.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

try:
    import pymongo
    from pymongo import MongoClient
except ImportError:
    sys.exit("ERROR: pymongo not installed.  pip install pymongo")

try:
    from dotenv import dotenv_values
except ImportError:
    sys.exit("ERROR: python-dotenv not installed.  pip install python-dotenv")

# ---------------------------------------------------------------------------
TWOPLACES = Decimal("0.01")
TARGET_DB_STATUS = "NOTIFIED_SUCCESS"
EB_SUCCESS_STATUS = "success"


# ---------------------------------------------------------------------------
# CSV row
# ---------------------------------------------------------------------------

@dataclass
class EBRow:
    easebuzz_id: str           # Easebuzz ID  (e.g. E2604140WZALBN)
    merchant_tx_id: str        # Merchant Transaction ID = our internal_tx_id
    transaction_status: str    # success / bounced / dropped / userCancelled / failure
    amount: Decimal            # Amount charged to customer
    settlement_amount: Decimal # Settlement Amount — what merchant actually receives
    service_charge: Decimal    # EaseBuzz's service charge (before GST)
    gst_on_service: Decimal    # GST on that service charge
    transaction_type: str      # UPI / Credit Card / Debit Card / Netbanking / Mobile Wallet
    tdr: str                   # TDR % string (informational)
    customer_name: str
    customer_phone: str
    date_of_transaction: str
    settlement_id: str         # Settlement batch ID (may be empty)
    settlement_date: str       # Settlement date (may be empty)
    bank_name: str
    bank_transaction_id: str

    @property
    def total_eb_fee(self) -> Decimal:
        """Service charge + GST = total fee EaseBuzz deducted."""
        return (self.service_charge + self.gst_on_service).quantize(TWOPLACES, ROUND_HALF_UP)

    @property
    def computed_net(self) -> Decimal:
        """Amount - total fee. Should equal Settlement Amount (verified in data)."""
        return (self.amount - self.total_eb_fee).quantize(TWOPLACES, ROUND_HALF_UP)

    @property
    def is_success(self) -> bool:
        return self.transaction_status == EB_SUCCESS_STATUS


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class Result:
    tx_id: str
    user_name: str
    eb_row: EBRow | None = None
    db_status: str = ""
    outcome: str = ""        # updated / dry_run / verify_only / skipped / not_in_csv / eb_not_success / amount_mismatch / wrong_db_status
    our_charged: Decimal | None = None
    amount_delta: Decimal | None = None   # our_charged - eb.amount  (0 = perfect match)
    notes: list[str] = field(default_factory=list)
    updated: bool = False


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _dec(val: str) -> Decimal:
    cleaned = val.strip().replace(",", "")
    return Decimal(cleaned).quantize(TWOPLACES, ROUND_HALF_UP) if cleaned else Decimal("0.00")


def load_csv(path: Path) -> list[EBRow]:
    rows: list[EBRow] = []
    skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader, start=2):
            mid = raw.get("Merchant Transaction ID", "").strip()
            if not mid:
                skipped += 1
                continue
            try:
                rows.append(EBRow(
                    easebuzz_id=raw.get("Easebuzz ID", "").strip(),
                    merchant_tx_id=mid,
                    transaction_status=raw.get("Transaction status", "").strip().lower(),
                    amount=_dec(raw.get("Amount", "0")),
                    settlement_amount=_dec(raw.get("Settlement Amount", "0")),
                    service_charge=_dec(raw.get("Service Charge", "0")),
                    gst_on_service=_dec(raw.get("GST", "0")),
                    transaction_type=raw.get("Transaction Type", "").strip(),
                    tdr=raw.get("TDR", "").strip(),
                    customer_name=raw.get("Customer Name", "").strip(),
                    customer_phone=raw.get("Customer Phone", "").strip(),
                    date_of_transaction=raw.get("Date of Transaction", "").strip(),
                    settlement_id=raw.get("Settlement ID", "").strip(),
                    settlement_date=raw.get("Settlement Date", "").strip(),
                    bank_name=raw.get("Bank Name", "").strip(),
                    bank_transaction_id=raw.get("Bank Transaction ID", "").strip(),
                ))
            except Exception as exc:
                print(f"  WARNING: Cannot parse CSV row {i}: {exc}")
                skipped += 1
    if skipped:
        print(f"  (Skipped {skipped} CSV rows with no Merchant Transaction ID or parse error)")
    return rows


# ---------------------------------------------------------------------------
# Our charged amount
# ---------------------------------------------------------------------------

def our_charged_amount(doc: dict) -> Decimal | None:
    try:
        base   = Decimal(str(doc.get("base_amount", "0")))
        coupon = Decimal(str(doc.get("coupon_discount_applied", "0")))
        gst    = Decimal(str(doc.get("gst_amount", "0")))
        return (base - coupon + gst).quantize(TWOPLACES, ROUND_HALF_UP)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def sync(
    eb_rows: list[EBRow],
    col: pymongo.collection.Collection,
    dry_run: bool,
    verify_only: bool,
) -> list[Result]:
    # Build CSV lookup: merchant_tx_id → EBRow
    eb_map: dict[str, EBRow] = {r.merchant_tx_id: r for r in eb_rows}

    # Load all NOTIFIED_SUCCESS docs from DB
    db_docs = {
        doc["internal_tx_id"]: doc
        for doc in col.find({"status": TARGET_DB_STATUS}, {"_id": 0})
    }

    print(f"\n  CSV rows parsed              : {len(eb_rows)}")
    print(f"  CSV rows with status=success : {sum(1 for r in eb_rows if r.is_success)}")
    print(f"  DB {TARGET_DB_STATUS} transactions : {len(db_docs)}")
    print("-" * 70)

    results: list[Result] = []

    for tx_id, doc in sorted(db_docs.items()):
        user_name = doc.get("user_name", "?")
        res = Result(tx_id=tx_id, user_name=user_name, db_status=TARGET_DB_STATUS)
        res.our_charged = our_charged_amount(doc)

        if tx_id not in eb_map:
            res.outcome = "not_in_csv"
            res.notes.append("Not found in EaseBuzz CSV — manual fix needed")
            results.append(res)
            continue

        eb = eb_map[tx_id]
        res.eb_row = eb

        # Must be success on EaseBuzz side
        if not eb.is_success:
            res.outcome = "eb_not_success"
            res.notes.append(f"EaseBuzz status='{eb.transaction_status}' — skipping")
            results.append(res)
            continue

        # Verify our charge = EaseBuzz amount
        if res.our_charged is not None:
            delta = (res.our_charged - eb.amount).quantize(TWOPLACES, ROUND_HALF_UP)
            res.amount_delta = delta
            if abs(delta) > Decimal("0.01"):
                res.notes.append(
                    f"AMOUNT MISMATCH: our portal charged ₹{res.our_charged} "
                    f"but EaseBuzz recorded ₹{eb.amount} (Δ={delta:+})"
                )

        # Verify settlement_amount == amount - sc - gst (should always hold, but log if not)
        if eb.settlement_amount != eb.computed_net:
            res.notes.append(
                f"CSV self-check: Settlement({eb.settlement_amount}) ≠ "
                f"Amount-SC-GST({eb.computed_net}) — using Settlement Amount column"
            )

        # Build DB update
        update: dict = {
            "easebuzz_transaction_fee": f"{eb.total_eb_fee:.2f}",
            "final_net_credited":       f"{eb.settlement_amount:.2f}",
            "payment_mode":             eb.transaction_type,
            "easebuzz_txid":            eb.easebuzz_id,
        }
        if eb.settlement_id:
            update["settlement_id"]   = eb.settlement_id
        if eb.settlement_date:
            update["settlement_date"] = eb.settlement_date

        if dry_run:
            res.outcome = "dry_run"
        elif verify_only:
            res.outcome = "verify_only"
        else:
            col.update_one({"internal_tx_id": tx_id}, {"$set": update})
            res.updated = True
            res.outcome = "updated"

        results.append(res)

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(results: list[Result], dry_run: bool, verify_only: bool) -> None:
    ok      = [r for r in results if r.outcome in ("updated", "dry_run", "verify_only")]
    bad_amt = [r for r in ok if r.amount_delta is not None and abs(r.amount_delta) > Decimal("0.01")]
    not_csv = [r for r in results if r.outcome == "not_in_csv"]
    not_suc = [r for r in results if r.outcome == "eb_not_success"]
    updated = [r for r in results if r.updated]

    # Per-transaction table
    print(f"\n{'TX (first 12)':14} {'User':22} {'Our ₹':>8} {'EB ₹':>8} {'SC+GST':>7} {'Net':>8} {'Mode':>15} {'Δ':>6} {'Result':>10}")
    print("-" * 110)

    for r in results:
        if r.outcome == "not_in_csv":
            print(f"  {r.tx_id[:12]:12}  {r.user_name[:22]:22}  {'— NOT IN CSV —':>50}")
            continue
        if r.outcome == "eb_not_success":
            eb_s = r.eb_row.transaction_status if r.eb_row else "?"
            print(f"  {r.tx_id[:12]:12}  {r.user_name[:22]:22}  {'— EB STATUS: ' + eb_s + ' —':>50}")
            continue

        eb = r.eb_row
        our = f"₹{r.our_charged:.2f}" if r.our_charged else "?"
        eb_amt = f"₹{eb.amount:.2f}"
        fee = f"₹{eb.total_eb_fee:.2f}"
        net = f"₹{eb.settlement_amount:.2f}"
        mode = eb.transaction_type[:15]
        delta = f"{r.amount_delta:+.2f}" if r.amount_delta is not None else "?"
        delta_flag = " ⚠" if bad_amt and r in bad_amt else ""
        result_str = {
            "updated":     "UPDATED",
            "dry_run":     "DRY RUN",
            "verify_only": "VERIFIED",
        }.get(r.outcome, r.outcome)

        print(
            f"  {r.tx_id[:12]:12}  {r.user_name[:22]:22}  "
            f"{our:>8}  {eb_amt:>8}  {fee:>7}  {net:>8}  {mode:>15}  "
            f"{delta:>6}{delta_flag}  {result_str:>8}"
        )
        for note in r.notes:
            print(f"    ⚠  {note}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  {TARGET_DB_STATUS} in DB            : {len(results)}")
    print(f"  Matched in CSV                     : {len(ok)}")
    print(f"  Not found in CSV                   : {len(not_csv)}")
    print(f"  EB status ≠ success                : {len(not_suc)}")
    print(f"  Amount mismatches (investigate!)   : {len(bad_amt)}")

    if dry_run:
        print(f"  Would update                       : {len(ok)}")
        print(f"\n  *** DRY RUN — rerun without --dry-run to apply ***")
    elif verify_only:
        print(f"  *** VERIFY ONLY — no DB writes ***")
    else:
        print(f"  Updated in DB                      : {len(updated)}")

    # Financials
    if ok:
        total_our    = sum(r.our_charged for r in ok if r.our_charged)
        total_eb_amt = sum(r.eb_row.amount for r in ok if r.eb_row)
        total_fee    = sum(r.eb_row.total_eb_fee for r in ok if r.eb_row)
        total_net    = sum(r.eb_row.settlement_amount for r in ok if r.eb_row)
        avg_rate     = (total_fee / total_eb_amt * 100).quantize(TWOPLACES) if total_eb_amt else Decimal("0")

        print(f"\n  FEE ACCURACY ACROSS {len(ok)} MATCHED TRANSACTIONS")
        print(f"  ─────────────────────────────────────────────────")
        print(f"  Our portal charged customers   : ₹{total_our:.2f}")
        print(f"  EaseBuzz recorded (Amount)     : ₹{total_eb_amt:.2f}")
        print(f"  Total EaseBuzz fee (SC + GST)  : ₹{total_fee:.2f}")
        print(f"  Total net credited to fest     : ₹{total_net:.2f}")
        print(f"  Effective EB fee rate          : {avg_rate}%")

        # Mode breakdown
        mode_stats: dict[str, dict] = {}
        for r in ok:
            if not r.eb_row:
                continue
            m = r.eb_row.transaction_type
            if m not in mode_stats:
                mode_stats[m] = {"count": 0, "amount": Decimal("0"), "fee": Decimal("0"), "tdr_set": set()}
            mode_stats[m]["count"] += 1
            mode_stats[m]["amount"] += r.eb_row.amount
            mode_stats[m]["fee"] += r.eb_row.total_eb_fee
            if r.eb_row.tdr:
                mode_stats[m]["tdr_set"].add(r.eb_row.tdr)

        print(f"\n  BREAKDOWN BY PAYMENT MODE")
        print(f"  {'Mode':16} {'Count':>6} {'Total Charged':>14} {'Total Fee':>10} {'Eff. Rate':>10} {'TDR%':>6}")
        print(f"  {'-'*68}")
        for mode, s in sorted(mode_stats.items(), key=lambda x: -x[1]["count"]):
            eff = (s["fee"] / s["amount"] * 100).quantize(TWOPLACES) if s["amount"] else Decimal("0")
            tdr_str = "/".join(sorted(s["tdr_set"])) or "?"
            amt_str = f"Rs{s['amount']:.2f}"
            fee_str = f"Rs{s['fee']:.2f}"
            eff_str = f"{eff}%"
            print(f"  {mode:16} {s['count']:>6} {amt_str:>14} {fee_str:>10} {eff_str:>10} {tdr_str:>6}")

    # Amount mismatches detail
    if bad_amt:
        print(f"\n  ⚠  AMOUNT MISMATCHES — investigate these:")
        for r in bad_amt:
            print(f"     {r.tx_id}")
            print(f"       Our portal charged : ₹{r.our_charged}")
            print(f"       EaseBuzz recorded  : ₹{r.eb_row.amount}")
            print(f"       Delta              : {r.amount_delta:+.2f}")

    # Not-in-CSV detail
    if not_csv:
        print(f"\n  ⚠  NOT IN CSV — need manual update or newer CSV export:")
        for r in not_csv:
            print(f"     {r.tx_id}  user={r.user_name}")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync EaseBuzz CSV → MongoDB (NOTIFIED_SUCCESS only)"
    )
    parser.add_argument("--csv", required=True, help="Path to EaseBuzz transactions CSV")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no DB writes")
    parser.add_argument("--verify-only", action="store_true", help="Verify only, no DB writes")
    parser.add_argument("--env", default=".env", help="Path to .env file (default: .env)")
    args = parser.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        sys.exit(f"ERROR: .env not found at {env_path.resolve()}\nRun from Central_Payment_2/")

    cfg = dotenv_values(env_path)
    mongo_uri = cfg.get("MONGODB_URI") or "mongodb://localhost:27017"
    db_name   = cfg.get("MONGODB_DB_NAME") or "central_payment"

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path.resolve()}")

    print(f"CSV          : {csv_path.resolve()}")
    eb_rows = load_csv(csv_path)

    if args.dry_run:
        print("Mode         : DRY RUN (no DB writes)")
    elif args.verify_only:
        print("Mode         : VERIFY ONLY (no DB writes)")
    else:
        print("Mode         : LIVE — will update NOTIFIED_SUCCESS transactions")

    print(f"MongoDB      : {mongo_uri[:50]}…  db={db_name}")

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to MongoDB: {exc}")

    col = client[db_name]["transactions"]
    results = sync(eb_rows, col, dry_run=args.dry_run, verify_only=args.verify_only)
    report(results, dry_run=args.dry_run, verify_only=args.verify_only)
    client.close()


if __name__ == "__main__":
    main()
