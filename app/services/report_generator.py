"""PDF report generation for a feed over a date range."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.database import Article, FeedReport, PipelineVersion, Story, StoryArticle
from app.pipeline.core import parse_article_datetime

logger = logging.getLogger(__name__)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_date(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        value = parse_article_datetime(value)
    if value is None:
        return ""
    return value.strftime("%b %-d, %Y")


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ── Story/article querying ────────────────────────────────────────────────────

def _get_floor_version_ids(db: Session, feed_id: str) -> list[str] | None:
    """Return the set of pipeline version IDs from the replay floor onward, or None if no replay."""
    floor = (
        db.query(PipelineVersion)
        .filter(
            PipelineVersion.feed_id == feed_id,
            PipelineVersion.has_been_replayed.is_(True),
        )
        .order_by(PipelineVersion.version_number.desc())
        .first()
    )
    if not floor:
        return None
    ids = [
        v.id
        for v in db.query(PipelineVersion).filter(
            PipelineVersion.feed_id == feed_id,
            PipelineVersion.version_number >= floor.version_number,
        ).all()
    ]
    return ids


def _load_story_articles(db: Session, story_id: str) -> list[dict[str, Any]]:
    links = db.query(StoryArticle).filter(StoryArticle.story_id == story_id).all()
    article_ids = [link.article_id for link in links]
    if not article_ids:
        return []
    records = db.query(Article).filter(Article.id.in_(article_ids)).all()
    payloads = []
    for record in records:
        if not record.article_json:
            continue
        try:
            payloads.append(json.loads(record.article_json))
        except Exception:
            continue
    payloads.sort(
        key=lambda p: parse_article_datetime(p.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return payloads


def query_stories_for_report(
    db: Session,
    feed_id: str,
    date_from: datetime,
    date_to: datetime,
) -> list[dict[str, Any]]:
    """Return story+articles dicts for the report, respecting the version floor and date range."""
    from sqlalchemy import exists

    date_from = _ensure_aware(date_from)
    date_to = _ensure_aware(date_to)

    floor_ids = _get_floor_version_ids(db, feed_id)

    query = db.query(Story).filter(Story.feed_id == feed_id, Story.status == "active")

    if floor_ids:
        query = query.filter(
            exists().where(
                (StoryArticle.story_id == Story.id)
                & (StoryArticle.article_id == Article.id)
                & Article.pipeline_version_id.in_(floor_ids)
            )
        )

    # Date range: story overlaps with [date_from, date_to]
    query = query.filter(
        Story.first_published_at <= date_to,
        Story.last_published_at >= date_from,
    )

    stories = query.order_by(Story.last_published_at.desc(), Story.updated_at.desc()).all()

    result = []
    for story in stories:
        articles = _load_story_articles(db, story.id)
        result.append(
            {
                "id": story.id,
                "title": story.title,
                "summary": story.summary,
                "article_count": story.article_count,
                "first_published_at": story.first_published_at,
                "last_published_at": story.last_published_at,
                "articles": articles,
            }
        )
    return result


# ── PDF generation ────────────────────────────────────────────────────────────

def build_pdf(
    *,
    feed_name: str,
    feed_topic: str,
    date_from: datetime,
    date_to: datetime,
    stories: list[dict[str, Any]],
) -> bytes:
    """Render stories+articles into a PDF and return the raw bytes."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=22 * mm,
    )

    base = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────────────────────
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=base["Heading1"],
        fontSize=22,
        leading=28,
        spaceAfter=4,
        textColor=colors.HexColor("#1a1a2e"),
    )
    meta_style = ParagraphStyle(
        "Meta",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#6e6e73"),
        spaceAfter=2,
    )
    story_heading_style = ParagraphStyle(
        "StoryHeading",
        parent=base["Normal"],
        fontSize=13,
        leading=18,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#1558D6"),
        spaceAfter=3,
        spaceBefore=10,
    )
    story_dates_style = ParagraphStyle(
        "StoryDates",
        parent=base["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#70757A"),
        spaceAfter=4,
    )
    summary_style = ParagraphStyle(
        "Summary",
        parent=base["Normal"],
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#3C4043"),
        spaceAfter=6,
    )
    article_title_style = ParagraphStyle(
        "ArticleTitle",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#202124"),
        leftIndent=10,
    )
    article_meta_style = ParagraphStyle(
        "ArticleMeta",
        parent=base["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#70757A"),
        leftIndent=10,
        spaceAfter=5,
    )
    article_url_style = ParagraphStyle(
        "ArticleUrl",
        parent=base["Normal"],
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1558D6"),
        leftIndent=10,
        spaceAfter=7,
    )

    story_count = len(stories)
    article_count = sum(len(s["articles"]) for s in stories)
    period_str = f"{_fmt_date(date_from)} – {_fmt_date(date_to)}"
    generated_str = _utcnow().strftime("%b %-d, %Y at %H:%M UTC")

    story_word = "story" if story_count == 1 else "stories"
    article_word = "article" if article_count == 1 else "articles"

    story = []

    # ── Document header ────────────────────────────────────────────────────────
    story.append(Paragraph(f"{feed_name} — Report", title_style))
    story.append(Paragraph(f"<b>Topic:</b> {_esc(feed_topic)}", meta_style))
    story.append(Paragraph(f"<b>Period:</b> {period_str}", meta_style))
    story.append(Paragraph(
        f"<b>Contents:</b> {story_count} {story_word}, {article_count} {article_word}",
        meta_style,
    ))
    story.append(Paragraph(f"<b>Generated:</b> {generated_str}", meta_style))
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1558D6")))
    story.append(Spacer(1, 4 * mm))

    if not stories:
        story.append(Paragraph("No stories found in this date range.", summary_style))
    else:
        for s in stories:
            # Story heading
            story.append(Paragraph(_esc(s["title"]), story_heading_style))

            first = _fmt_date(s["first_published_at"])
            last = _fmt_date(s["last_published_at"])
            date_label = first if first == last else f"{first} – {last}" if first and last else first or last
            story.append(Paragraph(date_label, story_dates_style))

            if s.get("summary"):
                story.append(Paragraph(_esc(s["summary"]), summary_style))

            for art in s["articles"]:
                title = str(art.get("title") or "Untitled")
                url = str(art.get("url") or "")
                source = str(art.get("source_name") or "")
                pub = _fmt_date(art.get("published_at"))
                meta_parts = [p for p in [source, pub] if p]
                meta_line = " · ".join(meta_parts)

                story.append(Paragraph(f"• {_esc(title)}", article_title_style))
                if meta_line:
                    story.append(Paragraph(_esc(meta_line), article_meta_style))
                if url:
                    story.append(Paragraph(
                        f'<link href="{url}">{_esc(url)}</link>',
                        article_url_style,
                    ))

            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E8EAED")))
            story.append(Spacer(1, 2 * mm))

    doc.build(story)
    return buf.getvalue()


def _esc(text: str) -> str:
    """Escape characters that would break ReportLab XML."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
