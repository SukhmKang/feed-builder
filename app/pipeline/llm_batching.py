import asyncio
import copy
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.ai.llm import generate_text
from app.pipeline.core import BlockResult, PipelineResult, copy_article, merge_tags
from app.pipeline.filters import Conditional, CustomBlock, KeywordFilter, LLMFilter, SemanticSimilarity, Switch
from app.pipeline.llm_config import VALID_LLM_TIERS, resolve_tier_model

logger = logging.getLogger(__name__)

DEFAULT_LLM_BATCH_SIZE = 10
DEFAULT_PIPELINE_EXECUTION_CHUNK_SIZE = 10
BATCH_COMPILER_MAX_COMPLETION_TOKENS = 4000
LLM_BATCH_COMPILER_SCHEMA = {
    "batch_prompt": "Instruction string that tells the model how to classify a list of articles at once.",
}


class BatchPromptCompilationError(RuntimeError):
    """Raised when an LLM batch prompt cannot be compiled safely."""


async def compile_llm_filter_batches(
    blocks_json: list[dict[str, Any]],
    *,
    path: str = "blocks",
) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for index, block_json in enumerate(blocks_json):
        compiled.append(await _compile_block_json(block_json, path=f"{path}[{index}]"))
    return compiled


async def _compile_block_json(block_json: dict[str, Any], *, path: str) -> dict[str, Any]:
    if not isinstance(block_json, dict):
        return block_json

    compiled = copy.deepcopy(block_json)
    block_type = str(compiled.get("type", "")).strip()

    if block_type == "llm_filter":
        prompt = str(compiled.get("prompt", "")).strip()
        batch_size = _coerce_batch_size(compiled.get("batch_size"), default=DEFAULT_LLM_BATCH_SIZE)
        compiled["batch_size"] = batch_size

        existing_hash = str(compiled.get("batch_prompt_source_hash", "")).strip()
        prompt_hash = _hash_prompt(prompt)
        existing_batch_prompt = str(compiled.get("batch_prompt", "")).strip()
        if existing_batch_prompt and existing_hash == prompt_hash:
            return compiled

        compiled["batch_prompt"] = await _compile_batch_prompt(prompt, path=path)
        compiled["batch_prompt_source_hash"] = prompt_hash
        return compiled

    if block_type == "conditional":
        compiled["if_true"] = await compile_llm_filter_batches(
            _coerce_block_list(compiled.get("if_true")),
            path=f"{path}.if_true",
        )
        compiled["if_false"] = await compile_llm_filter_batches(
            _coerce_block_list(compiled.get("if_false")),
            path=f"{path}.if_false",
        )
        return compiled

    if block_type == "switch":
        branches_json = compiled.get("branches", [])
        if isinstance(branches_json, list):
            compiled_branches: list[dict[str, Any]] = []
            for branch_index, branch_json in enumerate(branches_json):
                if not isinstance(branch_json, dict):
                    compiled_branches.append(branch_json)
                    continue
                branch_copy = copy.deepcopy(branch_json)
                branch_copy["blocks"] = await compile_llm_filter_batches(
                    _coerce_block_list(branch_copy.get("blocks")),
                    path=f"{path}.branches[{branch_index}].blocks",
                )
                compiled_branches.append(branch_copy)
            compiled["branches"] = compiled_branches
        compiled["default"] = await compile_llm_filter_batches(
            _coerce_block_list(compiled.get("default")),
            path=f"{path}.default",
        )
        return compiled

    return compiled


