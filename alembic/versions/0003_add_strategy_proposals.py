"""add strategy_proposals table

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_proposals",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("proposed_field", sa.Text(), nullable=False),
        sa.Column("current_value", postgresql.JSONB()),
        sa.Column("proposed_value", postgresql.JSONB()),
        sa.Column(
            "supporting_backtest_report_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_reports.id"),
        ),
        sa.Column(
            "supporting_lesson_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False, server_default="{}",
        ),
        sa.Column("validation_status", sa.Text(), nullable=False, server_default="not_simulatable"),
        sa.Column("validation_metrics", postgresql.JSONB()),
        sa.Column("review_status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_strategy_proposals_persona_review "
        "ON strategy_proposals (persona_id, review_status)"
    )


def downgrade() -> None:
    op.drop_table("strategy_proposals")
