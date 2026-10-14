"""AI research chat: conversations, messages, cited sources and tool executions

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14 00:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'chat_conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('data_mode', sa.String(length=8), nullable=False),
        sa.Column('ticker', sa.String(length=16), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.Column('analysis_id', sa.Integer(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_chat_conv_ticker', 'chat_conversations', ['data_mode', 'ticker', 'updated_at'], unique=False)
    op.create_table(
        'chat_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('analysis_id', sa.Integer(), nullable=True),
        sa.Column('forecast_model_version', sa.String(length=32), nullable=True),
        sa.Column('ai_model', sa.String(length=64), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('citations', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('scenario', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('follow_ups', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('context_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['conversation_id'], ['chat_conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_messages_conversation_id'), 'chat_messages', ['conversation_id'], unique=False)
    op.create_table(
        'chat_source_references',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=16), nullable=False),
        sa.Column('source_type', sa.String(length=32), nullable=False),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('url', sa.Text(), nullable=True),
        sa.Column('published_at', sa.String(length=32), nullable=True),
        sa.Column('record_ref', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_source_references_message_id'), 'chat_source_references', ['message_id'], unique=False)
    op.create_table(
        'chat_tool_executions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('tool_name', sa.String(length=48), nullable=False),
        sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['chat_conversations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_tool_executions_conversation_id'), 'chat_tool_executions', ['conversation_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chat_tool_executions_conversation_id'), table_name='chat_tool_executions')
    op.drop_table('chat_tool_executions')
    op.drop_index(op.f('ix_chat_source_references_message_id'), table_name='chat_source_references')
    op.drop_table('chat_source_references')
    op.drop_index(op.f('ix_chat_messages_conversation_id'), table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('ix_chat_conv_ticker', table_name='chat_conversations')
    op.drop_table('chat_conversations')
