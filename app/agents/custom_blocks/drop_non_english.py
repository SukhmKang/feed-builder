from dataclasses import dataclass
from typing import Any

from app.pipeline.core import BlockResult, copy_article


@dataclass(slots=True)
class DropNonEnglish:
    """Drop articles whose title+content are predominantly non-English.

    Strategy: measure the fraction of characters with code-point > 127
    (Arabic, CJK, Cyrillic, etc.).  If that fraction exceeds the threshold
    the article is considered non-English and is dropped.

    The default threshold of 0.15 is intentionally conservative so that
    English articles containing a few foreign-script proper nouns (e.g. an
    Arabic brand name inside an otherwise English tweet) still pass.
    """

    threshold: float = 0.15

    async def run(self, article: dict[str, Any]) -> BlockResult:
        working_article = copy_article(article)

        title = str(working_article.get("title") or "")
        content = str(working_article.get("content") or "")
        text = (title + " " + content).strip()

        if not text:
            return {
                "passed": True,
                "article": working_article,
                "reason": "No text to evaluate – passing through.",
            }

        non_ascii_count = sum(1 for ch in text if ord(ch) > 127)
        ratio = non_ascii_count / len(text)
        passed = ratio < self.threshold

        if passed:
            reason = f"English content retained (non-ASCII ratio {ratio:.2%})."
        else:
            reason = f"Non-English content dropped (non-ASCII ratio {ratio:.2%} >= threshold {self.threshold:.2%})."

        return {"passed": passed, "article": working_article, "reason": reason}
