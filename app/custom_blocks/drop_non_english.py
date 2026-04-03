"""Drop articles whose text is probably not English.

Uses `py3langid` for lightweight offline language detection. This is especially
useful on short social posts, for example Nitter content, before sending them
to more expensive LLM filters.
"""

from typing import Any

from py3langid import classify


MIN_TEXT_LENGTH = 24
LANGUAGE_SAMPLE_LIMIT = 1200
LANGUAGE_FIELDS = ("title", "content", "full_text")

async def run(article: dict[str, Any]) -> dict[str, Any]:
    working_article = dict(article)
    sample = _build_text_sample(working_article)
    if len(sample) < MIN_TEXT_LENGTH:
        return {
            "passed": True,
            "article": working_article,
            "reason": f"Language check skipped because text sample is too short ({len(sample)} chars)",
        }

    detected, confidence = classify(sample)
    if detected == "en":
        return {
            "passed": True,
            "article": working_article,
            "reason": f"Detected English content (confidence {confidence:.3f})",
        }

    if not detected:
        return {
            "passed": True,
            "article": working_article,
            "reason": "Language detector was inconclusive; leaving article in pipeline",
        }

    return {
        "passed": False,
        "article": working_article,
        "reason": f"Detected non-English language: {detected} (confidence {confidence:.3f})",
    }


def _build_text_sample(article: dict[str, Any]) -> str:
    parts: list[str] = []
    for field_name in LANGUAGE_FIELDS:
        value = str(article.get(field_name, "")).strip()
        if value:
            parts.append(value)
    return " ".join(parts)[:LANGUAGE_SAMPLE_LIMIT]
