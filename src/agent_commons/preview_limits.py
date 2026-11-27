"""Authoritative, transactional preview quota buckets."""

from datetime import datetime, timedelta
from hashlib import sha256
from math import ceil
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import DateTime, Interval, String, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from agent_commons.db import Base, SessionLocal
from agent_commons.peer_models import PeerGrant


class PreviewQuota(Base):
    __tablename__ = "preview_quotas"

    bucket: Mapped[str] = mapped_column(String(80), primary_key=True)
    timestamps: Mapped[list[datetime]] = mapped_column(ARRAY(DateTime(timezone=True)))


def shard(value: str) -> int:
    # Fixed key space even if clients can change addresses or account IDs.
    return sha256(value.encode()).digest()[0]


def _timeouts(db: Session) -> None:
    db.execute(text("SET LOCAL statement_timeout = '1s'"))
    db.execute(text("SET LOCAL lock_timeout = '250ms'"))


def _consume(db: Session, bucket: str, limit: int, seconds: float) -> None:
    _timeouts(db)
    row = db.get(PreviewQuota, bucket, with_for_update=True)
    if row is None:
        db.execute(
            insert(PreviewQuota).values(bucket=bucket, timestamps=[]).on_conflict_do_nothing()
        )
        row = db.get(PreviewQuota, bucket, with_for_update=True)
    now = db.scalar(select(func.clock_timestamp()))
    cutoff = now - timedelta(seconds=seconds)
    recent = [stamp for stamp in row.timestamps if stamp > cutoff]
    if len(recent) >= limit:
        raise quota_exceeded((min(recent) - cutoff).total_seconds())
    row.timestamps = [*recent, now]
    db.flush()


def quota_exceeded(seconds: float) -> HTTPException:
    return HTTPException(
        429, "Preview request rate exceeded", headers={"Retry-After": str(max(1, ceil(seconds)))}
    )


def consume(
    bucket: str,
    limit: int,
    seconds: float,
    *,
    db: Session | None = None,
) -> None:
    try:
        if db is not None:
            _consume(db, bucket, limit, seconds)
        else:
            with SessionLocal.begin() as session:
                _consume(session, bucket, limit, seconds)
    except SQLAlchemyError:
        raise HTTPException(503, "Preview admission is unavailable") from None


def _consume_grant(db: Session, grant_id: UUID, rate: float, burst: int) -> None:
    """Atomically advance a grant's theoretical arrival time."""
    interval = timedelta(seconds=1 / rate)
    now = func.clock_timestamp()
    _timeouts(db)
    admitted = db.scalar(
        update(PeerGrant)
        .where(
            PeerGrant.id == grant_id,
            PeerGrant.expires_at > now,
            PeerGrant.revoked_at.is_(None),
            func.coalesce(PeerGrant.rate_next_at, now)
            <= now + literal((burst - 1) * interval, Interval()),
        )
        .values(rate_next_at=func.greatest(PeerGrant.rate_next_at, now) + interval)
        .returning(PeerGrant.id)
    )
    if admitted is None:
        raise quota_exceeded(interval.total_seconds())


def consume_grant(grant_id: UUID, rate: float, burst: int, *, db: Session | None = None) -> None:
    try:
        if db is not None:
            _consume_grant(db, grant_id, rate, burst)
        else:
            with SessionLocal.begin() as session:
                _consume_grant(session, grant_id, rate, burst)
    except SQLAlchemyError:
        raise HTTPException(503, "Preview admission is unavailable") from None
