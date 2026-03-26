from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from fastapi import FastAPI, Request
from pymongo import ASCENDING, DESCENDING

from .config import get_settings


async def init_db(app: FastAPI) -> None:
    settings = get_settings()

    # Connection pool tuned for Atlas + payment burst traffic.
    #
    # minPoolSize=10  — 10 warm TLS connections kept alive per pod at all times.
    #                   During a fest registration spike the first 10 concurrent
    #                   ops fire immediately instead of paying a ~100ms TLS
    #                   handshake each.
    #
    # maxPoolSize=50  — 2 pods × 50 = 100 total Atlas connections.
    #                   Well within Atlas M0 (500) and M10+ (1500) limits.
    #
    # maxIdleTimeMS=45000 — Atlas forcibly closes idle connections after 60s.
    #                       We expire them at 45s so the pool never hands out
    #                       a stale connection.
    #
    # serverSelectionTimeoutMS — fail fast if Atlas is unreachable; lets k8s
    #                            readiness probe mark the pod NotReady quickly.
    #
    # compressors — snappy is near-zero CPU overhead and cuts wire traffic by
    #               ~60% for typical MongoDB docs. zlib as fallback.
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        settings.MONGODB_URI,
        maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
        minPoolSize=settings.MONGO_MIN_POOL_SIZE,
        maxIdleTimeMS=45_000,
        connectTimeoutMS=10_000,
        socketTimeoutMS=30_000,
        serverSelectionTimeoutMS=10_000,
        retryWrites=True,
        compressors=["snappy", "zlib"],
    )

    app.state.mongo_client = client
    app.state.db = client[settings.MONGODB_DB_NAME]
    await _create_indexes(app.state.db)


async def close_db(app: FastAPI) -> None:
    app.state.mongo_client.close()


async def ping_db(app: FastAPI) -> bool:
    """
    Used by the /health endpoint.
    Returns True if Atlas responds within the serverSelectionTimeout.
    The admin command is cheap — it never touches any collection.
    """
    try:
        await app.state.mongo_client.admin.command("ping")
        return True
    except Exception:
        return False


async def _create_indexes(db: AsyncIOMotorDatabase) -> None:
    fests_col = db["fests"]
    await fests_col.create_index("fest_id", unique=True)

    tx_col = db["transactions"]
    await tx_col.create_index("internal_tx_id", unique=True)
    await tx_col.create_index("easebuzz_txid")

    # Compound indexes for the two most common multi-field query patterns:
    #
    # (fest_id, status) — balance sheet aggregation matches on status then
    #                     groups by fest_id. Also used by per-fest filtered
    #                     queries in export routes.
    #
    # (fest_id, initiated_at DESC) — CSV export sorts transactions newest-first
    #                                for a given fest. Without this index Mongo
    #                                does a full collection scan + in-memory sort.
    await tx_col.create_index([("fest_id", ASCENDING), ("status", ASCENDING)])
    await tx_col.create_index([("fest_id", ASCENDING), ("initiated_at", DESCENDING)])


def get_db(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.db


def get_fests_col(db: AsyncIOMotorDatabase) -> AsyncIOMotorCollection:
    return db["fests"]


def get_transactions_col(db: AsyncIOMotorDatabase) -> AsyncIOMotorCollection:
    return db["transactions"]
