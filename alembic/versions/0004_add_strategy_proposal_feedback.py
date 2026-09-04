"""add strategy_proposal feedback columns (reviewed_at, superseded_by, outcome tracking)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("strategy_proposals", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "strategy_proposals",
        sa.Column(
            "superseded_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_proposals.id"),
        ),
    )
    op.add_column(
        "strategy_proposals",
        sa.Column("outcome_status", sa.Text(), nullable=False, server_default="not_yet_evaluated"),
    )
    op.add_column("strategy_proposals", sa.Column("outcome_metrics", postgresql.JSONB()))
    op.execute(
        "CREATE INDEX ix_strategy_proposals_persona_field_live "
        "ON strategy_proposals (persona_id, proposed_field) WHERE superseded_by IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_strategy_proposals_persona_field_live")
    op.drop_column("strategy_proposals", "outcome_metrics")
    op.drop_column("strategy_proposals", "outcome_status")
    op.drop_column("strategy_proposals", "superseded_by")
    op.drop_column("strategy_proposals", "reviewed_at")
