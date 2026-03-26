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
9. [Understanding the source Field (Direct Portal Visits)](#9-understanding-the-source-field)
10. [Transaction Status Reference](#10-transaction-status-reference)
11. [Error Scenarios & What To Do](#11-error-scenarios)
12. [Testing Checklist](#12-testing-checklist)
13. [Code Samples](#13-code-samples)

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
      │  5. Redirects user to EaseBuzz payment page
      ▼
EaseBuzz Payment Gateway
      │
      │  6. User pays (card / UPI / netbanking)
      │  7. EaseBuzz fires S2S webhook to Central Portal
      ▼
Central Payment Portal
      │
      │  8. Verifies EaseBuzz webhook hash
      │  9. Updates transaction → SUCCESS
      │  10. Signs payload with YOUR secret_key
      │  11. POSTs to YOUR callback_url
      ▼
Your Fest Backend  ◀── You implement this endpoint
      │
      │  12. Verify the signature
      │  13. Mark the user as paid, assign room/food slot
      │  14. Return HTTP 200
```

**You are responsible for steps 1 and 12–14. Everything else is handled by this portal.**

---

## 2. Glossary

| Term | Meaning |
|---|---|
| `fest_id` | Unique identifier for your fest, e.g. `"HackTU_2026"`. Assigned by the admin. |
| `origin_user_id` | The user's ID **in your own system**. We carry it through the entire flow and return it to you so you know exactly who paid. |
| `internal_tx_id` | Our UUID for the transaction. Use this as the canonical payment reference. |
| `easebuzz_txid` | EaseBuzz's own transaction ID (also called `easepayid`). Keep this for payment disputes. |
| `secret_key` | A 64-character hex token unique to your fest. Used to sign our callback payloads. Keep it secret — treat it like a private key. |
| `callback_url` | The HTTPS endpoint on **your backend** where we POST the signed payment confirmation. |
| `X-Signature` | The HMAC-SHA256 signature header we attach to every callback POST. You must verify this before trusting the payload. |

---

## 3. Step 1 — Get Onboarded

Contact the portal admin. They will:

1. Create your fest via `POST /admin/fests` with your:
   - `fest_id` (you choose the slug, e.g. `TechFest_2026`)
   - `legal_name`
   - `callback_url` — your backend endpoint (must be HTTPS in production)
   - `authorized_url` — your frontend origin
   - `pricing` config (fixed / per-day / bundled with add-ons)

2. The system auto-generates a `secret_key` for you. The admin retrieves it via `GET /admin/fests/{fest_id}/secret` and **shares it with you securely** (do not send over email/Slack in plain text).

3. You store `secret_key` in your backend environment — **never expose it to your frontend**.

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

**You don't build any of this.** Once EaseBuzz confirms the payment, we notify you automatically.

---

## 6. Step 4 — Implement Your Callback Endpoint

This is the most important part. You must build an HTTPS POST endpoint at the `callback_url` you registered.

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
   ✓ Match    → parse the JSON body and process the booking
5. Mark the user (identified by origin_user_id) as paid
6. Return HTTP 200
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
  "status":            "SUCCESS",
  "source":            "fest_redirect",
  "miscellaneous":     false
}
```

### Field Reference

#### Top Level

| Field | Type | Description |
|---|---|---|
| `internal_tx_id` | string (UUID4) | Our canonical transaction ID. **Use this as dedup key.** |
| `easebuzz_txid` | string | EaseBuzz's own payment ID. Use for dispute resolution with EaseBuzz. |
| `fest_id` | string | Your fest identifier. Always matches your registration. |
| `origin_user_id` | string | The user ID you passed when redirecting — identifies the user in your system. |
| `user_name` | string | Legal name the user filled in on our portal. |
| `user_state` | string | State the user selected. |
| `user_district` | string | District the user selected. |
| `payment_timestamp` | string (ISO 8601) | UTC timestamp of when EaseBuzz confirmed the payment. |
| `status` | string | Always `"SUCCESS"` in this callback (we only dispatch on successful payments). |
| `source` | string | `"fest_redirect"` or `"direct_portal"` — see Section 9. |
| `miscellaneous` | boolean | `true` when the user arrived via the portal directly (not from your frontend). |

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
| `easebuzz_transaction_fee` | string | Platform fee deducted. |
| `final_net_credited` | string | What your fest actually receives (after all deductions). |

> All monetary values are strings with exactly 2 decimal places. Parse them with `Decimal`, not `float`.

---

## 9. Understanding the source Field

The portal supports two entry paths for users. Every callback you receive contains a `source` field and a `miscellaneous` boolean so your backend can handle both cases.

### `"source": "fest_redirect"` — Normal Flow

```json
"source":        "fest_redirect",
"miscellaneous": false,
"origin_user_id": "user_789"
```

The user arrived via your redirect. `origin_user_id` is the exact value you sent in the `?origin_user_id=` query parameter. You can map it back to a user in your own system.

### `"source": "direct_portal"` — Walk-in Flow

```json
"source":        "direct_portal",
"miscellaneous": true,
"origin_user_id": "portal_a1b2c3d4-..."
```

The user visited the Central Payment Portal directly and selected your fest from the listing. **You did not initiate this redirect.** The `origin_user_id` is a portal-generated UUID prefixed `"portal_"` — it has no meaning in your own user system.

### How to handle this in your callback endpoint

```python
payload = json.loads(raw_body)

if payload["miscellaneous"]:
    # Walk-in registration — no prior user record on your side
    # Create a new user record using user_name, user_state, user_district
    # Use internal_tx_id as the primary reference
    handle_walkin_registration(payload)
else:
    # Orchestrated redirect — map back to your existing user
    user = lookup_user(payload["origin_user_id"])
    confirm_payment(user, payload)
```

Your callback endpoint **must handle both cases**. The booking details (`accommodation_days`, `food_days`, financial breakdown) are identical in structure regardless of source.

---

## 10. Transaction Status Reference

```
PENDING          → Payment initiated, user on EaseBuzz page
SUCCESS          → EaseBuzz confirmed payment received
FAILED           → Payment failed or was cancelled by user
NOTIFIED_SUCCESS → We successfully delivered the callback to your endpoint
NOTIFIED_FAILURE → All 3 callback delivery attempts to your endpoint failed
```

If you see `NOTIFIED_FAILURE` in our logs, check your `callback_url` is publicly reachable and returns HTTP 200.

---

## 11. Error Scenarios

| Scenario | What happens | What you should do |
|---|---|---|
| User cancels payment on EaseBuzz | Transaction → `FAILED`. No callback sent to you. | Detect via your own session/polling if needed. |
| Your callback returns non-200 | We retry up to 3 times (2s, 4s apart). | Ensure your endpoint is stable and idempotent. |
| Signature mismatch on your side | Reject the request, do not process. | Check you're using raw bytes, correct secret_key. |
| Duplicate delivery (retry) | You receive the same `internal_tx_id` again. | Idempotency check: if already processed, return 200 immediately. |
| Your endpoint is down | After 3 retries, status → `NOTIFIED_FAILURE`. | Contact portal admin to manually replay the callback. |
| User applies invalid coupon | Portal returns an error to the user before payment. You are not affected. | Nothing required. |

---

## 12. Testing Checklist

Before going live, verify each of the following:

- [ ] You receive a POST to your `callback_url` after a test payment
- [ ] `X-Signature` header is present on every callback
- [ ] Signature verification passes with your `secret_key`
- [ ] `origin_user_id` in the callback matches what you sent in the redirect (fest_redirect case)
- [ ] `miscellaneous: true` callbacks (direct_portal) are handled as walk-in registrations
- [ ] `origin_user_id` prefixed `"portal_"` does not cause errors in your user lookup logic
- [ ] `internal_tx_id` is stored in your DB for deduplication
- [ ] Sending the same callback twice does NOT double-book the user
- [ ] Your endpoint returns HTTP 200 within 10 seconds
- [ ] Your endpoint works over HTTPS (HTTP will be rejected in production)
- [ ] Monetary amounts are parsed as `Decimal`, not `float`
- [ ] You handle `accommodation_days: 0` or `food_days: 0` gracefully

---

## 13. Code Samples

### Verifying the Signature — Python

```python
import hashlib
import hmac
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

    # Idempotency check
    if already_processed(internal_tx_id):
        return {"status": "already_processed"}

    # Process booking
    mark_user_paid(origin_user_id, payload)
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
    const { internal_tx_id, origin_user_id, booking } = payload;

    // Idempotency check
    if (db.transactionExists(internal_tx_id)) {
        return res.status(200).json({ status: "already_processed" });
    }

    // Process booking
    db.markUserPaid(origin_user_id, payload);
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
    "io"
    "net/http"
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

    // parse payload, check idempotency, process booking...
    w.WriteHeader(http.StatusOK)
    w.Write([]byte(`{"status":"ok"}`))
}
```

---

## Contact

For onboarding, secret key rotation, or production issues — contact the portal admin team.
For payment disputes (wrong amount, double charge) — use `easebuzz_txid` when contacting EaseBuzz support.
