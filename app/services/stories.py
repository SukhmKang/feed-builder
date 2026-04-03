"""Story clustering and summarization for passed articles."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.ai.llm import generate_text
from app.database import Article, Story, StoryArticle
from app.pipeline.core import cosine_similarity, embed_text, parse_article_datetime
from app.pipeline.llm_config import resolve_tier_model

logger = logging.getLogger(__name__)

STORY_EMBEDDING_MODEL = "text-embedding-3-small"
STORY_CANDIDATE_LOOKBACK_DAYS = 7
STORY_MIN_SHORTLIST_SIMILARITY = 0.55
STORY_MAX_SHORTLIST_SIZE = 4
STORY_DECISION_TIER = "high"
STORY_SUMMARY_TIER = "medium"
STORY_DECISION_MAX_TOKENS = 1200
STORY_SUMMARY_MAX_TOKENS = 800


@dataclass(slots=True)
class StoryCandidate:
    story: Story
    similarity: float
    representative_article: dict[str, Any]
    recent_articles: list[dict[str, Any]]


async def assign_article_to_story(
    db: Session,
    *,
    feed_id: str,
    article_record: Article,
    article_payload: dict[str, Any],
) -> Story:
    """Create, update, or merge stories for a newly passed article."""

    article_embedding = await _embed_article(article_payload)
    article_published_at = parse_article_datetime(article_payload.get("published_at")) or _utcnow()
    candidates = await _find_story_candidates(
        db,
        feed_id=feed_id,
        article=article_payload,
        article_embedding=article_embedding,
        article_published_at=article_published_at,
    )

    logger.info(
        "Story assignment start feed_id=%s article_id=%s candidate_count=%s candidate_ids=%s",
        feed_id,
        article_record.id,
        len(candidates),
        [candidate.story.id for candidate in candidates],
    )

    decision = await _decide_story_action(
        article=article_payload,
        candidates=candidates,
    )
    logger.info(
        "Story assignment decision feed_id=%s article_id=%s action=%s payload=%s",
        feed_id,
        article_record.id,
        decision.get("action"),
        decision,
    )

    action = str(decision.get("action", "")).strip()
    if action == "attach":
        story = _find_candidate_story(candidates, str(decision.get("story_id", "")).strip())
        if story is None:
            logger.warning(
                "Story assignment returned unknown attach target; creating new story article_id=%s target=%r",
                article_record.id,
                decision.get("story_id"),
            )
            story = _create_story(db, feed_id=feed_id, article_id=article_record.id)
        _add_article_to_story(
            db,
            story=story,
            article_record=article_record,
            decision={
                "action": "attach",
                "reasoning": str(decision.get("reasoning", "")).strip(),
                "similarity_candidates": [
                    {
                        "story_id": candidate.story.id,
                        "similarity": round(candidate.similarity, 4),
                    }
                    for candidate in candidates
                ],
            },
        )
        _refresh_story_rollup(
            db,
            story=story,
            new_embedding=article_embedding,
            provenance={"action": "attach", "decision": decision},
        )
        await _refresh_story_summary(db, story)
        db.commit()
        db.refresh(story)
        return story

    if action == "merge":
        primary_story = _find_candidate_story(candidates, str(decision.get("primary_story_id", "")).strip())
        secondary_story = _find_candidate_story(candidates, str(decision.get("secondary_story_id", "")).strip())
        if primary_story is None or secondary_story is None or primary_story.id == secondary_story.id:
            logger.warning(
                "Story merge decision was invalid; creating new story article_id=%s decision=%s",
                article_record.id,
                decision,
            )
            story = _create_story(db, feed_id=feed_id, article_id=article_record.id)
            _add_article_to_story(
                db,
                story=story,
                article_record=article_record,
                decision={"action": "create_new", "reasoning": str(decision.get("reasoning", "")).strip()},
            )
            _refresh_story_rollup(
                db,
                story=story,
                new_embedding=article_embedding,
                provenance={"action": "create_new_fallback", "decision": decision},
            )
            await _refresh_story_summary(db, story)
            db.commit()
            db.refresh(story)
            return story

        _merge_stories(
            db,
            primary_story=primary_story,
            secondary_story=secondary_story,
            merge_reasoning=str(decision.get("reasoning", "")).strip(),
        )
        _add_article_to_story(
            db,
            story=primary_story,
            article_record=article_record,
            decision={"action": "merge_attach", "reasoning": str(decision.get("reasoning", "")).strip()},
        )
        _refresh_story_rollup(
            db,
            story=primary_story,
            new_embedding=article_embedding,
            provenance={"action": "merge", "decision": decision},
        )
        await _refresh_story_summary(db, primary_story)
        db.commit()
        db.refresh(primary_story)
        return primary_story

    story = _create_story(db, feed_id=feed_id, article_id=article_record.id)
    _add_article_to_story(
        db,
        story=story,
        article_record=article_record,
        decision={"action": "create_new", "reasoning": str(decision.get("reasoning", "")).strip()},
    )
    _refresh_story_rollup(
        db,
        story=story,
        new_embedding=article_embedding,
        provenance={"action": "create_new", "decision": decision},
    )
    await _refresh_story_summary(db, story)
    db.commit()
    db.refresh(story)
    return story


async def _find_story_candidates(
    db: Session,
    *,
    feed_id: str,
    article: dict[str, Any],
    article_embedding: list[float],
    article_published_at: datetime,
) -> list[StoryCandidate]:
    cutoff = article_published_at - timedelta(days=STORY_CANDIDATE_LOOKBACK_DAYS)
    stories = (
        db.query(Story)
        .filter(Story.feed_id == feed_id, Story.status == "active")
        .filter(Story.last_published_at.is_(None) | (Story.last_published_at >= cutoff))
        .all()
    )

    candidates: list[StoryCandidate] = []
    for story in stories:
        story_embedding = _load_embedding(story.story_embedding_json)
        if not story_embedding:
            continue
        try:
            similarity = cosine_similarity(article_embedding, story_embedding)
        except ValueError:
            continue
        if similarity < STORY_MIN_SHORTLIST_SIMILARITY:
            continue
        representative_article = _load_article_payload(db, story.canonical_article_id)
        recent_articles = _load_story_member_articles(db, story.id, limit=3)
        candidates.append(
            StoryCandidate(
                story=story,
                similarity=similarity,
                representative_article=representative_article,
                recent_articles=recent_articles,
            )
        )

    candidates.sort(key=lambda item: item.similarity, reverse=True)
    return candidates[:STORY_MAX_SHORTLIST_SIZE]


async def _decide_story_action(
    *,
    article: dict[str, Any],
    candidates: list[StoryCandidate],
) -> dict[str, Any]:
    if not candidates:
        return {"action": "create_new", "reasoning": "No recent similar active stories were available."}

    provider, model = resolve_tier_model(STORY_DECISION_TIER)
    prompt = _build_story_decision_prompt(article=article, candidates=candidates)
    raw = await generate_text(
        prompt,
        provider=provider,
        model=model,
        max_completion_tokens=STORY_DECISION_MAX_TOKENS,
        json_output=True,
    )
    decision = _validate_story_decision(raw, candidate_ids=[candidate.story.id for candidate in candidates])
    return decision


def _build_story_decision_prompt(*, article: dict[str, Any], candidates: list[StoryCandidate]) -> str:
    candidates_payload = []
    for candidate in candidates:
        candidates_payload.append(
            {
                "story_id": candidate.story.id,
                "title": candidate.story.title,
                "summary": candidate.story.summary,
                "article_count": candidate.story.article_count,
                "first_published_at": _iso(candidate.story.first_published_at),
                "last_published_at": _iso(candidate.story.last_published_at),
                "similarity": round(candidate.similarity, 4),
                "representative_article": {
                    "title": candidate.representative_article.get("title"),
                    "source_name": candidate.representative_article.get("source_name"),
                    "published_at": candidate.representative_article.get("published_at"),
                    "content": _truncate(candidate.representative_article.get("content"), 500),
                },
                "recent_articles": [
                    {
                        "title": member.get("title"),
                        "source_name": member.get("source_name"),
                        "published_at": member.get("published_at"),
                        "content": _truncate(member.get("content"), 300),
                    }
                    for member in candidate.recent_articles
                ],
            }
        )

    article_payload = {
        "id": article.get("id"),
        "title": article.get("title"),
        "source_name": article.get("source_name"),
        "source_type": article.get("source_type"),
        "published_at": article.get("published_at"),
        "content": _truncate(article.get("content"), 900),
        "full_text": _truncate(article.get("full_text"), 900),
        "tags": article.get("tags", []),
        "url": article.get("url"),
    }
    schema = {
        "action": "create_new | attach | merge",
        "story_id": "required for attach",
        "primary_story_id": "required for merge",
        "secondary_story_id": "required for merge",
        "reasoning": "brief explanation",
    }
    return (
        "You are clustering passed feed articles into persistent stories.\n"
        "Decide whether the incoming article should create a new story, attach to one existing story, "
        "or merge two existing stories and include the new article.\n"
        "Be conservative: only merge when they are clearly the same ongoing event/storyline.\n\n"
        f"Incoming article:\n{json.dumps(article_payload, ensure_ascii=True, indent=2)}\n\n"
        f"Candidate stories:\n{json.dumps(candidates_payload, ensure_ascii=True, indent=2)}\n\n"
        "Return JSON only using this schema:\n"
        f"{json.dumps(schema, ensure_ascii=True, indent=2)}\n"
    )


def _validate_story_decision(raw: str, *, candidate_ids: list[str]) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Story decision returned invalid JSON: {exc}") from exc

    action = str(parsed.get("action", "")).strip()
    if action not in {"create_new", "attach", "merge"}:
        raise ValueError("Story decision action must be create_new, attach, or merge")
    if action == "attach":
        story_id = str(parsed.get("story_id", "")).strip()
        if story_id not in candidate_ids:
            raise ValueError("Story decision attach target must be one of the candidate story ids")
    if action == "merge":
        primary = str(parsed.get("primary_story_id", "")).strip()
        secondary = str(parsed.get("secondary_story_id", "")).strip()
        if primary not in candidate_ids or secondary not in candidate_ids or primary == secondary:
            raise ValueError("Story decision merge targets must be two distinct candidate story ids")

    return {
        "action": action,
        "story_id": str(parsed.get("story_id", "")).strip() or None,
        "primary_story_id": str(parsed.get("primary_story_id", "")).strip() or None,
        "secondary_story_id": str(parsed.get("secondary_story_id", "")).strip() or None,
        "reasoning": str(parsed.get("reasoning", "")).strip(),
    }


def _create_story(db: Session, *, feed_id: str, article_id: str) -> Story:
    story = Story(
        id=str(uuid.uuid4()),
        feed_id=feed_id,
        status="active",
        canonical_article_id=article_id,
        title="Draft story",
        summary="",
        article_count=0,
        provenance_json=json.dumps({"created_from_article_id": article_id}, ensure_ascii=True),
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(story)
    db.flush()
    return story


def _add_article_to_story(
    db: Session,
    *,
    story: Story,
    article_record: Article,
    decision: dict[str, Any],
) -> None:
    existing = (
        db.query(StoryArticle)
        .filter(StoryArticle.story_id == story.id, StoryArticle.article_id == article_record.id)
        .first()
    )
    if existing is not None:
        return

    link = StoryArticle(
        story_id=story.id,
        article_id=article_record.id,
        is_representative=False,
        position=0,
        decision_json=json.dumps(decision, ensure_ascii=True),
        added_at=_utcnow(),
    )
    db.add(link)
    db.flush()


def _merge_stories(
    db: Session,
    *,
    primary_story: Story,
    secondary_story: Story,
    merge_reasoning: str,
) -> None:
    secondary_links = db.query(StoryArticle).filter(StoryArticle.story_id == secondary_story.id).all()
    for link in secondary_links:
        existing = (
            db.query(StoryArticle)
            .filter(StoryArticle.story_id == primary_story.id, StoryArticle.article_id == link.article_id)
            .first()
        )
        if existing is None:
            db.add(
                StoryArticle(
                    story_id=primary_story.id,
                    article_id=link.article_id,
                    is_representative=False,
                    position=0,
                    decision_json=json.dumps(
                        {
                            "action": "story_merge",
                            "merged_from_story_id": secondary_story.id,
                            "reasoning": merge_reasoning,
                        },
                        ensure_ascii=True,
                    ),
                    added_at=_utcnow(),
                )
            )
    secondary_story.status = "merged"
    secondary_story.updated_at = _utcnow()
    secondary_story.provenance_json = json.dumps(
        {
            "action": "merged_into",
            "primary_story_id": primary_story.id,
            "reasoning": merge_reasoning,
        },
        ensure_ascii=True,
    )
    db.flush()


def _refresh_story_rollup(
    db: Session,
    *,
    story: Story,
    new_embedding: list[float],
    provenance: dict[str, Any],
) -> None:
    member_articles = _load_story_article_records(db, story.id)
    member_payloads = [
        json.loads(article.article_json)
        for article in member_articles
        if article.article_json
    ]
    published_times = [
        parse_article_datetime(payload.get("published_at"))
        for payload in member_payloads
    ]
    normalized_times = [value for value in published_times if value is not None]

    story.article_count = len(member_articles)
    story.updated_at = _utcnow()
    story.first_published_at = min(normalized_times) if normalized_times else None
    story.last_published_at = max(normalized_times) if normalized_times else None
    story.provenance_json = json.dumps(provenance, ensure_ascii=True)

    if story.article_count <= 1:
        story.story_embedding_json = json.dumps(new_embedding, ensure_ascii=True)
    else:
        current_embedding = _load_embedding(story.story_embedding_json)
        if current_embedding and len(current_embedding) == len(new_embedding):
            weight = max(story.article_count - 1, 1)
            averaged = [
                ((left * weight) + right) / (weight + 1)
                for left, right in zip(current_embedding, new_embedding)
            ]
            story.story_embedding_json = json.dumps(averaged, ensure_ascii=True)
        else:
            story.story_embedding_json = json.dumps(new_embedding, ensure_ascii=True)

    representative = _select_representative_article(member_articles)
    story.canonical_article_id = representative.id if representative is not None else story.canonical_article_id
    db.query(StoryArticle).filter(StoryArticle.story_id == story.id).update({StoryArticle.is_representative: False})
    if representative is not None:
        (
            db.query(StoryArticle)
            .filter(StoryArticle.story_id == story.id, StoryArticle.article_id == representative.id)
            .update({StoryArticle.is_representative: True})
        )
    db.flush()


async def _refresh_story_summary(db: Session, story: Story) -> None:
    member_articles = _load_story_member_articles(db, story.id, limit=6)
    if not member_articles:
        return

    provider, model = resolve_tier_model(STORY_SUMMARY_TIER)
    prompt = _build_story_summary_prompt(member_articles)
    raw = await generate_text(
        prompt,
        provider=provider,
        model=model,
        max_completion_tokens=STORY_SUMMARY_MAX_TOKENS,
        json_output=True,
    )
    summary_payload = _validate_story_summary(raw)
    story.title = summary_payload["title"]
    story.summary = summary_payload["summary"]
    story.updated_at = _utcnow()
    db.flush()


def _build_story_summary_prompt(member_articles: list[dict[str, Any]]) -> str:
    payload = [
        {
            "title": article.get("title"),
            "source_name": article.get("source_name"),
            "source_type": article.get("source_type"),
            "published_at": article.get("published_at"),
            "content": _truncate(article.get("content"), 500),
            "full_text": _truncate(article.get("full_text"), 500),
            "url": article.get("url"),
        }
        for article in member_articles
    ]
    schema = {
        "title": "short story title, 6-14 words",
        "summary": "2-3 sentence summary of the current story arc",
    }
    return (
        "You are writing a concise title and summary for a grouped news story based on related articles.\n"
        "Use neutral, factual language. Focus on the shared event or storyline, not any single source.\n\n"
        f"Story member articles:\n{json.dumps(payload, ensure_ascii=True, indent=2)}\n\n"
        "Return JSON only using this schema:\n"
        f"{json.dumps(schema, ensure_ascii=True, indent=2)}\n"
    )


def _validate_story_summary(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Story summary returned invalid JSON: {exc}") from exc
    title = str(parsed.get("title", "")).strip()
    summary = str(parsed.get("summary", "")).strip()
    if not title:
        raise ValueError("Story summary title must be non-empty")
    if not summary:
        raise ValueError("Story summary summary must be non-empty")
    return {"title": title[:160], "summary": summary[:1200]}


def serialize_story_summary(db: Session, story: Story) -> dict[str, Any]:
    canonical_article = _load_article_payload(db, story.canonical_article_id)
    return {
        "id": story.id,
        "feed_id": story.feed_id,
        "title": story.title,
        "summary": story.summary,
        "status": story.status,
        "canonical_article_id": story.canonical_article_id,
        "article_count": story.article_count,
        "first_published_at": _iso(story.first_published_at),
        "last_published_at": _iso(story.last_published_at),
        "created_at": _iso(story.created_at),
        "updated_at": _iso(story.updated_at),
        "provenance": _load_json(story.provenance_json),
        "representative_article": canonical_article,
    }


def serialize_story_detail(db: Session, story: Story) -> dict[str, Any]:
    articles = _load_story_member_articles(db, story.id, limit=None)
    return {
        **serialize_story_summary(db, story),
        "articles": articles,
    }


def _load_story_member_articles(db: Session, story_id: str, limit: int | None) -> list[dict[str, Any]]:
    records = _load_story_article_records(db, story_id)
    articles = []
    for record in records:
        if not record.article_json:
            continue
        payload = json.loads(record.article_json)
        payload["_record_id"] = record.id
        articles.append(payload)
    articles.sort(
        key=lambda item: parse_article_datetime(item.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if limit is None:
        return articles
    return articles[:limit]


def _load_story_article_records(db: Session, story_id: str) -> list[Article]:
    links = db.query(StoryArticle).filter(StoryArticle.story_id == story_id).all()
    article_ids = [link.article_id for link in links]
    if not article_ids:
        return []
    records = db.query(Article).filter(Article.id.in_(article_ids)).all()
    record_by_id = {record.id: record for record in records}
    return [record_by_id[article_id] for article_id in article_ids if article_id in record_by_id]


def _load_article_payload(db: Session, article_id: str | None) -> dict[str, Any]:
    if not article_id:
        return {}
    record = db.get(Article, article_id)
    if record is None or not record.article_json:
        return {}
    return json.loads(record.article_json)


def _select_representative_article(records: list[Article]) -> Article | None:
    if not records:
        return None
    return max(
        records,
        key=lambda record: parse_article_datetime(json.loads(record.article_json).get("published_at"))
        if record.article_json
        else datetime.min.replace(tzinfo=timezone.utc),
    )


async def _embed_article(article: dict[str, Any]) -> list[float]:
    text = "\n".join(
        [
            str(article.get("title", "")).strip(),
            str(article.get("source_name", "")).strip(),
            str(article.get("content", "")).strip(),
            str(article.get("full_text", "")).strip(),
            " ".join(str(tag) for tag in article.get("tags", []) if isinstance(tag, str)),
        ]
    )
    return await embed_text(text, model=STORY_EMBEDDING_MODEL)


def _find_candidate_story(candidates: list[StoryCandidate], story_id: str) -> Story | None:
    for candidate in candidates:
        if candidate.story.id == story_id:
            return candidate.story
    return None


def _load_embedding(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [float(value) for value in parsed if isinstance(value, (int, float))]


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."
