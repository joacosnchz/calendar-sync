import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.crypto import EncryptedText


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSON().with_variant(JSONB(), "postgresql"),
    }


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# --- status vocabularies -------------------------------------------------------------


class SourceStatus:
    ACTIVE = "active"
    # The delegated grant stopped working (refresh token expired or revoked, MFA or
    # consent now required, password reset...). Syncing is paused and existing mirrors
    # are kept until the user signs in again (Reconnect).
    REAUTH_REQUIRED = "reauth_required"
    REMOVING = "removing"
    ERROR = "error"

    # Sources whose stored events still back mirrors in the primary calendar.
    MIRRORED = (ACTIVE, REAUTH_REQUIRED)


class PrimaryStatus:
    UNASSIGNED = "unassigned"  # user_mailbox mode, waiting for an operator
    PROVISIONING = "provisioning"
    READY = "ready"
    ERROR = "error"


class SubscriptionStatus:
    ACTIVE = "active"
    RENEW_FAILED = "renew_failed"


# --- entities ------------------------------------------------------------------------


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("login_tenant_id", "login_object_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    # Identity the user signs in to this platform with.
    login_tenant_id: Mapped[str] = mapped_column(String(64))
    login_object_id: Mapped[str] = mapped_column(String(64))
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False)

    # Primary calendar in our tenant.
    primary_mailbox: Mapped[str | None] = mapped_column(String(320))
    primary_calendar_id: Mapped[str | None] = mapped_column(Text)
    primary_status: Mapped[str] = mapped_column(String(32), default=PrimaryStatus.UNASSIGNED)
    primary_error: Mapped[str | None] = mapped_column(Text)

    sources: Mapped[list["LinkedSource"]] = relationship(back_populates="user", passive_deletes=True)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # Entra ID tenant GUID. Recorded the first time a user links a mailbox from it.
    tenant_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(256))
    domain: Mapped[str | None] = mapped_column(String(256))
    # Per-customer privacy control: when false, mirrors show "Busy" with no subject/location.
    copy_details: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text)

    sources: Mapped[list["LinkedSource"]] = relationship(back_populates="tenant", passive_deletes=True)


class LinkedSource(TimestampMixin, Base):
    __tablename__ = "linked_sources"
    __table_args__ = (UniqueConstraint("user_id", "tenant_pk", "source_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    source_user_id: Mapped[str] = mapped_column(String(64))  # object ID in the source tenant
    source_email: Mapped[str] = mapped_column(String(320))
    calendar_id: Mapped[str | None] = mapped_column(Text)  # None = the mailbox's default calendar
    label: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default=SourceStatus.ACTIVE, index=True)

    # Delegated access (Calendars.Read + offline_access) granted by the mailbox owner.
    # Rotated by Entra on use; the newest one is always stored.
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText)
    granted_scopes: Mapped[str | None] = mapped_column(Text)
    reauth_required_at: Mapped[datetime | None]

    delta_link: Mapped[str | None] = mapped_column(Text)
    delta_window_start: Mapped[datetime | None]
    delta_window_end: Mapped[datetime | None]
    last_synced_at: Mapped[datetime | None]
    last_full_sync_at: Mapped[datetime | None]
    last_notification_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(back_populates="sources")
    tenant: Mapped[Tenant] = relationship(back_populates="sources")
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="source", uselist=False, passive_deletes=True
    )


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("linked_sources.id", ondelete="CASCADE"), unique=True
    )
    graph_subscription_id: Mapped[str] = mapped_column(String(128), unique=True)
    client_state: Mapped[str] = mapped_column(EncryptedText)
    resource: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    renew_due_at: Mapped[datetime] = mapped_column(index=True)
    last_renewed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(32), default=SubscriptionStatus.ACTIVE)
    last_error: Mapped[str | None] = mapped_column(Text)

    source: Mapped[LinkedSource] = relationship(back_populates="subscription")


class SourceEvent(Base):
    """An event occurrence read from a source calendar. Only fields the mirror needs are kept."""

    __tablename__ = "source_events"
    __table_args__ = (
        UniqueConstraint("source_id", "graph_event_id"),
        Index("ix_source_events_dedup", "dedup_key"),
        Index("ix_source_events_window", "source_id", "start_utc", "end_utc"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("linked_sources.id", ondelete="CASCADE"))
    graph_event_id: Mapped[str] = mapped_column(Text)
    ical_uid: Mapped[str | None] = mapped_column(Text)
    # iCalUId when present (same meeting in two sources mirrors once), else source+event id.
    dedup_key: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(32), default="singleInstance")
    series_master_id: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(EncryptedText)
    location: Mapped[str | None] = mapped_column(EncryptedText)
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    start_tz: Mapped[str | None] = mapped_column(String(128))
    end_tz: Mapped[str | None] = mapped_column(String(128))
    is_all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    show_as: Mapped[str] = mapped_column(String(32), default="busy")
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal")
    last_modified: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MirrorMapping(Base):
    """Source event key (user + dedup key) -> mirrored event in the user's primary calendar."""

    __tablename__ = "mirror_mappings"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    dedup_key: Mapped[str] = mapped_column(Text)
    primary_mailbox: Mapped[str | None] = mapped_column(String(320))
    primary_calendar_id: Mapped[str | None] = mapped_column(Text)
    graph_event_id: Mapped[str | None] = mapped_column(Text)
    # Sent as the event's transactionId so a retried create never makes a second mirror.
    pending_transaction_id: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    last_written_at: Mapped[datetime | None]
    reconciled_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SyncError(Base):
    __tablename__ = "sync_errors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_pk: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("linked_sources.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(320))  # user email, "system" or "graph"
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
