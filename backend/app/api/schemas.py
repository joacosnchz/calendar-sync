import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PrimaryOut(BaseModel):
    status: str
    mailbox: str | None
    error: str | None
    mode: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_operator: bool
    primary: PrimaryOut


class TenantRef(BaseModel):
    id: uuid.UUID
    display_name: str
    tenant_id: str | None
    consent_status: str


class SubscriptionHealth(BaseModel):
    expires_at: datetime
    renew_due_at: datetime
    last_renewed_at: datetime | None
    status: str
    last_error: str | None


class SourceOut(BaseModel):
    id: uuid.UUID
    label: str
    source_email: str
    status: str
    tenant: TenantRef
    last_synced_at: datetime | None
    last_full_sync_at: datetime | None
    last_notification_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    subscription: SubscriptionHealth | None
    event_count: int
    consent_url: str | None = None
    created_at: datetime


class SourceUpdate(BaseModel):
    label: str = Field(min_length=1, max_length=64)


class CalendarEventOut(BaseModel):
    key: str
    subject: str
    location: str
    start: datetime
    end: datetime
    is_all_day: bool
    show_as: str
    labels: list[str]
    source_ids: list[uuid.UUID]
    mirrored: bool


class TenantCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    domain: str | None = Field(default=None, max_length=256)
    tenant_id: str | None = Field(default=None, max_length=64)
    copy_details: bool = True


class TenantUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    copy_details: bool | None = None


class TenantOut(BaseModel):
    id: uuid.UUID
    display_name: str
    domain: str | None
    tenant_id: str | None
    consent_status: str
    copy_details: bool
    connected_at: datetime | None
    revoked_at: datetime | None
    last_error: str | None
    created_at: datetime
    source_count: int = 0
    active_source_count: int = 0
    next_subscription_expiry: datetime | None = None
    last_notification_at: datetime | None = None
    errors_24h: int = 0
    consent_url: str | None = None


class UserAdminOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_operator: bool
    primary_status: str
    primary_mailbox: str | None
    primary_error: str | None
    source_count: int
    mirror_count: int
    created_at: datetime


class UserAdminUpdate(BaseModel):
    primary_mailbox: str | None = Field(default=None, max_length=320)
    is_operator: bool | None = None


class SubscriptionAdminOut(BaseModel):
    id: uuid.UUID
    graph_subscription_id: str
    source_id: uuid.UUID
    source_email: str
    user_email: str
    tenant_name: str
    expires_at: datetime
    renew_due_at: datetime
    last_renewed_at: datetime | None
    status: str
    last_error: str | None


class SyncErrorOut(BaseModel):
    id: uuid.UUID
    kind: str
    message: str
    created_at: datetime
    source_email: str | None
    user_email: str | None
    tenant_name: str | None


class AuditOut(BaseModel):
    id: uuid.UUID
    actor: str
    action: str
    target_type: str
    target_id: str | None
    details: dict
    created_at: datetime


class OverviewOut(BaseModel):
    users: int
    tenants: dict[str, int]
    sources: dict[str, int]
    subscriptions: int
    subscriptions_expiring_24h: int
    subscriptions_failing: int
    errors_24h: int
    mirrors: int