async def _compile_batch_prompt(prompt: str, *, path: str) -> str:
    if not prompt.strip():
        raise BatchPromptCompilationError(f"{path}: cannot compile batch prompt from an empty llm_filter prompt")

    provider, model = _resolve_batch_compiler_model()
    task_prompt = "\n\n".join(
        [
            "Rewrite this single-article classification prompt into a batch prompt.",
            "The rewritten prompt must expect a JSON list of articles instead of a single article.",
            "Each article will have: article_id, title, content, source_name.",
            "Preserve the original filtering intent and decision criteria as closely as possible.",
            "Do not ask for chain-of-thought or free-form prose outside the required JSON output.",
            "Do not use placeholders like {title} or {content}; refer to fields on each article object in the provided list.",
            "The batch response format itself will be enforced separately, so focus on the task instructions.",
            "Return JSON only with this schema:",
            json.dumps(LLM_BATCH_COMPILER_SCHEMA, indent=2),
            "Original single-article prompt:",
            prompt.strip(),
        ]
    )

    logger.info(
        "Batch prompt compilation start path=%s provider=%s model=%s prompt_hash=%s",
        path,
        provider,
        model,
        _hash_prompt(prompt),
    )
    logger.info("Batch prompt compilation source prompt path=%s:\n%s", path, prompt)
    logger.info("Batch prompt compilation task prompt path=%s:\n%s", path, task_prompt)

    raw = await generate_text(
        task_prompt,
        provider=provider,
        model=model,
        system=(
            "You rewrite prompt templates for batch article classification. "
            "Return valid JSON only."
        ),
        json_output=True,
        max_completion_tokens=BATCH_COMPILER_MAX_COMPLETION_TOKENS,
        model_params={"reasoning_effort": "low"} if provider == "openai" else None,
    )
    logger.info("Batch prompt compilation raw response path=%s:\n%s", path, raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BatchPromptCompilationError(
            f"{path}: batch prompt compiler returned invalid JSON: {exc}. Raw response: {raw[:500]!r}"
        ) from exc

    batch_prompt = str(parsed.get("batch_prompt", "")).strip()
    if not batch_prompt:
        raise BatchPromptCompilationError(
            f"{path}: batch prompt compiler returned empty batch_prompt. Parsed payload: {parsed!r}"
        )
    return batch_prompt


def _resolve_batch_compiler_model() -> tuple[str, str]:
    requested_tier = str(os.getenv("PIPELINE_BATCH_COMPILER_TIER", "medium")).strip().lower()
    if requested_tier not in VALID_LLM_TIERS:
        requested_tier = "medium"
    return resolve_tier_model(requested_tier)  # type: ignore[arg-type]


def infer_pipeline_execution_chunk_size(blocks_json: list[dict[str, Any]]) -> int:
    batch_sizes: list[int] = []
    _collect_batch_sizes(blocks_json, batch_sizes)
    if not batch_sizes:
        return DEFAULT_PIPELINE_EXECUTION_CHUNK_SIZE
    return max(max(batch_sizes), 1)


def _collect_batch_sizes(blocks_json: list[dict[str, Any]], collected: list[int]) -> None:
    for block_json in blocks_json:
        if not isinstance(block_json, dict):
            continue
        block_type = str(block_json.get("type", "")).strip()
        if block_type == "llm_filter":
            collected.append(_coerce_batch_size(block_json.get("batch_size"), default=DEFAULT_LLM_BATCH_SIZE))
            continue
        if block_type == "conditional":
            _collect_batch_sizes(_coerce_block_list(block_json.get("if_true")), collected)
            _collect_batch_sizes(_coerce_block_list(block_json.get("if_false")), collected)
            continue
        if block_type == "switch":
            branches_json = block_json.get("branches", [])
            if isinstance(branches_json, list):
                for branch_json in branches_json:
                    if isinstance(branch_json, dict):
                        _collect_batch_sizes(_coerce_block_list(branch_json.get("blocks")), collected)
            _collect_batch_sizes(_coerce_block_list(block_json.get("default")), collected)


@dataclass(slots=True)
class _PipelineState:
    index: int
    article: dict[str, Any]
    block_results: list[BlockResult] = field(default_factory=list)
    dropped_at: str | None = None


async def run_pipeline_batch(articles: list[dict[str, Any]], blocks: list[Any]) -> list[PipelineResult]:
    states = [_PipelineState(index=i, article=copy_article(article)) for i, article in enumerate(articles)]
    final_results: list[PipelineResult | None] = [None] * len(states)
    active_states = states

    for block_index, block in enumerate(blocks, start=1):
        if not active_states:
            break

        logger.info(
            "Pipeline batch block start block_index=%s block_type=%s article_count=%s",
            block_index,
            block.__class__.__name__,
            len(active_states),
        )
        block_results = await _run_block_batch(block, [state.article for state in active_states])

        next_active: list[_PipelineState] = []
        for state, block_result in zip(active_states, block_results, strict=False):
            state.article = block_result["article"]
            state.block_results.append(block_result)
            logger.info(
                "Pipeline batch block result article_id=%s block_index=%s block_type=%s passed=%s reason=%r",
                str(state.article.get("id", "")).strip(),
                block_index,
                block.__class__.__name__,
                block_result["passed"],
                str(block_result.get("reason", ""))[:300],
            )
            if block_result["passed"]:
                next_active.append(state)
                continue

            state.dropped_at = block.__class__.__name__
            logger.info(
                "Pipeline batch article dropped article_id=%s dropped_at=%s executed_blocks=%s",
                str(state.article.get("id", "")).strip(),
                state.dropped_at,
                len(state.block_results),
            )
            final_results[state.index] = {
                "passed": False,
                "article": state.article,
                "block_results": state.block_results,
                "dropped_at": state.dropped_at,
            }

        active_states = next_active

    for state in active_states:
        logger.info(
            "Pipeline batch article passed article_id=%s executed_blocks=%s",
            str(state.article.get("id", "")).strip(),
            len(state.block_results),
        )
        final_results[state.index] = {
            "passed": True,
            "article": state.article,
            "block_results": state.block_results,
            "dropped_at": None,
        }

    return [result for result in final_results if result is not None]


async def _run_block_batch(block: Any, articles: list[dict[str, Any]]) -> list[BlockResult]:
    if not articles:
        return []

    if isinstance(block, LLMFilter):
        return await block.run_batch(articles)

    if isinstance(block, Conditional):
        return await _run_conditional_batch(block, articles)

    if isinstance(block, Switch):
        return await _run_switch_batch(block, articles)

    if isinstance(block, (KeywordFilter, SemanticSimilarity, CustomBlock)):
        return await asyncio.gather(*[block.run(article) for article in articles])

    return await asyncio.gather(*[block.run(article) for article in articles])


async def _run_conditional_batch(block: Conditional, articles: list[dict[str, Any]]) -> list[BlockResult]:
    working_articles = [copy_article(article) for article in articles]
    branch_true = await asyncio.gather(*[block.condition.evaluate(article) for article in working_articles])

    true_group: list[tuple[int, dict[str, Any]]] = []
    false_group: list[tuple[int, dict[str, Any]]] = []
    for index, (article, passes_true) in enumerate(zip(working_articles, branch_true, strict=False)):
        branch_label = "branch:true" if passes_true else "branch:false"
        merge_tags(article, [branch_label])
        if passes_true:
            true_group.append((index, article))
        else:
            false_group.append((index, article))

    results: list[BlockResult | None] = [None] * len(working_articles)
    await _resolve_conditional_group(results, true_group, block.if_true, "branch:true")
    await _resolve_conditional_group(results, false_group, block.if_false, "branch:false")
    return [result for result in results if result is not None]


async def _resolve_conditional_group(
    results: list[BlockResult | None],
    group: list[tuple[int, dict[str, Any]]],
    blocks: list[Any],
    branch_label: str,
) -> None:
    if not group:
        return

    nested_results = await run_pipeline_batch([article for _, article in group], blocks)
    for (index, _), nested_result in zip(group, nested_results, strict=False):
        if not blocks:
            results[index] = {
                "passed": True,
                "article": nested_result["article"],
                "reason": f"{branch_label} with no nested blocks",
            }
        elif nested_result["passed"]:
            results[index] = {
                "passed": True,
                "article": nested_result["article"],
                "reason": f"{branch_label} passed {len(blocks)} nested blocks",
            }
        else:
            dropped_at = nested_result["dropped_at"] or "unknown"
            results[index] = {
                "passed": False,
                "article": nested_result["article"],
                "reason": f"{branch_label} dropped by {dropped_at}",
            }


async def _run_switch_batch(block: Switch, articles: list[dict[str, Any]]) -> list[BlockResult]:
    working_articles = [copy_article(article) for article in articles]
    results: list[BlockResult | None] = [None] * len(working_articles)

    remaining: list[tuple[int, dict[str, Any]]] = list(enumerate(working_articles))
    for branch_index, (condition, branch_blocks) in enumerate(block.branches, start=1):
        if not remaining:
            break

        decisions = await asyncio.gather(*[condition.evaluate(article) for _, article in remaining])
        matched: list[tuple[int, dict[str, Any]]] = []
        still_remaining: list[tuple[int, dict[str, Any]]] = []
        for (original_index, article), matched_branch in zip(remaining, decisions, strict=False):
            if matched_branch:
                merge_tags(article, [f"switch:{branch_index}"])
                matched.append((original_index, article))
            else:
                still_remaining.append((original_index, article))

        if matched:
            nested_results = await run_pipeline_batch([article for _, article in matched], branch_blocks)
            for (original_index, _), nested_result in zip(matched, nested_results, strict=False):
                if not branch_blocks:
                    results[original_index] = {
                        "passed": True,
                        "article": nested_result["article"],
                        "reason": f"switch branch {branch_index} matched with no nested blocks",
                    }
                elif nested_result["passed"]:
                    results[original_index] = {
                        "passed": True,
                        "article": nested_result["article"],
                        "reason": f"switch branch {branch_index} passed {len(branch_blocks)} nested blocks",
                    }
                else:
                    dropped_at = nested_result["dropped_at"] or "unknown"
                    results[original_index] = {
                        "passed": False,
                        "article": nested_result["article"],
                        "reason": f"switch branch {branch_index} dropped by {dropped_at}",
                    }
        remaining = still_remaining

    if remaining:
        for _, article in remaining:
            merge_tags(article, ["switch:default"])
        nested_results = await run_pipeline_batch([article for _, article in remaining], block.default)
        for (original_index, _), nested_result in zip(remaining, nested_results, strict=False):
            if not block.default:
                results[original_index] = {
                    "passed": True,
                    "article": nested_result["article"],
                    "reason": "switch default branch with no nested blocks",
                }
            elif nested_result["passed"]:
                results[original_index] = {
                    "passed": True,
                    "article": nested_result["article"],
                    "reason": f"switch default branch passed {len(block.default)} nested blocks",
                }
            else:
                dropped_at = nested_result["dropped_at"] or "unknown"
                results[original_index] = {
                    "passed": False,
                    "article": nested_result["article"],
                    "reason": f"switch default branch dropped by {dropped_at}",
                }

    return [result for result in results if result is not None]


def _coerce_batch_size(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 1)


def _coerce_block_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()
