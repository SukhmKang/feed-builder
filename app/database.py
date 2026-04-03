import uuid
import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

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
    notifications_enabled = Column(Boolean, default=False)
    poll_interval_hours = Column(Integer, default=24)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_polled_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class Article(Base):
    __tablename__ = "articles"

    id = Column(String, primary_key=True)  # "{feed_id}:{article_url_hash}"
    feed_id = Column(String)
    article_json = Column(Text)  # full normalized article dict
    passed = Column(Boolean)
    pipeline_result_json = Column(Text)  # PipelineResult for self-improvement
    fetched_at = Column(DateTime, default=datetime.utcnow)
    notified = Column(Boolean, default=False)


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


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    feed_id = Column(String)
    subscription_json = Column(Text)  # Web Push subscription object
    created_at = Column(DateTime, default=datetime.utcnow)


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
