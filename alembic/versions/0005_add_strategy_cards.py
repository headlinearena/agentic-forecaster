"""add strategy_cards and shadow_predictions tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_cards",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version", sa.Integer()),
        sa.Column("card", postgresql.JSONB(), nullable=False),
        # active | candidate | retired | rejected | rolled_back
        sa.Column("status", sa.Text(), nullable=False, server_default="candidate"),
        # reflection | bootstrap | rollback | manual
        sa.Column("source", sa.Text(), nullable=False, server_default="reflection"),
        sa.Column("rationale", sa.Text()),
        # pending | passed | rolled_back (only set on promoted cards)
        sa.Column("canary_status", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("persona_id", "version", name="uq_strategy_cards_persona_version"),
    )
    op.execute(
        "CREATE INDEX ix_strategy_cards_persona_status ON strategy_cards (persona_id, status)"
    )

    op.create_table(
        "shadow_predictions",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        # 0 = the card-less baseline (live persona before any card was active)
        sa.Column("card_version", sa.Integer(), nullable=False),
        sa.Column("challenge_id", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text()),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text()),
        # pending | correct | incorrect
        sa.Column("outcome", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("actual_result", postgresql.JSONB()),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "persona_id", "card_version", "challenge_id",
            name="uq_shadow_predictions_persona_version_challenge",
        ),
    )
    op.execute(
        "CREATE INDEX ix_shadow_predictions_persona_outcome "
        "ON shadow_predictions (persona_id, card_version, outcome)"
    )


def downgrade() -> None:
    op.drop_table("shadow_predictions")
    op.drop_table("strategy_cards")
