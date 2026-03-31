import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = "sqlite:///./feed_builder_app.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
