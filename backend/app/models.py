"""SQLAlchemy ORM models. All timestamps are stored in UTC (timestamptz).

Demo and live data never mix: companies are keyed by (ticker, data_mode) and every
child row follows its company; free-standing rows (news, events, runs) carry data_mode.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


TS = DateTime(timezone=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ticker", "data_mode", name="uq_company_ticker_mode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    data_mode: Mapped[str] = mapped_column(String(8), default="live")
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(32))
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(255))
    sic_code: Mapped[str | None] = mapped_column(String(8))
    currency: Mapped[str | None] = mapped_column(String(8))
    market_cap: Mapped[float | None] = mapped_column(Float)
    shares_outstanding: Mapped[float | None] = mapped_column(Float)
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))  # MMDD
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    profile_source: Mapped[str | None] = mapped_column(String(64))
    profile_updated_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class StockPrice(Base):
    """Raw (unadjusted) daily bars. Split adjustment is applied at read time so that
    later splits never leave stale adjusted history in the table."""

    __tablename__ = "stock_prices"
    __table_args__ = (
        UniqueConstraint("company_id", "trade_date", name="uq_price_company_date"),
        Index("ix_price_company_date", "company_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    trade_date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class MarketDataRevision(Base):
    __tablename__ = "market_data_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    field: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[float | None] = mapped_column(Float)
    new_value: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("company_id", "action_type", "ex_date", name="uq_corp_action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(16))  # split | dividend
    ex_date: Mapped[date] = mapped_column(Date)
    split_from: Mapped[float | None] = mapped_column(Float)
    split_to: Mapped[float | None] = mapped_column(Float)
    cash_amount: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class FinancialStatement(Base):
    """Normalized, long-format financial facts with full SEC provenance.

    One row per (metric, period, accession). Restatements produce additional rows with
    later filed dates, which enables point-in-time ("as filed") queries for backtests.
    """

    __tablename__ = "financial_statements"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "metric", "period_type", "period_start", "period_end", "accession",
            name="uq_fin_fact",
        ),
        Index("ix_fin_company_metric", "company_id", "metric", "period_type", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(48))
    period_type: Mapped[str] = mapped_column(String(10))  # annual | quarterly | instant
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))  # FY, Q1..Q4
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24))
    concept: Mapped[str] = mapped_column(String(160))
    taxonomy: Mapped[str] = mapped_column(String(16))
    accession: Mapped[str] = mapped_column(String(32))
    form: Mapped[str | None] = mapped_column(String(16))
    filed_date: Mapped[date] = mapped_column(Date)
    derived: Mapped[bool] = mapped_column(Boolean, default=False)
    derivation: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(32), default="sec_edgar")
    ingested_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class SourceReference(Base):
    __tablename__ = "source_references"
    __table_args__ = (UniqueConstraint("data_mode", "source_type", "url", name="uq_source_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    source_type: Mapped[str] = mapped_column(String(32))  # news | sec_filing | sec_xbrl | market_data | rss | calendar
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(TS)
    accession: Mapped[str | None] = mapped_column(String(32))
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("data_mode", "fingerprint", name="uq_event_fingerprint"),
        Index("ix_event_mode_published", "data_mode", "last_published_at"),
        Index("ix_event_mode_date", "data_mode", "event_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(32))
    # confirmed | unconfirmed (rumor/report) | scheduled | estimated
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    impact: Mapped[str] = mapped_column(String(16), default="uncertain")  # positive | negative | uncertain
    impact_explanation: Mapped[str | None] = mapped_column(Text)
    materiality: Mapped[float | None] = mapped_column(Float)
    source_name: Mapped[str | None] = mapped_column(String(255))
    primary_url: Mapped[str | None] = mapped_column(Text)
    first_published_at: Mapped[datetime | None] = mapped_column(TS)
    last_published_at: Mapped[datetime | None] = mapped_column(TS)
    event_date: Mapped[date | None] = mapped_column(Date)
    article_count: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    ai_processed_hash: Mapped[str | None] = mapped_column(String(64))
    ai_analysis: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    companies: Mapped[list[EventCompanyRelationship]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("data_mode", "canonical_url", name="uq_article_url"),
        Index("ix_article_mode_published", "data_mode", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data_mode: Mapped[str] = mapped_column(String(8))
    canonical_url: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(TS)
    ingested_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64))
    tickers: Mapped[Any | None] = mapped_column(JSONB)
    is_rumor: Mapped[bool] = mapped_column(Boolean, default=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), index=True)


class EventCompanyRelationship(Base):
    __tablename__ = "event_company_relationships"
    __table_args__ = (UniqueConstraint("event_id", "company_id", name="uq_event_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(32))  # provider_tag | exchange_ticker | cashtag | name_match | sec_cik | ai
    impact: Mapped[str] = mapped_column(String(16), default="uncertain")
    explanation: Mapped[str | None] = mapped_column(Text)

    event: Mapped[Event] = relationship(back_populates="companies")
    company: Mapped[Company] = relationship()


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analysis_company_created", "company_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    data_mode: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    input_hash: Mapped[str] = mapped_column(String(64))
    observed_price: Mapped[float | None] = mapped_column(Float)
    observed_price_date: Mapped[date | None] = mapped_column(Date)
    ai_kind: Mapped[str] = mapped_column(String(16))  # openai | rule_based | demo
    ai_model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    thesis: Mapped[Any] = mapped_column(JSONB)
    snapshot: Mapped[Any] = mapped_column(JSONB)  # key metrics used, for diffing vs. previous analysis
    usage: Mapped[Any | None] = mapped_column(JSONB)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    financial_data_version: Mapped[str | None] = mapped_column(String(64))
    forecast_model_version: Mapped[str] = mapped_column(String(32))
    warnings: Mapped[Any | None] = mapped_column(JSONB)

    company: Mapped[Company] = relationship()
    sources: Mapped[list[AnalysisSource]] = relationship(cascade="all, delete-orphan")


class AnalysisSource(Base):
    __tablename__ = "analysis_sources"
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_references.id", ondelete="CASCADE"), primary_key=True)
    label: Mapped[str] = mapped_column(String(16))  # e.g. S3 — the citation key shown to the model/user

    source: Mapped[SourceReference] = relationship()


class Forecast(Base):
    """Immutable forecast record (UPDATE/DELETE blocked by a database trigger)."""

    __tablename__ = "forecasts"
    __table_args__ = (Index("ix_forecast_company_created", "company_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id", ondelete="RESTRICT"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    data_mode: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    horizon_months: Mapped[int] = mapped_column(Integer)
    target_date: Mapped[date] = mapped_column(Date)
    observed_price: Mapped[float] = mapped_column(Float)
    observed_price_date: Mapped[date] = mapped_column(Date)
    valuation_methods: Mapped[Any] = mapped_column(JSONB)
    assumptions: Mapped[Any] = mapped_column(JSONB)
    sensitivity: Mapped[Any | None] = mapped_column(JSONB)
    financial_data_version: Mapped[str | None] = mapped_column(String(64))
    news_source_ids: Mapped[Any | None] = mapped_column(JSONB)
    ai_model: Mapped[str | None] = mapped_column(String(64))
    forecast_model_version: Mapped[str] = mapped_column(String(32))

    scenarios: Mapped[list[ForecastScenario]] = relationship(
        cascade="save-update, merge", order_by="ForecastScenario.id", lazy="selectin"
    )


class ForecastScenario(Base):
    __tablename__ = "forecast_scenarios"
    __table_args__ = (UniqueConstraint("forecast_id", "scenario", name="uq_forecast_scenario"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id", ondelete="RESTRICT"))
    scenario: Mapped[str] = mapped_column(String(8))  # bear | base | bull
    revenue: Mapped[float | None] = mapped_column(Float)
    revenue_growth: Mapped[float | None] = mapped_column(Float)
    operating_margin: Mapped[float | None] = mapped_column(Float)
    net_margin: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    pe_multiple: Mapped[float | None] = mapped_column(Float)
    target_price_pe: Mapped[float | None] = mapped_column(Float)
    target_price_dcf: Mapped[float | None] = mapped_column(Float)
    target_price_other: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    potential_return: Mapped[float | None] = mapped_column(Float)
    explanation: Mapped[str | None] = mapped_column(Text)
    details: Mapped[Any | None] = mapped_column(JSONB)


class WatchlistItem(Base):
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True)
    added_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship()


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        Index("ix_job_status_created", "status", "created_at"),
        Index(
            "uq_job_dedupe_key", "dedupe_key", unique=True, postgresql_where="dedupe_key IS NOT NULL"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | succeeded | failed
    data_mode: Mapped[str] = mapped_column(String(8))
    payload: Mapped[Any] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS)  # refreshed by the worker while running


class ProviderSyncHistory(Base):
    __tablename__ = "provider_sync_history"
    __table_args__ = (Index("ix_sync_provider_started", "provider", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    records: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class HttpCache(Base):
    __tablename__ = "http_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(Text)
    response: Mapped[Any] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS, index=True)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(48))
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    response: Mapped[Any | None] = mapped_column(JSONB)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_mode: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    universe_size: Mapped[int | None] = mapped_column(Integer)
    candidates_evaluated: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[Any | None] = mapped_column(JSONB)
    notes: Mapped[Any | None] = mapped_column(JSONB)
    seed: Mapped[str | None] = mapped_column(String(32))  # tie-break rotation seed (reproducible)
    stats: Mapped[Any | None] = mapped_column(JSONB)  # coverage / diversity summary

    recommendations: Mapped[list[Recommendation]] = relationship(order_by="Recommendation.rank")


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("run_id", "company_id", name="uq_rec_run_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="SET NULL"))
    forecast_id: Mapped[int | None] = mapped_column(ForeignKey("forecasts.id", ondelete="SET NULL"))
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    score_band: Mapped[str] = mapped_column(String(16))
    factors: Mapped[Any] = mapped_column(JSONB)
    outlook: Mapped[str] = mapped_column(String(16))
    risk_rating: Mapped[str] = mapped_column(String(16))
    horizon_class: Mapped[str] = mapped_column(String(16))
    catalyst_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    catalyst_title: Mapped[str | None] = mapped_column(Text)
    catalyst_date: Mapped[date | None] = mapped_column(Date)
    selection_pathway: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    company: Mapped[Company] = relationship()


class CompanyBaseline(Base):
    """Pre-selection snapshot for EVERY universe stock, refreshed independently of analysis, so
    candidate selection never depends on which stocks happened to be analyzed before."""

    __tablename__ = "company_baselines"

    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    data_mode: Mapped[str] = mapped_column(String(8), index=True)
    price: Mapped[float | None] = mapped_column(Float)
    price_date: Mapped[date | None] = mapped_column(Date)
    avg_dollar_volume_20d: Mapped[float | None] = mapped_column(Float)
    price_sessions: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[Any | None] = mapped_column(JSONB)  # revenue, growth, margins, net cash ...
    financials_as_of: Mapped[date | None] = mapped_column(Date)  # latest SEC filing date used
    financials_fetched_at: Mapped[datetime | None] = mapped_column(TS)
    news_checked_at: Mapped[datetime | None] = mapped_column(TS)  # company-specific news coverage
    news_window_start: Mapped[datetime | None] = mapped_column(TS)
    ticker_news_checked_at: Mapped[datetime | None] = mapped_column(TS)  # per-ticker news rotation
    filings_checked_at: Mapped[datetime | None] = mapped_column(TS)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    ineligible_reason: Mapped[str | None] = mapped_column(Text)
    errors: Mapped[Any | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    company: Mapped[Company] = relationship()


class DiscoveryCandidate(Base):
    """One row per universe stock per discovery run: why it was (not) selected, through which
    pathway(s), the data timestamps it was judged on, its score factors and exclusion reason."""

    __tablename__ = "discovery_candidates"
    __table_args__ = (UniqueConstraint("run_id", "company_id", name="uq_candidate_run_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    ticker: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # ineligible | not_selected | excluded | screened | ranked
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_order: Mapped[int | None] = mapped_column(Integer)
    pathway: Mapped[str | None] = mapped_column(String(24))  # pathway that filled the slot
    qualifying_pathways: Mapped[Any | None] = mapped_column(JSONB)  # every pathway it qualified for
    baseline_score: Mapped[float | None] = mapped_column(Float)
    catalyst_status: Mapped[str | None] = mapped_column(String(16))  # detected | none_detected | unknown
    catalyst_score: Mapped[float | None] = mapped_column(Float)
    previously_analyzed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(TS)
    tie_break_key: Mapped[str | None] = mapped_column(String(16))
    data_timestamps: Mapped[Any | None] = mapped_column(JSONB)
    score: Mapped[float | None] = mapped_column(Float)
    factors: Mapped[Any | None] = mapped_column(JSONB)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="SET NULL"))
    final_rank: Mapped[int | None] = mapped_column(Integer)
