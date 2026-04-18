import uuid
import os
import logging
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy import func

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "sqlite:///./feed_builder_app.db"


def _get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://") :]
    return database_url


DATABASE_URL = _get_database_url()
ENGINE_KWARGS = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    ENGINE_KWARGS["connect_args"] = {"check_same_thread": False}
else:
    ENGINE_KWARGS["pool_recycle"] = 1800

engine = create_engine(DATABASE_URL, **ENGINE_KWARGS)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Feed(Base):
    __tablename__ = "feeds"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, default="default")
    name = Column(String)
    topic = Column(String)
    status = Column(String, default="building")  # building|ready|error
    config_json = Column(Text)  # final_config as JSON
    agent_output_json = Column(Text)  # full PipelineAgentResult JSON
    poll_interval_hours = Column(Integer, default=24)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_polled_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feed_id = Column(String, nullable=False)
    version_number = Column(Integer, nullable=False)
    config_json = Column(Text, nullable=False)   # {sources, blocks, topic}
    is_active = Column(Boolean, default=False, nullable=False)
    has_been_replayed = Column(Boolean, default=False, nullable=False)
    label = Column(String, nullable=True)        # optional description e.g. "Added Reddit source"
    created_at = Column(DateTime, default=datetime.utcnow)


class Article(Base):
    __tablename__ = "articles"

    id = Column(String, primary_key=True)  # "{feed_id}:{article_url_hash}"
    feed_id = Column(String)
    article_json = Column(Text)  # full normalized article dict
    passed = Column(Boolean)
    pipeline_result_json = Column(Text)  # PipelineResult for self-improvement
    fetched_at = Column(DateTime, default=datetime.utcnow)
    notified = Column(Boolean, default=False)
    manual_verdict = Column(String, nullable=True)    # "passed" | "filtered" | null
    pipeline_version_id = Column(String, nullable=True)  # FK to pipeline_versions.id
    source_type = Column(String, nullable=True)
    source_url = Column(String, nullable=True)
    spec_source_type = Column(String, nullable=True)   # source spec "type" field (e.g. "reddit_subreddit")
    spec_source_feed = Column(String, nullable=True)   # source spec "feed" field (e.g. "artificial")


class Story(Base):
    __tablename__ = "stories"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feed_id = Column(String, nullable=False)
    title = Column(String, nullable=False, default="Untitled story")
    summary = Column(Text, nullable=False, default="")
    status = Column(String, nullable=False, default="active")  # active|merged
    canonical_article_id = Column(String, nullable=True)
    story_embedding_json = Column(Text, nullable=True)
    article_count = Column(Integer, nullable=False, default=0)
    first_published_at = Column(DateTime, nullable=True)
    last_published_at = Column(DateTime, nullable=True)
    provenance_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class StoryArticle(Base):
    __tablename__ = "story_articles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    story_id = Column(String, nullable=False)
    article_id = Column(String, nullable=False)
    is_representative = Column(Boolean, default=False)
    position = Column(Integer, default=0)
    decision_json = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)


class AuditResult(Base):
    __tablename__ = "audit_results"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feed_id = Column(String, nullable=False)
    audit_period_start = Column(DateTime, nullable=False)
    audit_period_end = Column(DateTime, nullable=False)
    status = Column(String, default="pending")  # pending|running|complete|error
    result_json = Column(Text, nullable=True)          # full AuditReport as JSON
    proposed_config_json = Column(Text, nullable=True) # proposed pipeline config JSON
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    pipeline_version_id = Column(String, nullable=True)  # FK to pipeline_versions.id


class FeedReport(Base):
    __tablename__ = "feed_reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feed_id = Column(String, nullable=False)
    date_from = Column(DateTime, nullable=False)
    date_to = Column(DateTime, nullable=False)
    story_count = Column(Integer, default=0)
    r2_key = Column(String, nullable=True)   # object key in the R2 bucket
    created_at = Column(DateTime, default=datetime.utcnow)


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations() -> None:
    """Apply additive schema migrations that create_all won't handle."""
    with engine.connect() as conn:
        for stmt, label in [
            ("ALTER TABLE articles ADD COLUMN manual_verdict TEXT", "articles.manual_verdict"),
            ("ALTER TABLE audit_results ADD COLUMN proposed_config_json TEXT", "audit_results.proposed_config_json"),
            ("ALTER TABLE articles ADD COLUMN pipeline_version_id TEXT", "articles.pipeline_version_id"),
            ("ALTER TABLE articles ADD COLUMN source_type TEXT", "articles.source_type"),
            ("ALTER TABLE articles ADD COLUMN source_url TEXT", "articles.source_url"),
            ("ALTER TABLE audit_results ADD COLUMN pipeline_version_id TEXT", "audit_results.pipeline_version_id"),
            ("ALTER TABLE pipeline_versions ADD COLUMN has_been_replayed BOOLEAN DEFAULT 0", "pipeline_versions.has_been_replayed"),
            ("ALTER TABLE articles ADD COLUMN spec_source_type TEXT", "articles.spec_source_type"),
            ("ALTER TABLE articles ADD COLUMN spec_source_feed TEXT", "articles.spec_source_feed"),
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
                logger.info("migration: added %s column", label)
            except Exception:
                pass


def create_pipeline_version(
    feed_id: str,
    config_json: str,
    db,
    *,
    label: str | None = None,
) -> "PipelineVersion":
    """Deactivate the current active version and create a new one. Does NOT commit."""
    db.query(PipelineVersion).filter(
        PipelineVersion.feed_id == feed_id,
        PipelineVersion.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)

    max_v = db.query(func.max(PipelineVersion.version_number)).filter(
        PipelineVersion.feed_id == feed_id
    ).scalar() or 0

    version = PipelineVersion(
        id=str(uuid.uuid4()),
        feed_id=feed_id,
        version_number=max_v + 1,
        config_json=config_json,
        is_active=True,
        label=label,
    )
    db.add(version)
    return version


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
