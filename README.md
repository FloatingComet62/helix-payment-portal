# Central Payment & Fest Accommodation Portal

A production-ready, multi-tenant payment middleware built with **FastAPI + MongoDB**.
Acts as a centralized gateway between multiple external fest frontends and the **EaseBuzz** payment gateway.
Each fest is an independent tenant with its own pricing config, HMAC secret key, and registered callback URL.

Deployed on **Kubernetes**.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Full System Flow](#full-system-flow)
3. [Transaction State Machine](#transaction-state-machine)
4. [Financial Ledger Logic](#financial-ledger-logic)
5. [Project Structure](#project-structure)
6. [API Reference](#api-reference)
7. [Running Locally](#running-locally)
8. [Environment Variables](#environment-variables)
9. [MongoDB Collections](#mongodb-collections)

---

## Architecture Overview

```
┌─────────────────────┐        ┌──────────────────────────────────┐
│  Fest A Frontend    │        │                                  │
│  (HackTU_2026)      │──────▶ │                                  │
└─────────────────────┘        │                                  │
                                │   Central Payment Portal         │
┌─────────────────────┐        │        (This System)             │        ┌────────────────────┐
│  Fest B Frontend    │──────▶ │                                  │──────▶ │  EaseBuzz Payment  │
│  (TechFest_2026)    │        │   FastAPI + Motor + MongoDB       │        │     Gateway        │
└─────────────────────┘        │                                  │        └────────────────────┘
                                │   • Multi-tenant                 │
┌─────────────────────┐        │   • Decimal-safe financials      │
│  Fest C Frontend    │──────▶ │   • Signed webhook dispatch      │
│  (...)              │        │   • Retry with backoff           │
└─────────────────────┘        │                                  │
                                └──────────────────────────────────┘
                                     │              │
                    ┌────────────────┘              └────────────────┐
                    ▼                                                ▼
         Fest A Backend                                   Fest B Backend
         (receives signed callback)                       (receives signed callback)
```

---

## Full System Flow

### Phase 1 — Admin Onboarding (one-time per fest)

```
Admin
  │
  │  POST /admin/fests
  │  {
  │    "fest_id":        "HackTU_2026",
  │    "legal_name":     "HackTU by TU",
  │    "callback_url":   "https://hacktu.in/payment/callback",
  │    "authorized_url": "https://hacktu.in",
  │    "pricing": {
  │      "mode":        "bundled",
  │      "gst_percent": "18.00",
  │      "addon": {
  │        "accommodation_per_day":   "500.00",
  │        "food_per_day":            "200.00",
  │        "bundled_discount_percent": "5.00"
  │      }
  │    }
  │  }
  ▼
[admin.py] ── verify X-Admin-Key ──▶ [fest_service.py]
                                           │
                                           │  secrets.token_hex(32)
                                           │  generates 64-char secret_key
                                           ▼
                                     MongoDB  fests  collection
                                     {
                                       fest_id,
                                       legal_name,
                                       secret_key,     ◀── never returned to API callers
                                       callback_url,
                                       authorized_url,
                                       pricing,
                                       created_at
                                     }
```

---

### Phase 2 — Payment Initiation

```
External Fest Frontend
  │
  │  Redirects user:
  │  https://portal.com/?fest_id=HackTU_2026&origin_user_id=user_789
  ▼
Portal Frontend
  │  Collects: user_name, user_state, user_district, accommodation_days, food_days
  │
  │  POST /payment/initiate
  │  {
  │    "fest_id":        "HackTU_2026",
  │    "origin_user_id": "user_789",
  │    "user_name":      "Arjun Sharma",
  │    "user_state":     "Punjab",
  │    "user_district":  "Ludhiana",
  │    "booking": { "accommodation_days": 2, "food_days": 2 },
  │    "coupon_code":    "HACK100"       ← optional
  │  }
  ▼
[payment.py router]
  │
  ├──▶ Load FestDB from MongoDB (includes pricing + secret_key)
  │
  └──▶ [payment_service.py] ── ALL MATH IN Decimal, ZERO FLOATS ──
            │
            │  base_amount = accommodation(500×2) + food(200×2) = 1400.00
            │              → bundle discount 5%  = 1330.00
            │
            │  if coupon_code:
            │    [coupon_service] ──▶ EaseBuzz Coupon API
            │    coupon_discount = 100.00
            │
            │  taxable_amount    = 1330.00 - 100.00  = 1230.00
            │  gst_amount        = 1230.00 × 18/100  =  221.40   ◀── BEFORE gateway
            │  charge_amount     = 1230.00 + 221.40  = 1451.40   ← EaseBuzz charges user this
            │  easebuzz_fee      = 1451.40 × 2/100   =   29.03
            │  final_net_credited= 1451.40 - 29.03   = 1422.37   ← fest receives this
            │
            │  Create TransactionDB in MongoDB
            │  { internal_tx_id: UUID4, status: PENDING,
            │    base_amount: "1330.00", gst_amount: "221.40",
            │    coupon_discount_applied: "100.00",
            │    easebuzz_transaction_fee: "29.03",
            │    final_net_credited: "1422.37",
            │    initiated_at: ISO8601, ... }
            │
            │  generate_easebuzz_hash()
            │  sha512(key|txnid|1451.40|productinfo|firstname|email|udf1...|salt)
            │
            ▼
       PaymentInitiateResponse
       { easebuzz_payment_url, key, txnid, hash, amount: "1451.40", ... }
            │
            ▼
  Portal Frontend POSTs to EaseBuzz URL
  User completes payment on EaseBuzz hosted page
```

---

### Phase 3 — EaseBuzz S2S Webhook

```
EaseBuzz Servers
  │
  │  POST /webhook/easebuzz
  │  (application/x-www-form-urlencoded)
  │  { txnid, status, hash, easepayid, amount, firstname, email, ... }
  ▼
[webhook.py router]
  │
  ├──▶ verify_easebuzz_response_hash()
  │    sha512(salt|status||udf5|...|udf1|email|firstname|productinfo|amount|txnid|key)
  │         ✗ FAIL ──▶ HTTP 400  (tampered payload, stop processing)
  │         ✓ PASS ──▶ continue
  │
  ├──▶ Look up transaction by txnid (= internal_tx_id)
  │
  ├── status == "success":
  │     MongoDB update: status=SUCCESS, easebuzz_txid, completed_at=now
  │     BackgroundTask ──▶ dispatch_to_fest()    ◀── non-blocking, returns 200 immediately
  │
  └── status != "success":
        MongoDB update: status=FAILED, completed_at=now
  │
  ▼
  {"status": "ok"}   ← HTTP 200 returned immediately (EaseBuzz requires this)
```

---

### Phase 4 — Signed Callback to Fest Backend (Background Task)

```
[webhook_dispatch.py]  (runs as BackgroundTask — does not block the webhook response)
  │
  ├──▶ Build payload:
  │    {
  │      internal_tx_id, easebuzz_txid, fest_id, origin_user_id,
  │      user_name, user_state, user_district,
  │      booking: { accommodation_days, food_days, amounts },
  │      financials: { base, gst, fee, discount, net_credited },
  │      payment_timestamp, status: "SUCCESS"
  │    }
  │
  ├──▶ json.dumps(sort_keys=True)    ◀── deterministic serialisation for HMAC
  │
  ├──▶ HMAC-SHA256(payload_str, fest.secret_key) → X-Signature header
  │
  ├── Attempt 1: POST to fest.callback_url
  │       ✓ HTTP 200  ──▶  status = NOTIFIED_SUCCESS   ──▶  DONE
  │       ✗ non-200 / timeout / exception
  │           └──▶ log to notification_logs[], wait 2s
  │
  ├── Attempt 2: POST to fest.callback_url
  │       ✓ HTTP 200  ──▶  status = NOTIFIED_SUCCESS   ──▶  DONE
  │       ✗ failure
  │           └──▶ log to notification_logs[], wait 4s
  │
  └── Attempt 3: POST to fest.callback_url
          ✓ HTTP 200  ──▶  status = NOTIFIED_SUCCESS   ──▶  DONE
          ✗ all exhausted
              └──▶ status = NOTIFIED_FAILURE
                   (all attempts logged in notification_logs[])
```

---

### Phase 5 — Finance Office Balance Sheet

```
Admin
  │
  │  GET /admin/balance-sheet
  │  X-Admin-Key: <key>
  ▼
[admin.py] ──▶ [balance_sheet.py]
                    │
                    ▼
          MongoDB Aggregation Pipeline
          on  transactions  collection
                    │
          ┌─────────────────────────────────────────────┐
          │  $match: status IN                          │
          │    [SUCCESS, NOTIFIED_SUCCESS,              │
          │     NOTIFIED_FAILURE]                       │
          ├─────────────────────────────────────────────┤
          │  $addFields: $toDecimal on all amounts      │
          │    (128-bit exact — not IEEE-754 float)     │
          ├─────────────────────────────────────────────┤
          │  $group by fest_id:                         │
          │    total_transactions    = $sum 1           │
          │    total_base_revenue    = $sum base_dec    │
          │    total_gst_collected   = $sum gst_dec     │
          │    total_easebuzz_fees   = $sum fee_dec     │
          │    total_discounts_given = $sum discount_dec│
          │    total_net_creditable  = $sum net_dec     │
          ├─────────────────────────────────────────────┤
          │  $addFields: $toString (back to strings)    │
          ├─────────────────────────────────────────────┤
          │  $sort: fest_id ASC                         │
          └─────────────────────────────────────────────┘
                    │
                    ▼
          [
            {
              "fest_id":               "HackTU_2026",
              "total_transactions":    142,
              "total_base_revenue":    "188600.00",
              "total_gst_collected":   "33948.00",
              "total_easebuzz_fees":   "4450.96",
              "total_discounts_given": "14200.00",
              "total_net_creditable":  "203097.04"
            },
            ...
          ]
```

---

## Transaction State Machine

```
                         ┌─────────┐
    POST /payment/initiate│         │
    ────────────────────▶ │ PENDING │
                          │         │
                          └────┬────┘
                               │
                    EaseBuzz S2S webhook received
                               │
                  ┌────────────┴────────────┐
                  │                         │
            status="success"          status!="success"
                  │                         │
                  ▼                         ▼
            ┌─────────┐              ┌────────┐
            │ SUCCESS │              │ FAILED │
            └────┬────┘              └────────┘
                 │
         BackgroundTask fires
         dispatch_to_fest()
                 │
       ┌─────────┴──────────┐
       │                    │
  ≥1 attempt               all 3 attempts
  returned 200             failed/timed out
       │                    │
       ▼                    ▼
┌──────────────────┐  ┌──────────────────┐
│ NOTIFIED_SUCCESS │  │ NOTIFIED_FAILURE │
└──────────────────┘  └──────────────────┘
```

---

## Financial Ledger Logic

```
base_amount
  = pricing calculation (fixed / per_day / bundled)
  = e.g. (acc_per_day × acc_days) + (food_per_day × food_days) − bundle_discount

coupon_discount_applied
  = validated via EaseBuzz Coupon API (0.00 if no coupon)

taxable_amount  = base_amount − coupon_discount_applied

gst_amount      = taxable_amount × gst_percent / 100
                                  ↑
                    computed HERE, before EaseBuzz handoff

charge_amount   = taxable_amount + gst_amount
                  ↑ this is what EaseBuzz charges the user

easebuzz_fee    = charge_amount × EASEBUZZ_FEE_PERCENT / 100

final_net_credited = charge_amount − easebuzz_fee
                     ↑ this is what the fest receives
```

All arithmetic uses Python `decimal.Decimal` with `ROUND_HALF_UP`.
All values stored in MongoDB as **strings** (`"1500.00"`), never as floats.
Balance sheet aggregation uses MongoDB `$toDecimal` to sum — never Python `float()`.

---

## Project Structure

```
app/
├── main.py                    FastAPI app init, lifespan, router registration
├── core/
│   ├── config.py              Settings(BaseSettings), @lru_cache get_settings()
│   ├── database.py            Motor async client, index creation, collection helpers
│   ├── security.py            EaseBuzz sha512 (forward + reverse), HMAC-SHA256
│   └── exceptions.py          Typed HTTPException subclasses
├── models/
│   ├── fest.py                FestCreate, FestDB, FestPublic, PricingConfig, AddonPricing
│   └── transaction.py         TransactionDB, TxStatus, BookingDetail, NotificationLog
├── services/
│   ├── fest_service.py        Fest CRUD
│   ├── payment_service.py     Financial math + EaseBuzz hash generation
│   ├── coupon_service.py      EaseBuzz coupon API validation
│   └── webhook_dispatch.py    HMAC-signed POST + exponential backoff retry
├── routers/
│   ├── admin.py               /admin/* endpoints
│   ├── payment.py             /payment/initiate
│   └── webhook.py             /webhook/easebuzz
└── aggregations/
    └── balance_sheet.py       MongoDB aggregation pipeline
```

---

## API Reference

### Admin Routes
> All require `X-Admin-Key: <key>` header.

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/fests` | Onboard a new fest |
| `GET` | `/admin/fests` | List all fests |
| `GET` | `/admin/fests/{fest_id}` | Get a single fest |
| `PATCH` | `/admin/fests/{fest_id}` | Update pricing config |
| `GET` | `/admin/fests/{fest_id}/secret` | Reveal the fest's HMAC secret_key |
| `GET` | `/admin/balance-sheet` | Finance office per-fest summary |

### Payment Routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/payment/initiate` | Initiate payment — returns EaseBuzz form fields |

### Webhook Routes

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook/easebuzz` | EaseBuzz S2S payment confirmation |

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |

---

## Running Locally

```bash
# 1. Clone and set up env
cp .env.example .env
# Fill in EASEBUZZ_KEY, EASEBUZZ_SALT, ADMIN_API_KEY, PORTAL_CALLBACK_URL

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start server
uvicorn app.main:app --reload

# Interactive docs
open http://localhost:8000/docs
```

### Docker

```bash
docker build -t central-payment .
docker run -p 8000:8000 --env-file .env central-payment
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MONGODB_URI` | Yes | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB_NAME` | No | `central_payment` | Database name |
| `EASEBUZZ_KEY` | Yes | — | EaseBuzz merchant key |
| `EASEBUZZ_SALT` | Yes | — | EaseBuzz salt for hash generation |
| `EASEBUZZ_ENV` | No | `test` | `test` or `prod` |
| `PORTAL_CALLBACK_URL` | Yes | — | Our server's URL — EaseBuzz POSTs here |
| `ADMIN_API_KEY` | Yes | — | Shared secret for `X-Admin-Key` header |
| `EASEBUZZ_FEE_PERCENT` | No | `2.00` | Platform fee percentage |

---

## MongoDB Collections

### `fests`
Indexes: `fest_id` (unique)

### `transactions`
Indexes: `internal_tx_id` (unique), `easebuzz_txid`, `fest_id`, `status`

All monetary fields (`base_amount`, `gst_amount`, `easebuzz_transaction_fee`, `coupon_discount_applied`, `final_net_credited`) are stored as **strings** to preserve Decimal precision across the application boundary.

---

## Critical Rules — Never Break These

1. **No floats for money.** All arithmetic uses `decimal.Decimal` with `ROUND_HALF_UP`.
2. **GST before gateway.** `gst_amount` is computed before the EaseBuzz hash is generated.
3. **Amounts as strings in MongoDB.** Parse back with `Decimal()`, not `float()`.
4. **Balance sheet via `$toDecimal`.** Never sum money fields in Python after reading from Mongo.
5. **HMAC on raw bytes.** The fest callback signature is computed over `sort_keys=True` JSON — verify against raw body, not re-serialised.
6. **`secret_key` never in API responses.** Only exposed via the dedicated `/admin/fests/{fest_id}/secret` endpoint.
