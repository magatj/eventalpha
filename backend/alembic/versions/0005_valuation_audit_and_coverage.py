"""valuation audit, screening coverage and market splits

Adds the per-analysis valuation audit (status + evidence), multi-reason exclusion codes and
provider share counts on securities, the SEC companyfacts fallback on screening fundamentals, and
a market-wide splits table. Purely additive: existing analyses and forecasts are untouched
(their valuation_status stays NULL = produced by an earlier model version).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('analyses', sa.Column('valuation_status', sa.String(length=24), nullable=True))
    op.add_column('analyses', sa.Column('valuation', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index(op.f('ix_analyses_valuation_status'), 'analyses', ['valuation_status'], unique=False)
    op.add_column('securities', sa.Column('exclusion_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('securities', sa.Column('provider_shares', sa.Float(), nullable=True))
    op.add_column('securities', sa.Column('provider_market_cap', sa.Float(), nullable=True))
    op.add_column('securities', sa.Column('provider_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('security_fundamentals', sa.Column('fallback', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('security_fundamentals', sa.Column('fallback_fetched_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'market_splits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('data_mode', sa.String(length=8), nullable=False),
        sa.Column('ticker', sa.String(length=16), nullable=False),
        sa.Column('execution_date', sa.Date(), nullable=False),
        sa.Column('split_from', sa.Float(), nullable=False),
        sa.Column('split_to', sa.Float(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('data_mode', 'ticker', 'execution_date', name='uq_market_split'),
    )
    op.create_index(op.f('ix_market_splits_ticker'), 'market_splits', ['ticker'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_market_splits_ticker'), table_name='market_splits')
    op.drop_table('market_splits')
    op.drop_column('security_fundamentals', 'fallback_fetched_at')
    op.drop_column('security_fundamentals', 'fallback')
    op.drop_column('securities', 'provider_checked_at')
    op.drop_column('securities', 'provider_market_cap')
    op.drop_column('securities', 'provider_shares')
    op.drop_column('securities', 'exclusion_codes')
    op.drop_index(op.f('ix_analyses_valuation_status'), table_name='analyses')
    op.drop_column('analyses', 'valuation')
    op.drop_column('analyses', 'valuation_status')
