"""initial schema: pgvector extension + knowledge/runtime-state tables

Revision ID: 0001
Revises:
Create Date: 2026-07-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "market_data_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("asset_key", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_market_data_snapshot_source_asset_fetched "
        "ON market_data_snapshot (source, asset_key, fetched_at DESC)"
    )

    op.create_table(
        "prediction_history",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column("challenge_id", sa.Text(), nullable=False),
        sa.Column("asset_key", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column(
            "snapshot_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False,
            server_default="{}",
        ),
        sa.Column("model_output", postgresql.JSONB(), nullable=False),
        sa.Column("direction", sa.Text()),
        sa.Column("confidence", sa.Numeric()),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("outcome", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("actual_result", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "CREATE INDEX ix_prediction_history_embedding_hnsw "
        "ON prediction_history USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_prediction_history_persona_asset_created "
        "ON prediction_history (persona_id, asset_key, created_at DESC)"
    )

    op.create_table(
        "comment_history",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column("news_id", sa.Text(), nullable=False),
        sa.Column("comment_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_comment_history_embedding_hnsw "
        "ON comment_history USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "agent_lessons",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text()),
        sa.Column("asset_key", sa.Text()),
        sa.Column("strategy_scope", sa.Text()),
        sa.Column("lesson_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column(
            "supporting_prediction_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False, server_default="{}",
        ),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "superseded_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_lessons.id"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_agent_lessons_embedding_hnsw "
        "ON agent_lessons USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "backtest_reports",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_backtest_reports_persona_period "
        "ON backtest_reports (persona_id, period_start)"
    )

    op.create_table(
        "agent_credentials",
        sa.Column("persona_id", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("encrypted_client_secret", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_access_token", sa.LargeBinary()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "agent_processed_items",
        sa.Column("persona_id", sa.Text(), primary_key=True),
        sa.Column("item_type", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), primary_key=True),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "agent_llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("persona_id", sa.Text(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("cost_estimate", sa.Numeric()),
        sa.Column("request_type", sa.Text()),
    )
    op.execute(
        "CREATE INDEX ix_agent_llm_usage_persona_ts "
        "ON agent_llm_usage (persona_id, ts)"
    )


def downgrade() -> None:
    op.drop_table("agent_llm_usage")
    op.drop_table("agent_processed_items")
    op.drop_table("agent_credentials")
    op.drop_table("backtest_reports")
    op.drop_table("agent_lessons")
    op.drop_table("comment_history")
    op.drop_table("prediction_history")
    op.drop_table("market_data_snapshot")
