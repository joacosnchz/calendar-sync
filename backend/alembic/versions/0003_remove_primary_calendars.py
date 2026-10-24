"""remove primary calendars: the app's own calendar is the unified calendar

Source events are no longer mirrored into a per-user primary calendar in our tenant. The
unified calendar is built from the stored source events (de-duplicated by dedup_key) and
shown in the app, so the users.primary_* columns and the mirror_mappings table go, along
with the primary (Calendars.ReadWrite) app.

Mirrors already written to the old primary calendars are NOT deleted by this migration:
the primary app is gone, so nothing here can reach them. Operators remove those calendars
(or the shared mailbox) manually. Drain the queue before deploying: old reconcile_group /
rebuild_user / provision_primary jobs no longer exist.

Downgrade recreates the columns and the (empty) table. Every user comes back as
"provisioning", so the old scheduler provisions a fresh primary calendar for each and
mirrors their stored events into it; calendars from before the upgrade aren't reattached.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_mirror_mappings_user_id"), table_name="mirror_mappings")
    op.drop_table("mirror_mappings")
    op.drop_column("users", "primary_error")
    op.drop_column("users", "primary_status")
    op.drop_column("users", "primary_calendar_id")
    op.drop_column("users", "primary_mailbox")


def downgrade() -> None:
    op.add_column("users", sa.Column("primary_mailbox", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("primary_calendar_id", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("primary_status", sa.String(length=32), nullable=False, server_default="provisioning"),
    )
    op.alter_column("users", "primary_status", server_default=None)
    op.add_column("users", sa.Column("primary_error", sa.Text(), nullable=True))
    op.create_table(
        "mirror_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("primary_mailbox", sa.String(length=320), nullable=True),
        sa.Column("primary_calendar_id", sa.Text(), nullable=True),
        sa.Column("graph_event_id", sa.Text(), nullable=True),
        sa.Column("pending_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("last_written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "dedup_key"),
    )
    op.create_index(op.f("ix_mirror_mappings_user_id"), "mirror_mappings", ["user_id"], unique=False)
