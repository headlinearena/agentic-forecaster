from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class MarketDataSnapshot(Base):
    __tablename__ = "market_data_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    asset_key: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PredictionHistory(Base):
    __tablename__ = "prediction_history"

    id: Mapped[object] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    challenge_id: Mapped[str] = mapped_column(Text, nullable=False)
    asset_key: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_ids: Mapped[list] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default="{}"
    )
    model_output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    direction: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))
    outcome: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    actual_result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    settled_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class CommentHistory(Base):
    __tablename__ = "comment_history"

    id: Mapped[object] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    news_id: Mapped[str] = mapped_column(Text, nullable=False)
    comment_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentLesson(Base):
    __tablename__ = "agent_lessons"

    id: Mapped[object] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    persona_id: Mapped[str | None] = mapped_column(Text)
    asset_key: Mapped[str | None] = mapped_column(Text)
    strategy_scope: Mapped[str | None] = mapped_column(Text)
    lesson_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))
    supporting_prediction_ids: Mapped[list] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    superseded_by: Mapped[object | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_lessons.id")
    )


class BacktestReport(Base):
    __tablename__ = "backtest_reports"

    id: Mapped[object] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[object] = mapped_column(Date, nullable=False)
    period_end: Mapped[object] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    persona_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_client_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    granted_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    last_cycle_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentProcessedItem(Base):
    __tablename__ = "agent_processed_items"

    persona_id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_type: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentLlmUsage(Base):
    __tablename__ = "agent_llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_estimate: Mapped[float | None] = mapped_column(Numeric)
    request_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="success")
    model_provider: Mapped[str | None] = mapped_column(Text)
    deployment: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
