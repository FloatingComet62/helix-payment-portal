from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from redis.asyncio import Redis, from_url

from app.core.exceptions import SeatUnavailableError

logger = logging.getLogger(__name__)

# ── Key helpers ──────────────────────────────────────────────────────────────

def _seat_key(fest_id: str) -> str:
    return f"seats:{fest_id}:accommodation"

def _reservation_key(internal_tx_id: str) -> str:
    return f"reservation:{internal_tx_id}"

RESERVATION_TTL_SECONDS = 15 * 60  # 15 minutes — auto-expires if user abandons payment

# ── Lua script: atomic DECR with floor-at-zero rollback ──────────────────────
# Returns the remaining seat count after decrement, or -1 if no seats left.
_ACQUIRE_SCRIPT = """
local key   = KEYS[1]
local resv  = KEYS[2]
local ttl   = tonumber(ARGV[1])
local remaining = redis.call('DECR', key)
if remaining < 0 then
    redis.call('INCR', key)
    return -1
end
redis.call('SET', resv, '1', 'EX', ttl)
return remaining
"""

# ── Client lifecycle ─────────────────────────────────────────────────────────

async def init_redis(url: str) -> Redis:
    client = from_url(url, decode_responses=True)
    await client.ping()
    logger.info("Redis connection established: %s", url)
    return client


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


# ── Seat operations ───────────────────────────────────────────────────────────

async def acquire_seat(redis: Redis, fest_id: str, internal_tx_id: str) -> None:
    """
    Atomically decrement the available seat count for a fest.
    Stores a reservation key with a 15-minute TTL so the seat is
    auto-released if the user never completes payment.

    Raises SeatUnavailableError (HTTP 409) if no seats remain.
    """
    result = await redis.eval(
        _ACQUIRE_SCRIPT,
        2,                              # numkeys
        _seat_key(fest_id),
        _reservation_key(internal_tx_id),
        RESERVATION_TTL_SECONDS,
    )
    if result == -1:
        raise SeatUnavailableError(fest_id)
    logger.info(
        "Seat acquired",
        extra={"fest_id": fest_id, "internal_tx_id": internal_tx_id, "remaining": result},
    )


async def confirm_seat(redis: Redis, internal_tx_id: str) -> None:
    """
    Called on payment SUCCESS. Permanently consumes the seat by deleting
    the reservation key (no INCR — seat stays decremented).
    """
    await redis.delete(_reservation_key(internal_tx_id))
    logger.info("Seat confirmed (permanently consumed)", extra={"internal_tx_id": internal_tx_id})


async def release_seat(redis: Redis, fest_id: str, internal_tx_id: str) -> None:
    """
    Called on payment FAILURE or refund.
    Only releases if the reservation key still exists — if it already
    expired (user took > 15 min), the seat was auto-released, so we skip
    the INCR to avoid double-counting.
    """
    resv_key = _reservation_key(internal_tx_id)
    deleted = await redis.delete(resv_key)   # returns 1 if key existed, 0 if already gone
    if deleted:
        await redis.incr(_seat_key(fest_id))
        logger.info(
            "Seat released",
            extra={"fest_id": fest_id, "internal_tx_id": internal_tx_id},
        )
    else:
        logger.info(
            "Seat reservation already expired — no release needed",
            extra={"fest_id": fest_id, "internal_tx_id": internal_tx_id},
        )


# ── Startup initialization ────────────────────────────────────────────────────

async def init_all_seats(
    redis: Redis,
    fests_col,
    tx_col,
) -> None:
    """
    Called once at pod startup. Re-initializes seat counts in Redis from
    MongoDB so that pod restarts and Redis flushes don't cause drift.

    Only counts transactions with status in [SUCCESS, NOTIFIED_SUCCESS,
    NOTIFIED_FAILURE] — these all represent confirmed payments regardless
    of whether the fest callback succeeded.

    PENDING transactions are NOT counted: their reservations had a 15-min
    TTL which will have already expired after any pod restart.
    """
    confirmed_statuses = ["SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"]

    cursor = fests_col.find(
        {"total_accommodation_seats": {"$exists": True, "$ne": None}},
        {"fest_id": 1, "total_accommodation_seats": 1, "_id": 0},
    )

    initialized = 0
    async for fest_doc in cursor:
        fest_id = fest_doc["fest_id"]
        total: int = fest_doc["total_accommodation_seats"]

        confirmed = await tx_col.count_documents({
            "fest_id": fest_id,
            "status": {"$in": confirmed_statuses},
            "booking.accommodation_days": {"$gt": 0},
        })

        available = max(0, total - confirmed)
        await redis.set(_seat_key(fest_id), available)
        initialized += 1
        logger.info(
            "Seat counter initialized",
            extra={"fest_id": fest_id, "total": total, "confirmed": confirmed, "available": available},
        )

    logger.info("Redis seat initialization complete — %d fests initialized", initialized)


async def sync_seats_for_fest(
    redis: Redis,
    fest_id: str,
    total_seats: int,
    tx_col,
) -> int:
    """
    Sets the Redis seat counter for a single fest.
    Called immediately after a fest is created or its total_accommodation_seats is updated
    so the counter is live without needing a pod restart.

    Returns the available count that was set.
    """
    confirmed_statuses = ["SUCCESS", "NOTIFIED_SUCCESS", "NOTIFIED_FAILURE"]
    confirmed = await tx_col.count_documents({
        "fest_id": fest_id,
        "status": {"$in": confirmed_statuses},
        "booking.accommodation_days": {"$gt": 0},
    })
    available = max(0, total_seats - confirmed)
    await redis.set(_seat_key(fest_id), available)
    logger.info(
        "Seat counter synced",
        extra={"fest_id": fest_id, "total": total_seats, "confirmed": confirmed, "available": available},
    )
    return available
