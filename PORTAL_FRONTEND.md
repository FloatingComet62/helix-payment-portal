# Portal Frontend Integration Guide

This document is written for the developers building the **Central Payment Portal's own frontend UI** — the web interface that sits between the external fest redirect and the EaseBuzz payment page.

---

## Table of Contents

1. [Where the Portal Frontend Sits](#1-where-the-portal-frontend-sits)
2. [Step 1 — Read the Incoming Redirect](#2-step-1--read-the-incoming-redirect)
3. [Step 2 — Collect User Details](#3-step-2--collect-user-details)
4. [Step 3 — Apply a Coupon (Optional)](#4-step-3--apply-a-coupon-optional)
5. [Step 4 — Call POST /payment/initiate](#5-step-4--call-post-paymentinitiate)
6. [Step 5 — Submit the EaseBuzz Form](#6-step-5--submit-the-easebuzz-form)
7. [Step 6 — Handle EaseBuzz Redirect Back](#7-step-6--handle-easebuzz-redirect-back)
8. [API Reference](#8-api-reference)
9. [Error Handling](#9-error-handling)
10. [Full Page Flow Summary](#10-full-page-flow-summary)

---

## 1. Where the Portal Frontend Sits

```
External Fest Frontend
      │
      │  ?fest_id=...&origin_user_id=...   (both params required)
      ▼
┌─────────────────────────────────────────────────────┐
│                PORTAL FRONTEND (you)                │
│                                                     │
│  Read redirect params → show booking form           │
└─────────────────────────────────────────────────────┘
      │
      │  POST /payment/initiate  (to our own backend)
      ▼
Portal Backend  ──▶  EaseBuzz Payment Page
                           │
                           │  User pays
                           ▼
                     EaseBuzz redirects
                     back to surl / furl
                           │
                           ▼
                  Portal Frontend success/failure page
```

All users arrive via a fest redirect. There is no direct portal visit path — `fest_id` and `origin_user_id` are always required.

---

## 2. Step 1 — Read the Incoming Redirect

The external fest frontend redirects the user to your portal's URL with two mandatory query parameters.

```
https://your-portal.com/pay?fest_id=HackTU_2026&origin_user_id=user_789
```

Read both parameters immediately on page load. If either is missing, show an error page — the user arrived here incorrectly (e.g. typed the URL directly or followed a broken link).

```javascript
const params = new URLSearchParams(window.location.search);
const festId        = params.get("fest_id");
const originUserId  = params.get("origin_user_id");

if (!festId || !originUserId) {
    showError("Invalid payment link. Please return to the fest website and try again.");
    return;
}

// Both present — proceed to booking form
startBookingFlow(festId, originUserId);
```

Store both in component/page state. You will need them in the `POST /payment/initiate` body.

---

## 3. Step 2 — Collect User Details

Display a form to collect the following fields from the user. All are required.

| Field | Type | Label (suggested) | Notes |
|---|---|---|---|
| `user_name` | string | Full Legal Name | As per government ID |
| `user_state` | string | State | Dropdown preferred |
| `user_district` | string | District | Dropdown or text |
| `accommodation_days` | int | Accommodation Days | 0 if not booking accommodation |
| `food_days` | int | Food Days | 0 if not booking food |

> **Do not ask the user for `fest_id` or `origin_user_id`** — these come from the URL params and are passed silently.

### Booking options — show only what's available

Call `GET /portal/fests/{fest_id}` on page load and check the `available_options` field before rendering the booking form. Only show the options the fest supports:

```javascript
// available_options is an array of: "accommodation", "food", "bundled"
const showAccommodation = festData.available_options.includes("accommodation")
                       || festData.available_options.includes("bundled");
const showFood          = festData.available_options.includes("food")
                       || festData.available_options.includes("bundled");
```

If a fest only offers food (e.g. `available_options: ["food"]`), hide the accommodation days input and send `accommodation_days: 0`.

### Fest display info

The `GET /portal/fests/{fest_id}` response also includes optional display fields you can render at the top of the booking page:

| Field | Type | Description |
|---|---|---|
| `event_dates` | string \| null | Human-readable event date string, e.g. `"March 28–30, 2026"` |
| `short_description` | string \| null | One-line tagline for the fest |
| `long_description` | string \| null | Full description / about text |

### Pricing display (optional but recommended)

Before the user submits, you can display estimated pricing. Use the fest's `pricing` object (returned by `GET /portal/fests/{fest_id}`) to show accommodation/food rates.

**Pricing mode reference:**

| Mode | Fields to display | Rate formula |
|---|---|---|
| `fixed` | `accommodation_price`, `food_price`, `bundled_price` | Flat fee per option (days not used) |
| `per_day` | `addon.accommodation_per_day`, `addon.food_per_day` | Rate × days booked |
| `bundled` | `addon.accommodation_per_day`, `addon.food_per_day`, `addon.bundled_discount_percent` | Rate × days; discount applied when both are booked |

The exact final amounts are always returned by `POST /payment/initiate` — treat those as authoritative.

---

## 4. Step 3 — Apply a Coupon (Optional)

Render an optional "Apply Coupon" input on the form. When the user submits a coupon code, pass it in the `coupon_code` field of the `POST /payment/initiate` body. The backend will validate it against EaseBuzz's coupon API and return the discounted amount.

You do **not** call EaseBuzz's coupon API directly from the frontend. Just pass the code string through.

If the coupon is invalid, `POST /payment/initiate` will return a `422` error with a descriptive message — display it inline on the form.

---

## 5. Step 4 — Call POST /payment/initiate

Once the user fills the form and clicks "Proceed to Payment", call your portal backend.

### Request

```
POST /payment/initiate
Content-Type: application/json
```

```json
{
  "fest_id":        "HackTU_2026",
  "origin_user_id": "user_789",
  "user_name":      "Arjun Sharma",
  "user_state":     "Punjab",
  "user_district":  "Ludhiana",
  "booking": {
    "accommodation_days":   2,
    "food_days":            2,
    "accommodation_amount": "0.00",
    "food_amount":          "0.00"
  },
  "coupon_code": "HACK100"
}
```

> `accommodation_amount` and `food_amount` inside the `booking` object are **computed by the backend** — pass `"0.00"` here. The backend fills in the correct values before storing.

### Response (201 Created)

```json
{
  "internal_tx_id":       "550e8400-e29b-41d4-a716-446655440000",
  "easebuzz_payment_url": "https://testpay.easebuzz.in/payment/initiateLink",
  "txnid":                "550e8400-e29b-41d4-a716-446655440000",
  "key":                  "ABCDE12345",
  "hash":                 "a3f8c1d2e4...sha512 hex...",
  "amount":               "1451.40",
  "productinfo":          "HackTU_2026_booking",
  "firstname":            "Arjun Sharma",
  "email":                "noreply@portal.local",
  "phone":                "9999999999",
  "udf1":                 "user_789",
  "udf2":                 "HackTU_2026",
  "udf3":                 "",
  "udf4":                 "",
  "udf5":                 "",
  "surl":                 "https://api.accommodationstiet.shop/webhook/easebuzz",
  "furl":                 "https://api.accommodationstiet.shop/webhook/easebuzz"
}
```

**Before submitting to EaseBuzz:** optionally show the user a confirmation screen with the amount breakdown. The `amount` field is what EaseBuzz will charge them.

---

## 6. Step 5 — Submit the EaseBuzz Form

EaseBuzz requires a **browser-side HTML form POST** — you cannot do this via `fetch` or `axios` because EaseBuzz needs the browser to navigate to their hosted payment page.

Build and auto-submit a hidden form using every field from the response:

```javascript
function redirectToEaseBuzz(data) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = data.easebuzz_payment_url;

  const fields = [
    "key", "txnid", "amount", "productinfo",
    "firstname", "email", "phone",
    "udf1", "udf2", "udf3", "udf4", "udf5",
    "surl", "furl", "hash"
  ];

  fields.forEach((field) => {
    const input = document.createElement("input");
    input.type  = "hidden";
    input.name  = field;
    input.value = data[field];
    form.appendChild(input);
  });

  document.body.appendChild(form);
  form.submit();
}
```

Call this function after the user confirms the amount on your confirmation screen.

> **Important:** Do not modify `hash`, `amount`, `txnid`, or `key` before submitting. EaseBuzz will reject the payment if the hash doesn't match.

---

## 7. Step 6 — Handle EaseBuzz Redirect Back

After payment, EaseBuzz redirects the user's browser back to `surl` (on success) or `furl` (on failure). Both currently point to `/webhook/easebuzz` — which is a server-side route that handles the S2S confirmation and returns `{"status": "ok"}`.

For a proper user experience, configure two separate frontend routes:

| Route | Purpose |
|---|---|
| `/payment/success?txnid=...` | Shown to user after successful payment |
| `/payment/failure?txnid=...` | Shown to user after failed/cancelled payment |

Update `PORTAL_CALLBACK_URL` in your `.env` accordingly, and make sure your portal backend routes these correctly. The **actual payment confirmation** to the fest happens via EaseBuzz's separate S2S webhook — the browser redirect is just for UX.

### What to show on the success page

Use the `txnid` query param (= `internal_tx_id`) to look up and display:
- Booking summary (accommodation days, food days)
- Amount paid
- A reference number for the user to note down

Do **not** mark anything as confirmed purely based on the browser redirect — the ground truth is the S2S webhook update in your MongoDB `transactions` collection.

---

## 8. API Reference

### `POST /payment/initiate`

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `fest_id` | string | Yes | From URL query param |
| `origin_user_id` | string | Yes | From URL query param — always required |
| `user_name` | string | Yes | User's legal name |
| `user_state` | string | Yes | User's state |
| `user_district` | string | Yes | User's district |
| `booking.accommodation_days` | int | Yes | 0 if not booking accommodation |
| `booking.food_days` | int | Yes | 0 if not booking food |
| `booking.accommodation_amount` | string | Yes | Pass `"0.00"` — backend computes this |
| `booking.food_amount` | string | Yes | Pass `"0.00"` — backend computes this |
| `coupon_code` | string | No | Coupon code string if user applied one |

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `internal_tx_id` | string | Our transaction UUID — store this for reference |
| `easebuzz_payment_url` | string | The URL to POST the EaseBuzz form to |
| `key` | string | EaseBuzz merchant key |
| `txnid` | string | Transaction ID (= `internal_tx_id`) |
| `hash` | string | sha512 hash — submit as-is, do not modify |
| `amount` | string | Total amount EaseBuzz will charge the user (INR) |
| `productinfo` | string | Booking description |
| `firstname` | string | User's first name |
| `email` | string | User's email (portal default if not collected) |
| `phone` | string | User's phone (portal default if not collected) |
| `udf1`–`udf5` | string | Custom fields — carry `origin_user_id` and `fest_id` |
| `surl` | string | EaseBuzz success redirect URL |
| `furl` | string | EaseBuzz failure redirect URL |

**Error responses:**

| Status | When |
|---|---|
| `404` | `fest_id` does not exist |
| `409` | No accommodation seats available (fest has seat cap, all filled) |
| `422` | Invalid coupon code / validation failure |
| `500` | Internal server error |

---

## 9. Error Handling

| Scenario | What to show the user |
|---|---|
| Missing `fest_id` or `origin_user_id` in URL | "Invalid payment link. Please return to the fest website and try again." |
| `404` from `/payment/initiate` | "This fest is not registered. Contact the fest organizer." |
| `409` from `/payment/initiate` (no seats) | "No accommodation seats are available for this fest. Food-only bookings may still be available — contact the fest organizer." |
| `422` from `/payment/initiate` (bad coupon) | Inline: "Coupon code is invalid or expired." |
| EaseBuzz payment page error | EaseBuzz handles this — user is redirected to `furl` |
| User lands on failure page | "Payment was not completed. You have not been charged. Please try again." |
| Network timeout calling `/payment/initiate` | "Could not connect to the payment server. Please try again in a few moments." |

---

## 10. Full Page Flow Summary

```
User arrives at portal
  │
  │  ?fest_id=X&origin_user_id=Y  (both required)
  ▼
Check both params present
  │  Missing? → show error "Invalid payment link"
  │  Present  → proceed
  ▼
┌─────────────────────────────────┐
│  Page: Booking Form             │
│  ─────────────────              │
│  Full Legal Name   [          ] │
│  State             [▼ Punjab  ] │
│  District          [▼ Ludhiana] │
│  Accommodation     [2] days     │
│  Food              [2] days     │
│  Coupon Code       [          ] │
│                                 │
│         [Proceed to Payment]    │
└────────────────┬────────────────┘
                 │  POST /payment/initiate
                 │  (fest_id + origin_user_id from URL params)
                 │
                 │  409? → "No seats available"
                 │  422? → show coupon error inline
                 ▼
┌─────────────────────────────────┐
│  Page: Confirm & Pay            │
│  ─────────────────              │
│  Accommodation (2 days): ₹1000  │
│  Food (2 days):           ₹400  │
│  Bundle Discount (5%):   -₹70   │
│  Coupon (HACK100):       -₹100  │
│  GST (18%):              ₹221   │
│  ─────────────────────────────  │
│  Total:                 ₹1451   │
│                                 │
│         [Pay Now]               │
└────────────────┬────────────────┘
                 │  Auto-submit hidden HTML form
                 ▼
         EaseBuzz Hosted Payment Page
         (card / UPI / netbanking)
                 │
         ┌───────┴────────┐
         │                │
    Payment OK        Payment Failed
         │                │
         ▼                ▼
  /payment/success   /payment/failure
  Show booking ref   "Try again" CTA
```
