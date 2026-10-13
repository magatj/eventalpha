"""job heartbeat for orphaned-job recovery

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_jobs", "heartbeat_at")
