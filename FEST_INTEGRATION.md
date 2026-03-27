# Frontend Integration Guide

This document is written for the **external fest frontend/backend teams** integrating with the Central Payment Portal. Read this top to bottom before writing a single line of code.

---

## Table of Contents

1. [How the System Works — 30 Second Overview](#1-how-the-system-works)
2. [Glossary](#2-glossary)
3. [Step 1 — Get Onboarded (Admin does this)](#3-step-1--get-onboarded)
4. [Step 2 — Redirect Your User to the Portal](#4-step-2--redirect-your-user-to-the-portal)
5. [Step 3 — Portal Collects Info and Initiates Payment](#5-step-3--portal-collects-info-and-initiates-payment)
6. [Step 4 — Implement Your Callback Endpoint](#6-step-4--implement-your-callback-endpoint)
7. [Verifying the Signature (Critical Security Step)](#7-verifying-the-signature)
8. [Callback Payload Reference](#8-callback-payload-reference)
9. [Transaction Status Reference](#9-transaction-status-reference)
10. [Error Scenarios & What To Do](#10-error-scenarios)
11. [Testing Checklist](#11-testing-checklist)
12. [Code Samples](#12-code-samples)

---

## 1. How the System Works

```
Your Fest Frontend
      │
      │  1. Redirect user to Portal (with fest_id + origin_user_id)
      ▼
Central Payment Portal  (this system)
      │
      │  2. Collects user details (name, state, district)
      │  3. Applies coupon if provided
      │  4. Calculates amounts (base + GST + fees)
      │  5. Checks accommodation seat availability (if fest has a cap)
      │  6. Redirects user to EaseBuzz payment page
      ▼
EaseBuzz Payment Gateway
      │
      │  7. User pays (card / UPI / netbanking)
      │  8. EaseBuzz fires S2S webhook to Central Portal
      ▼
Central Payment Portal
      │
      │  9. Verifies EaseBuzz webhook hash
      │  10. Updates transaction → SUCCESS or FAILED
      │  11. Signs payload with YOUR secret_key
      │  12. POSTs to YOUR callback_url  (for both SUCCESS and FAILED)
      ▼
Your Fest Backend  ◀── You implement this endpoint
      │
      │  13. Verify the signature
      │  14. Check status field — SUCCESS or FAILED
      │  15. On SUCCESS: mark user as paid, assign room/food slot
      │  16. On FAILED: update your UI / notify the user
      │  17. Return HTTP 200
```

**You are responsible for steps 1 and 13–17. Everything else is handled by this portal.**

---

## 2. Glossary

| Term | Meaning |
|---|---|
| `fest_id` | Unique identifier for your fest, e.g. `"HackTU_2026"`. Assigned by the admin. |
| `origin_user_id` | The user's ID **in your own system**. We carry it through the entire flow and return it to you so you know exactly who paid. |
| `internal_tx_id` | Our UUID for the transaction. Use this as the canonical payment reference. |
| `easebuzz_txid` | EaseBuzz's own transaction ID (also called `easepayid`). Keep this for payment disputes. |
| `secret_key` | A 64-character hex token unique to your fest. Used to sign our callback payloads. Keep it secret — treat it like a private key. |
| `callback_url` | The HTTPS endpoint on **your backend** where we POST the signed payment confirmation (for both SUCCESS and FAILED). |
| `X-Signature` | The HMAC-SHA256 signature header we attach to every callback POST. You must verify this before trusting the payload. |

---

## 3. Step 1 — Get Onboarded

Contact the portal admin. They will:

1. Create your fest via `POST /admin/fests` with your:
   - `fest_id` (you choose the slug, e.g. `TechFest_2026`)
   - `legal_name`
   - `callback_url` — your backend endpoint (must be HTTPS in production)
   - `authorized_url` — your frontend origin
   - `pricing` config (see Pricing Modes below)
   - `total_accommodation_seats` (optional) — if your fest has a fixed number of accommodation slots, provide this. The system will enforce it atomically and return HTTP 409 when seats run out.
   - `event_dates` (optional) — human-readable date string shown to users, e.g. `"March 28–30, 2026"`
   - `short_description` (optional) — one-line tagline displayed on the portal
   - `long_description` (optional) — full about text displayed on the portal
   - `available_options` (optional, default: `["accommodation", "food", "bundled"]`) — controls which booking options are shown to users. Set to `["food"]` if your fest only offers food, or `["accommodation"]` for accommodation-only. The portal UI will hide unavailable options.

2. The system auto-generates a `secret_key` for you. The admin retrieves it via `GET /admin/fests/{fest_id}/secret` and **shares it with you securely** (do not send over email/Slack in plain text).

3. You store `secret_key` in your backend environment — **never expose it to your frontend**.

### Pricing Modes

The `pricing` object you provide during onboarding controls how amounts are calculated.

| Mode | Key fields | Logic |
|---|---|---|
| `fixed` | `accommodation_price`, `food_price`, `bundled_price`, `gst_percent` | Flat fee per option. `accommodation_price` if only accommodation booked, `food_price` if only food, `bundled_price` if both. |
| `per_day` | `addon.accommodation_per_day`, `addon.food_per_day`, `gst_percent` | Per-item daily rate. Accommodation total = `accommodation_per_day × accommodation_days`; food total = `food_per_day × food_days`. |
| `bundled` | `addon.accommodation_per_day`, `addon.food_per_day`, `addon.bundled_discount_percent`, `gst_percent` | Same per-day calculation as `per_day`, but a percentage discount is applied to the subtotal when the user books both accommodation and food. |

`gst_percent` is always required (e.g. `18.00` for 18% GST). All monetary values use `Decimal` precision — pass them as strings when using the API (e.g. `"500.00"`).

### What you receive from the admin
```
fest_id:     "TechFest_2026"
secret_key:  "a3f8c1d2e4b5...64 hex chars"    ← store in your backend .env
callback_url you registered: "https://your-api.com/payment/callback"
```

---

## 4. Step 2 — Redirect Your User to the Portal

When a user on your fest website wants to register for accommodation/food:

**Redirect them to the portal's payment initiation page**, passing two query parameters:

```
https://portal.yourdomain.com/?fest_id=TechFest_2026&origin_user_id=YOUR_USER_ID
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `fest_id` | string | Yes | Your fest's identifier |
| `origin_user_id` | string | Yes | The user's ID in your system (any unique string — UUID, email, int ID) |

**Important:** Before redirecting, save the `origin_user_id` → pending-payment mapping on your side. You'll need it when the callback arrives.

---

## 5. Step 3 — Portal Collects Info and Initiates Payment

The portal handles this entirely. Your user will:

1. Fill in their legal name, state, and district on our portal UI
2. Optionally apply a coupon code
3. Review the amount breakdown (base + GST)
4. Get redirected to the EaseBuzz hosted payment page
5. Complete payment (card / UPI / netbanking)

**You don't build any of this.** Once EaseBuzz confirms (or rejects) the payment, we notify you automatically.

---

## 6. Step 4 — Implement Your Callback Endpoint

This is the most important part. You must build an HTTPS POST endpoint at the `callback_url` you registered.

**We call this endpoint for both successful AND failed payments.** Always check the `status` field.

### What we send you

- **Method:** `POST`
- **Content-Type:** `application/json`
- **Headers:**
  ```
  X-Signature: <hmac-sha256-hex-string>
  X-Fest-ID:   TechFest_2026
  Content-Type: application/json
  ```
- **Body:** See [Callback Payload Reference](#8-callback-payload-reference)

### What you must do

```
1. Read the raw request body as bytes (before JSON parsing)
2. Read the X-Signature header
3. Compute HMAC-SHA256(raw_body, your_secret_key)
4. Compare your computed signature to X-Signature using a timing-safe comparison
   ✗ Mismatch → return HTTP 400 or 403, do NOT process
   ✓ Match    → parse the JSON body and process

5. Check the status field:
   "SUCCESS" → mark the user (identified by origin_user_id) as paid
   "FAILED"  → update your system accordingly (user was not charged)

6. Use internal_tx_id as a deduplication key (idempotency check first)
7. Return HTTP 200
```

### Retry behaviour

We retry up to **3 times** with exponential backoff (2s, then 4s) if your endpoint returns non-200 or is unreachable. Your endpoint must be **idempotent** — if we deliver the same `internal_tx_id` twice, do not double-book the user. Use `internal_tx_id` as a deduplication key.

### Response requirement

Return **HTTP 200** as soon as you've accepted the payload. Do your async booking logic after. If you return anything other than 200, we will retry.

---

## 7. Verifying the Signature

This step is non-negotiable. Without it, anyone on the internet can POST fake payment confirmations to your endpoint.

### Algorithm

```
signature = HMAC-SHA256(raw_request_body, secret_key)
```

- `raw_request_body` — the **raw bytes** of the JSON body, exactly as received (do not parse and re-serialize)
- `secret_key` — your 64-char hex secret key as a UTF-8 string
- Compare using a constant-time comparison function to prevent timing attacks

### Why raw bytes matter

We serialize the payload with `sort_keys=True` and no extra spaces before signing. If you parse the JSON and re-serialize it yourself, key ordering or spacing differences will break the HMAC. Always verify against the **raw body**.

---

## 8. Callback Payload Reference

### SUCCESS callback

```json
{
  "internal_tx_id":    "550e8400-e29b-41d4-a716-446655440000",
  "easebuzz_txid":     "EZPAY20260327ABC123",
  "fest_id":           "TechFest_2026",
  "origin_user_id":    "user_789",

  "user_name":         "Arjun Sharma",
  "user_state":        "Punjab",
  "user_district":     "Ludhiana",

  "booking": {
    "accommodation_days": 2,
    "food_days":          2,
    "accommodation_amount": "1000.00",
    "food_amount":          "400.00"
  },

  "financials": {
    "base_amount":              "1400.00",
    "coupon_discount_applied":  "200.00",
    "gst_amount":               "216.00",
    "easebuzz_transaction_fee": "28.32",
    "final_net_credited":       "1387.68"
  },

  "payment_timestamp": "2026-03-27T14:32:10.123456+00:00",
  "status":            "SUCCESS"
}
```

### FAILED callback

```json
{
  "internal_tx_id":    "550e8400-e29b-41d4-a716-446655440000",
  "easebuzz_txid":     "",
  "fest_id":           "TechFest_2026",
  "origin_user_id":    "user_789",

  "user_name":         "Arjun Sharma",
  "user_state":        "Punjab",
  "user_district":     "Ludhiana",

  "booking": {
    "accommodation_days": 2,
    "food_days":          2,
    "accommodation_amount": "1000.00",
    "food_amount":          "400.00"
  },

  "financials": {
    "base_amount":              "1400.00",
    "coupon_discount_applied":  "200.00",
    "gst_amount":               "216.00",
    "easebuzz_transaction_fee": "0.00",
    "final_net_credited":       "0.00"
  },

  "payment_timestamp": "2026-03-27T14:34:55.000000+00:00",
  "status":            "FAILED"
}
```

> On FAILED payments, the user is **not charged**. `easebuzz_transaction_fee` and `final_net_credited` are `"0.00"`. Any reserved accommodation seat is automatically released.

### Field Reference

#### Top Level

| Field | Type | Description |
|---|---|---|
| `internal_tx_id` | string (UUID4) | Our canonical transaction ID. **Use this as dedup key.** |
| `easebuzz_txid` | string | EaseBuzz's own payment ID. Empty string on FAILED. Use for dispute resolution with EaseBuzz. |
| `fest_id` | string | Your fest identifier. Always matches your registration. |
| `origin_user_id` | string | The user ID you passed when redirecting — identifies the user in your system. |
| `user_name` | string | Legal name the user filled in on our portal. |
| `user_state` | string | State the user selected. |
| `user_district` | string | District the user selected. |
| `payment_timestamp` | string (ISO 8601) | UTC timestamp of when EaseBuzz confirmed or rejected the payment. |
| `status` | string | `"SUCCESS"` or `"FAILED"`. Always check this before acting on the payload. |

#### `booking` Object

| Field | Type | Description |
|---|---|---|
| `accommodation_days` | int | Number of accommodation days booked (0 if none). |
| `food_days` | int | Number of food days booked (0 if none). |
| `accommodation_amount` | string | Subtotal for accommodation in INR, 2 decimal places, e.g. `"1000.00"`. |
| `food_amount` | string | Subtotal for food in INR, 2 decimal places, e.g. `"400.00"`. |

#### `financials` Object

| Field | Type | Description |
|---|---|---|
| `base_amount` | string | Raw booking cost before discounts/GST. |
| `coupon_discount_applied` | string | Discount from coupon (`"0.00"` if no coupon used). |
| `gst_amount` | string | GST charged on `(base - discount)`. |
| `easebuzz_transaction_fee` | string | Platform fee deducted (`"0.00"` on FAILED). |
| `final_net_credited` | string | What your fest actually receives (`"0.00"` on FAILED). |

> All monetary values are strings with exactly 2 decimal places. Parse them with `Decimal`, not `float`.

---

## 9. Transaction Status Reference

```
PENDING          → Payment initiated, user on EaseBuzz page
SUCCESS          → EaseBuzz confirmed payment received
FAILED           → Payment failed or was cancelled by user
NOTIFIED_SUCCESS → We successfully delivered the callback to your endpoint
NOTIFIED_FAILURE → All 3 callback delivery attempts to your endpoint failed
REFUNDED         → Admin initiated a full refund via EaseBuzz (rare, emergency only)
```

If you see `NOTIFIED_FAILURE` in our logs, check your `callback_url` is publicly reachable and returns HTTP 200.

---

## 10. Error Scenarios

| Scenario | What happens | What you should do |
|---|---|---|
| User cancels payment on EaseBuzz | Transaction → `FAILED`. Callback sent to you with `"status": "FAILED"`. | Handle the FAILED case in your callback endpoint — update any pending state. |
| Accommodation seats full | Payment initiation returns HTTP 409 to the user before payment starts. No callback sent. | User sees "No seats available" — redirect them appropriately. |
| Your callback returns non-200 | We retry up to 3 times (2s, 4s apart). | Ensure your endpoint is stable and idempotent. |
| Signature mismatch on your side | Reject the request, do not process. | Check you're using raw bytes, correct secret_key. |
| Duplicate delivery (retry) | You receive the same `internal_tx_id` again. | Idempotency check: if already processed, return 200 immediately. |
| Your endpoint is down | After 3 retries, status → `NOTIFIED_FAILURE`. | Contact portal admin to manually replay the callback. |
| User applies invalid coupon | Portal returns an error to the user before payment. You are not affected. | Nothing required. |

---

## 11. Testing Checklist

Before going live, verify each of the following:

- [ ] You receive a POST to your `callback_url` after a test payment succeeds
- [ ] You receive a POST to your `callback_url` after a test payment fails/is cancelled
- [ ] `X-Signature` header is present on every callback (both SUCCESS and FAILED)
- [ ] Signature verification passes with your `secret_key`
- [ ] Your callback handles `"status": "SUCCESS"` — marks user as paid
- [ ] Your callback handles `"status": "FAILED"` — does NOT mark user as paid
- [ ] `origin_user_id` in the callback matches what you sent in the redirect
- [ ] `internal_tx_id` is stored in your DB for deduplication
- [ ] Sending the same callback twice does NOT double-book the user
- [ ] Your endpoint returns HTTP 200 within 10 seconds
- [ ] Your endpoint works over HTTPS (HTTP will be rejected in production)
- [ ] Monetary amounts are parsed as `Decimal`, not `float`
- [ ] You handle `accommodation_days: 0` or `food_days: 0` gracefully
- [ ] (If your fest has seat limits) Attempting a 4th booking when only 3 seats exist returns 409 to the user

---

## 12. Code Samples

### Verifying the Signature — Python

```python
import hashlib
import hmac
import json
from fastapi import Request, HTTPException

SECRET_KEY = "your_64_char_hex_secret_key"  # from your .env

async def payment_callback(request: Request):
    raw_body = await request.body()               # raw bytes — do NOT decode yet
    received_sig = request.headers.get("X-Signature", "")

    computed_sig = hmac.new(
        SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed_sig, received_sig):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(raw_body)
    internal_tx_id = payload["internal_tx_id"]
    origin_user_id = payload["origin_user_id"]
    status = payload["status"]  # "SUCCESS" or "FAILED"

    # Idempotency check
    if already_processed(internal_tx_id):
        return {"status": "already_processed"}

    if status == "SUCCESS":
        mark_user_paid(origin_user_id, payload)
    else:
        # Payment failed — update any pending state on your side
        mark_payment_failed(origin_user_id, internal_tx_id)

    return {"status": "ok"}
```

### Verifying the Signature — Node.js (Express)

```javascript
const crypto = require("crypto");

const SECRET_KEY = process.env.FEST_SECRET_KEY;

app.post("/payment/callback", express.raw({ type: "application/json" }), (req, res) => {
    const rawBody = req.body;                          // Buffer — must use express.raw()
    const receivedSig = req.headers["x-signature"] || "";

    const computedSig = crypto
        .createHmac("sha256", SECRET_KEY)
        .update(rawBody)
        .digest("hex");

    const sigValid = crypto.timingSafeEqual(
        Buffer.from(computedSig, "hex"),
        Buffer.from(receivedSig, "hex"),
    );

    if (!sigValid) {
        return res.status(400).json({ error: "Invalid signature" });
    }

    const payload = JSON.parse(rawBody.toString("utf8"));
    const { internal_tx_id, origin_user_id, status, booking } = payload;

    // Idempotency check
    if (db.transactionExists(internal_tx_id)) {
        return res.status(200).json({ status: "already_processed" });
    }

    if (status === "SUCCESS") {
        db.markUserPaid(origin_user_id, payload);
    } else {
        // Payment failed — update pending state
        db.markPaymentFailed(origin_user_id, internal_tx_id);
    }

    res.status(200).json({ status: "ok" });
});
```

### Verifying the Signature — Go

```go
package main

import (
    "crypto/hmac"
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "io"
    "net/http"
    "os"
)

var secretKey = []byte(os.Getenv("FEST_SECRET_KEY"))

func paymentCallbackHandler(w http.ResponseWriter, r *http.Request) {
    rawBody, err := io.ReadAll(r.Body)
    if err != nil {
        http.Error(w, "cannot read body", http.StatusBadRequest)
        return
    }

    receivedSig := r.Header.Get("X-Signature")

    mac := hmac.New(sha256.New, secretKey)
    mac.Write(rawBody)
    computedSig := hex.EncodeToString(mac.Sum(nil))

    receivedBytes, _ := hex.DecodeString(receivedSig)
    computedBytes, _ := hex.DecodeString(computedSig)

    if !hmac.Equal(computedBytes, receivedBytes) {
        http.Error(w, "invalid signature", http.StatusBadRequest)
        return
    }

    var payload map[string]interface{}
    json.Unmarshal(rawBody, &payload)

    status := payload["status"].(string) // "SUCCESS" or "FAILED"

    // Idempotency check + process booking based on status...
    w.WriteHeader(http.StatusOK)
    w.Write([]byte(`{"status":"ok"}`))
}
```

---

## Contact

For onboarding, secret key rotation, or production issues — contact the portal admin team.
For payment disputes (wrong amount, double charge) — use `easebuzz_txid` when contacting EaseBuzz support.
