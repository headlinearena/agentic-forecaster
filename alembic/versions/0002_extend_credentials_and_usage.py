"""extend agent_credentials and agent_llm_usage for state migration

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_credentials",
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "agent_credentials",
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "agent_credentials",
        sa.Column("last_cycle_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "agent_llm_usage",
        sa.Column("status", sa.Text(), nullable=False, server_default="success"),
    )
    op.add_column("agent_llm_usage", sa.Column("model_provider", sa.Text(), nullable=True))
    op.add_column("agent_llm_usage", sa.Column("deployment", sa.Text(), nullable=True))
    op.add_column(
        "agent_llm_usage",
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("agent_llm_usage", "details")
    op.drop_column("agent_llm_usage", "deployment")
    op.drop_column("agent_llm_usage", "model_provider")
    op.drop_column("agent_llm_usage", "status")

    op.drop_column("agent_credentials", "last_cycle_at")
    op.drop_column("agent_credentials", "granted_scopes")
    op.drop_column("agent_credentials", "payload")
