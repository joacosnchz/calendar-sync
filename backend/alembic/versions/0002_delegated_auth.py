"""delegated source access: per-source refresh tokens, no tenant admin consent

Source calendars are now read with each mailbox owner's delegated Calendars.Read grant
instead of an admin-consented application permission. Each linked source stores its own
(encrypted) refresh token, and a source whose grant stops working is paused as
reauth_required until the user reconnects it.

No existing source has a refresh token, so every source that isn't being removed becomes
reauth_required: its mirrors are kept and the user is asked to reconnect.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

import app.crypto
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECONNECT_MESSAGE = (
    "Calendar Sync now reads calendars with your own Microsoft sign-in. "
    "Sign in again to reconnect this calendar."
)


def upgrade() -> None:
    op.add_column("linked_sources", sa.Column("refresh_token", app.crypto.EncryptedText(), nullable=True))
    op.add_column("linked_sources", sa.Column("granted_scopes", sa.Text(), nullable=True))
    op.add_column(
        "linked_sources", sa.Column("reauth_required_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE linked_sources SET status = 'reauth_required', reauth_required_at = now(), "
            "last_error = :message, last_error_at = now() "
            "WHERE status IN ('active', 'pending_consent', 'revoked', 'error')"
        ).bindparams(message=RECONNECT_MESSAGE)
    )
    op.drop_column("tenants", "consent_status")
    op.drop_column("tenants", "connected_at")
    op.drop_column("tenants", "revoked_at")


def downgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("consent_status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.alter_column("tenants", "consent_status", server_default=None)
    op.add_column("tenants", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    # Without admin consent nothing can sync under the old model: links wait for it again.
    op.execute(
        "UPDATE linked_sources SET status = 'pending_consent' WHERE status IN ('active', 'reauth_required')"
    )
    op.drop_column("linked_sources", "reauth_required_at")
    op.drop_column("linked_sources", "granted_scopes")
    op.drop_column("linked_sources", "refresh_token")
