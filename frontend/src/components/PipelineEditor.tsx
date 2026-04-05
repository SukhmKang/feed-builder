import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  DragDropContext,
  Draggable,
  Droppable,
  type DropResult,
  type DraggableProvidedDragHandleProps,
} from "@hello-pangea/dnd";
import { api } from "../api/client";
import { PIPELINE_ARTICLE_FIELDS, PIPELINE_SOURCE_TYPE_VALUES } from "../types";
import type { CustomBlockOption, PipelineBlock, PipelineCondition, PipelineTier, PipelineVersion, SourceSpec, SourceTypeValue } from "../types";

const SOURCE_TYPES = [
  "rss",
  "tavily",
  "google_news_search",
  "nitter_user",
  "nitter_search",
  "reddit_subreddit",
  "reddit_search",
  "reddit_subreddits_by_topic",
  "youtube_search",
  "youtube_channel",
  "youtube_channel_url",
  "youtube_channels_by_topic",
  "youtube_videos_by_topic",
] as const;

const PIPELINE_SOURCE_TYPES: readonly SourceTypeValue[] = PIPELINE_SOURCE_TYPE_VALUES;

type ArticleField = (typeof PIPELINE_ARTICLE_FIELDS)[number];

const BLOCK_META: Record<PipelineBlock["type"], { label: string; accent: string; icon: string; hint: string }> = {
  keyword_filter: { label: "Keyword Filter", accent: "#4c6fff", icon: "K", hint: "Quick include/exclude terms" },
  semantic_similarity: { label: "Semantic Similarity", accent: "#00a6b4", icon: "S", hint: "Vector match against content" },
  llm_filter: { label: "LLM Filter", accent: "#f28f3b", icon: "L", hint: "Prompted reasoning over article fields" },
  regex_filter: { label: "Regex Filter", accent: "#be185d", icon: "R", hint: "Include or exclude by regex pattern" },
  conditional: { label: "Conditional", accent: "#2f9e72", icon: "?", hint: "Branch on a condition tree" },
  switch: { label: "Switch", accent: "#8b5cf6", icon: "⇄", hint: "Route blocks by source or metadata" },
  custom_block: { label: "Custom Block", accent: "#6b7280", icon: "C", hint: "Run a custom Python block" },
};

const BLOCK_TYPES = Object.keys(BLOCK_META) as PipelineBlock["type"][];

const SOURCE_TYPE_META: Record<string, { label: string; accent: string }> = {
  rss: { label: "RSS / Atom", accent: "#4c6fff" },
  tavily: { label: "Tavily Search", accent: "#0f766e" },
  google_news_search: { label: "Google News", accent: "#0d8f66" },
  nitter_user: { label: "Nitter User", accent: "#111827" },
  nitter_search: { label: "Nitter Search", accent: "#4b5563" },
  reddit_subreddit: { label: "Reddit Subreddit", accent: "#ff5700" },
  reddit_search: { label: "Reddit Search", accent: "#ff7a1a" },
  reddit_subreddits_by_topic: { label: "Reddit Topic Discovery", accent: "#c2410c" },
  youtube_search: { label: "YouTube Search", accent: "#dc2626" },
  youtube_channel: { label: "YouTube Channel", accent: "#b91c1c" },
  youtube_channel_url: { label: "YouTube Channel URL", accent: "#ef4444" },
  youtube_channels_by_topic: { label: "YouTube Topic Channels", accent: "#f87171" },
  youtube_videos_by_topic: { label: "YouTube Topic Videos", accent: "#fb7185" },
};

const CONDITION_TYPES: PipelineCondition["type"][] = [
  "and",
  "or",
  "not",
  "source_type",
  "source_name",
  "source_url",
  "domain",
  "source_domain",
  "field_equals",
  "field_contains",
  "field_exists",
  "field_matches_regex",
  "keyword",
  "length",
  "published_after",
  "published_before",
  "llm",
];

function defaultSource(type: SourceSpec["type"] = "rss"): SourceSpec {
  return { type, feed: "" };
}

function defaultBlock(type: PipelineBlock["type"]): PipelineBlock {
  switch (type) {
    case "keyword_filter":
      return { type, include: [], exclude: [] };
    case "semantic_similarity":
      return { type, query: "", field: "content", threshold: 0.62 };
    case "llm_filter":
      return { type, prompt: "", tier: "mini" };
    case "regex_filter":
      return { type, field: "title", pattern: "", mode: "include" };
    case "conditional":
      return { type, condition: defaultCondition("source_type"), if_true: [], if_false: [] };
    case "switch":
      return { type, branches: [{ condition: defaultCondition("source_type"), blocks: [] }], default: [] };
    case "custom_block":
      return { type, name: "" };
  }
}

function normalizeStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim());
}

function normalizeSources(value: unknown): SourceSpec[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      type: typeof item.type === "string" && item.type.trim() ? item.type : "rss",
      feed: typeof item.feed === "string" ? item.feed : "",
    }));
}

function normalizeCondition(condition: PipelineCondition): PipelineCondition {
  switch (condition.type) {
    case "and":
      return {
        type: "and",
        conditions: Array.isArray(condition.conditions)
          ? condition.conditions.map((child) => normalizeCondition(child))
          : [defaultCondition("source_type")],
      };
    case "or":
      return {
        type: "or",
        conditions: Array.isArray(condition.conditions)
          ? condition.conditions.map((child) => normalizeCondition(child))
          : [defaultCondition("source_type")],
      };
    case "not":
      return { type: "not", condition: normalizeCondition(condition.condition ?? defaultCondition("source_type")) };
    case "keyword":
      return { ...condition, terms: normalizeStringList(condition.terms), operator: condition.operator ?? "any" };
    case "length":
      return {
        ...condition,
        field: condition.field ?? "content",
        min: Number.isFinite(condition.min) ? condition.min : 0,
        max: Number.isFinite(condition.max) ? condition.max : 10000,
      };
    case "published_after":
    case "published_before":
      return { ...condition, days_ago: Number.isFinite(condition.days_ago) ? condition.days_ago : 7 };
    case "llm":
      return { ...condition, prompt: condition.prompt ?? "", tier: condition.tier ?? "mini" };
    default:
      return condition;
  }
}

function normalizeBlock(block: PipelineBlock): PipelineBlock {
  switch (block.type) {
    case "keyword_filter":
      return {
        ...block,
        include: normalizeStringList(block.include),
        exclude: normalizeStringList(block.exclude),
      };
    case "semantic_similarity":
      return {
        ...block,
        query: block.query ?? "",
        field: block.field ?? "content",
        threshold: Number.isFinite(block.threshold) ? block.threshold : 0.62,
      };
    case "llm_filter":
      return { ...block, prompt: block.prompt ?? "", tier: block.tier ?? "mini" };
    case "regex_filter":
      return { ...block, field: block.field ?? "title", pattern: block.pattern ?? "", mode: block.mode ?? "include" };
    case "conditional":
      return {
        ...block,
        condition: normalizeCondition(block.condition),
        if_true: Array.isArray(block.if_true) ? block.if_true.map((child) => normalizeBlock(child)) : [],
        if_false: Array.isArray(block.if_false) ? block.if_false.map((child) => normalizeBlock(child)) : [],
      };
    case "switch":
      return {
        ...block,
        branches: Array.isArray(block.branches)
          ? block.branches.map((branch) => ({
              condition: normalizeCondition(branch.condition),
              blocks: Array.isArray(branch.blocks) ? branch.blocks.map((child) => normalizeBlock(child)) : [],
            }))
          : [],
        default: Array.isArray(block.default) ? block.default.map((child) => normalizeBlock(child)) : [],
      };
    case "custom_block":
      return { ...block, name: block.name ?? "" };
  }
}

function updateLLMFilterPrompt(
  block: Extract<PipelineBlock, { type: "llm_filter" }>,
  prompt: string,
): PipelineBlock {
  return {
    ...block,
    prompt,
    batch_prompt: "",
    batch_prompt_source_hash: "",
  } as PipelineBlock;
}

function normalizePipeline(value: unknown): PipelineBlock[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is PipelineBlock => Boolean(item) && typeof item === "object" && "type" in item)
    .map((item) => normalizeBlock(item));
}

function defaultCondition(type: PipelineCondition["type"]): PipelineCondition {
  switch (type) {
    case "and":
      return { type, conditions: [defaultCondition("source_type")] };
    case "or":
      return { type, conditions: [defaultCondition("source_type")] };
    case "not":
      return { type, condition: defaultCondition("source_type") };
    case "source_type":
      return { type, value: "rss" };
    case "source_name":
    case "source_url":
    case "domain":
    case "source_domain":
      return { type, value: "" };
    case "field_equals":
    case "field_contains":
      return { type, field: "title", value: "" };
    case "field_exists":
      return { type, field: "title" };
    case "field_matches_regex":
      return { type, field: "title", pattern: "" };
    case "keyword":
      return { type, terms: [""], operator: "any" };
    case "length":
      return { type, field: "content", min: 80, max: 8000 };
    case "published_after":
      return { type, days_ago: 7 };
    case "published_before":
      return { type, days_ago: 30 };
    case "llm":
      return { type, prompt: "", tier: "mini" };
  }
}

function reorder<T>(items: T[], startIndex: number, endIndex: number): T[] {
  const next = [...items];
  const [removed] = next.splice(startIndex, 1);
  next.splice(endIndex, 0, removed);
  return next;
}

function updateAt<T>(items: T[], index: number, nextItem: T): T[] {
  return items.map((item, itemIndex) => (itemIndex === index ? nextItem : item));
}

function replaceAt<T>(items: T[], index: number, replacements: T[]): T[] {
  return [...items.slice(0, index), ...replacements, ...items.slice(index + 1)];
}

interface BlockEditContext {
  blockPath: string;
  parentContext: string;
  siblingBlocks: PipelineBlock[];
}

function describeContainerContext(containerPath: string, parentBlock: PipelineBlock | null): string {
  if (!parentBlock) {
    return `${containerPath} (top-level pipeline sequence)`;
  }
  if (parentBlock.type === "conditional") {
    if (containerPath.endsWith(".if_true")) return `${containerPath} (conditional true branch)`;
    if (containerPath.endsWith(".if_false")) return `${containerPath} (conditional false branch)`;
  }
  if (parentBlock.type === "switch") {
    if (containerPath.includes(".branches[")) return `${containerPath} (switch branch block sequence)`;
    if (containerPath.endsWith(".default")) return `${containerPath} (switch default block sequence)`;
  }
  return `${containerPath} (${parentBlock.type} child block sequence)`;
}

function removeAt<T>(items: T[], index: number): T[] {
  return items.filter((_, itemIndex) => itemIndex !== index);
}

function PromptEditor({
  value,
  onChange,
  minHeight = 120,
}: {
  value: string;
  onChange: (value: string) => void;
  minHeight?: number;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder="Write your filter criteria. The article title, source, and content will be included automatically."
      style={{
        width: "100%",
        minHeight,
        maxHeight: 220,
        padding: 9,
        border: "1px solid silver",
        borderRadius: 12,
        fontSize: 14,
        fontFamily: "inherit",
        lineHeight: 1.55,
        resize: "vertical",
        outline: 0,
        boxSizing: "border-box",
      }}
    />
  );
}

function TagListEditor({
  values,
  onChange,
  placeholder,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
}) {
  return (
    <div style={tagList}>
      {values.map((value, index) => (
        <div key={index} style={tagChip}>
          <input
            value={value}
            onChange={(event) => onChange(updateAt(values, index, event.target.value))}
            placeholder={placeholder}
            style={tagChipInput}
          />
          <button type="button" onClick={() => onChange(removeAt(values, index))} style={tagChipButton}>
            ×
          </button>
        </div>
      ))}
      <button type="button" style={softPillButton} onClick={() => onChange([...values, ""])}>
        + add
      </button>
    </div>
  );
}

function TierPicker({ value, onChange }: { value: PipelineTier; onChange: (tier: PipelineTier) => void }) {
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {(["mini", "medium", "high"] as PipelineTier[]).map((tier) => (
        <button
          key={tier}
          type="button"
          style={{
            ...tierButton,
            ...(value === tier ? tierButtonActive[tier] : null),
          }}
          onClick={() => onChange(tier)}
        >
          {tier}
        </button>
      ))}
    </div>
  );
}

function FieldSelect({
  value,
  onChange,
}: {
  value: ArticleField;
  onChange: (field: ArticleField) => void;
}) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value as ArticleField)}>
      {PIPELINE_ARTICLE_FIELDS.map((field) => (
        <option key={field} value={field}>
          {field}
        </option>
      ))}
    </select>
  );
}

function SourceGroupsEditor({
  sources,
  onChange,
  embedded = false,
}: {
  sources: SourceSpec[];
  onChange: (sources: SourceSpec[]) => void;
  embedded?: boolean;
}) {
  const grouped = useMemo(() => {
    const groups = new Map<string, SourceSpec[]>();
    for (const source of sources) {
      const key = source.type || "rss";
      const current = groups.get(key) ?? [];
      current.push(source);
      groups.set(key, current);
    }
    return groups;
  }, [sources]);

  const orderedTypes = useMemo(() => {
    const present = Array.from(grouped.keys());
    const known = SOURCE_TYPES.filter((type) => present.includes(type));
    const custom = present.filter((type) => !SOURCE_TYPES.includes(type as (typeof SOURCE_TYPES)[number])).sort();
    return [...known, ...custom];
  }, [grouped]);

  function addSource(type: string) {
    onChange([...sources, defaultSource(type)]);
  }

  function updateSource(sourceIndex: number, nextSource: SourceSpec) {
    onChange(updateAt(sources, sourceIndex, nextSource));
  }

  function deleteSource(sourceIndex: number) {
    onChange(removeAt(sources, sourceIndex));
  }

  return (
    <section style={embedded ? embeddedSectionShell : sectionShell}>
      {!embedded ? (
        <div style={sectionHeader}>
          <div>
            <p style={eyebrow}>Sources</p>
            <p style={sectionDescription}>Edit the inputs feeding your pipeline. Sources are grouped by their ingestion type so it is easier to reason about coverage.</p>
          </div>
          <select
            value=""
            onChange={(event) => {
              if (event.target.value) {
                addSource(event.target.value);
                event.target.value = "";
              }
            }}
            style={addSourceSelect}
          >
            <option value="">+ add source</option>
            {SOURCE_TYPES.map((type) => (
              <option key={type} value={type}>
                {SOURCE_TYPE_META[type]?.label ?? type}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 14 }}>
          <select
            value=""
            onChange={(event) => {
              if (event.target.value) {
                addSource(event.target.value);
                event.target.value = "";
              }
            }}
            style={addSourceSelect}
          >
            <option value="">+ add source</option>
            {SOURCE_TYPES.map((type) => (
              <option key={type} value={type}>
                {SOURCE_TYPE_META[type]?.label ?? type}
              </option>
            ))}
          </select>
        </div>
      )}

      <div style={sourceGroupList}>
        {orderedTypes.map((type) => {
          const meta = SOURCE_TYPE_META[type] ?? { label: type, accent: "#64748b" };
          const members = grouped.get(type) ?? [];
          return (
            <div key={type} style={{ ...sourceGroupCard, borderTopColor: meta.accent }}>
              <div style={sourceGroupHeader}>
                <div>
                  <div style={{ ...sourceGroupBadge, color: meta.accent, background: `${meta.accent}14` }}>{meta.label}</div>
                  <div style={sourceCount}>{members.length} source{members.length === 1 ? "" : "s"}</div>
                </div>
                <button type="button" style={ghostAction} onClick={() => addSource(type)}>
                  + add
                </button>
              </div>

              <div style={sourceRows}>
                {sources.map((source, index) => {
                  if (source.type !== type) return null;
                  return (
                    <div key={`${type}-${index}`} style={sourceRowCard}>
                      <div style={sourceRowMeta}>
                        <span style={sourceOrdinal}>#{index + 1}</span>
                        <span style={sourceTypeInline}>{meta.label}</span>
                      </div>
                      <div style={sourceRowBody}>
                        <label style={{ ...fieldStack, minWidth: 170 }}>
                          <span style={fieldLabel}>Type</span>
                          <select
                            value={source.type}
                            onChange={(event) => updateSource(index, { ...source, type: event.target.value })}
                          >
                            {SOURCE_TYPES.map((option) => (
                              <option key={option} value={option}>
                                {SOURCE_TYPE_META[option]?.label ?? option}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label style={{ ...fieldStack, flex: 1 }}>
                          <span style={fieldLabel}>Feed / query</span>
                          <input
                            value={source.feed}
                            onChange={(event) => updateSource(index, { ...source, feed: event.target.value })}
                            placeholder="https://example.com/feed or search query"
                          />
                        </label>
                        <button type="button" style={iconGhost} onClick={() => deleteSource(index)}>
                          ×
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ConditionEditor({
  condition,
  onChange,
  onDelete,
  depth = 0,
}: {
  condition: PipelineCondition;
  onChange: (condition: PipelineCondition) => void;
  onDelete?: () => void;
  depth?: number;
}) {
  const accent = ["#4c6fff", "#2f9e72", "#8b5cf6", "#f28f3b"][Math.min(depth, 3)];

  function body() {
    switch (condition.type) {
      case "and":
      case "or":
        return (
          <div style={nestedStack}>
            {condition.conditions.map((child, index) => (
              <ConditionEditor
                key={index}
                depth={depth + 1}
                condition={child}
                onChange={(next) => onChange({ type: condition.type, conditions: updateAt(condition.conditions, index, next) })}
                onDelete={() => onChange({ type: condition.type, conditions: removeAt(condition.conditions, index) })}
              />
            ))}
            <button
              type="button"
              style={ghostAction}
              onClick={() => onChange({ type: condition.type, conditions: [...condition.conditions, defaultCondition("source_type")] })}
            >
              + add condition
            </button>
          </div>
        );
      case "not":
        return (
          <div style={nestedStack}>
            <ConditionEditor
              depth={depth + 1}
              condition={condition.condition}
              onChange={(next) => onChange({ type: "not", condition: next })}
            />
          </div>
        );
      case "source_type":
        return (
          <label style={fieldStack}>
            <span style={fieldLabel}>Source type</span>
            <select value={condition.value} onChange={(event) => onChange({ type: "source_type", value: event.target.value as SourceTypeValue })}>
              {PIPELINE_SOURCE_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
          </label>
        );
      case "source_name":
      case "source_url":
      case "domain":
      case "source_domain":
        return (
          <label style={fieldStack}>
            <span style={fieldLabel}>Value</span>
            <input value={condition.value} onChange={(event) => onChange({ type: condition.type, value: event.target.value } as PipelineCondition)} />
          </label>
        );
      case "field_equals":
      case "field_contains":
        return (
          <div style={fieldGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Field</span>
              <FieldSelect
                value={condition.field}
                onChange={(field) => onChange({ type: condition.type, field, value: condition.value } as PipelineCondition)}
              />
            </label>
            <label style={fieldStack}>
              <span style={fieldLabel}>Value</span>
              <input
                value={condition.value}
                onChange={(event) => onChange({ type: condition.type, field: condition.field, value: event.target.value } as PipelineCondition)}
              />
            </label>
          </div>
        );
      case "field_exists":
        return (
          <label style={fieldStack}>
            <span style={fieldLabel}>Field</span>
            <FieldSelect value={condition.field} onChange={(field) => onChange({ type: "field_exists", field })} />
          </label>
        );
      case "field_matches_regex":
        return (
          <div style={fieldGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Field</span>
              <FieldSelect
                value={condition.field}
                onChange={(field) => onChange({ type: "field_matches_regex", field, pattern: condition.pattern })}
              />
            </label>
            <label style={fieldStack}>
              <span style={fieldLabel}>Regex</span>
              <input
                value={condition.pattern}
                onChange={(event) => onChange({ type: "field_matches_regex", field: condition.field, pattern: event.target.value })}
              />
            </label>
          </div>
        );
      case "keyword":
        return (
          <div style={{ display: "grid", gap: 12 }}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Terms</span>
              <TagListEditor values={condition.terms} onChange={(terms) => onChange({ type: "keyword", terms, operator: condition.operator })} placeholder="term" />
            </label>
            <label style={fieldStack}>
              <span style={fieldLabel}>Operator</span>
              <select value={condition.operator} onChange={(event) => onChange({ type: "keyword", terms: condition.terms, operator: event.target.value as "any" | "all" })}>
                <option value="any">any</option>
                <option value="all">all</option>
              </select>
            </label>
          </div>
        );
      case "length":
        return (
          <div style={fieldGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Field</span>
              <FieldSelect value={condition.field} onChange={(field) => onChange({ type: "length", field, min: condition.min, max: condition.max })} />
            </label>
            <label style={fieldStack}>
              <span style={fieldLabel}>Range</span>
              <div style={numberPair}>
                <input type="number" value={condition.min} onChange={(event) => onChange({ type: "length", field: condition.field, min: Number(event.target.value), max: condition.max })} />
                <input type="number" value={condition.max} onChange={(event) => onChange({ type: "length", field: condition.field, min: condition.min, max: Number(event.target.value) })} />
              </div>
            </label>
          </div>
        );
      case "published_after":
      case "published_before":
        return (
          <label style={fieldStack}>
            <span style={fieldLabel}>Days ago</span>
            <input type="number" value={condition.days_ago} onChange={(event) => onChange({ type: condition.type, days_ago: Number(event.target.value) } as PipelineCondition)} />
          </label>
        );
      case "llm":
        return (
          <div style={{ display: "grid", gap: 12 }}>
            <PromptEditor value={condition.prompt} onChange={(prompt) => onChange({ type: "llm", prompt, tier: condition.tier })} minHeight={84} />
            <TierPicker value={condition.tier ?? "mini"} onChange={(tier) => onChange({ type: "llm", prompt: condition.prompt, tier })} />
          </div>
        );
    }
  }

  return (
    <div style={{ ...conditionCard, borderLeftColor: accent }}>
      <div style={conditionHeader}>
        <select value={condition.type} onChange={(event) => onChange(defaultCondition(event.target.value as PipelineCondition["type"]))} style={conditionTypeSelect}>
          {CONDITION_TYPES.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
        {onDelete ? (
          <button type="button" style={iconGhost} onClick={onDelete}>
            ×
          </button>
        ) : null}
      </div>
      {body()}
    </div>
  );
}

function BlockEditor({
  block,
  index,
  onChange,
  onDelete,
  onAiEditSelf,
  onAiEditBlock,
  dragHandleProps,
  customBlockOptions,
  blockPath,
}: {
  block: PipelineBlock;
  index: number;
  onChange: (block: PipelineBlock) => void;
  onDelete: () => void;
  onAiEditSelf: (instruction: string) => Promise<void>;
  onAiEditBlock: (block: PipelineBlock, instruction: string, context: BlockEditContext) => Promise<PipelineBlock[]>;
  dragHandleProps?: DraggableProvidedDragHandleProps | null;
  customBlockOptions: CustomBlockOption[];
  blockPath: string;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiInstruction, setAiInstruction] = useState("");
  const [aiWorking, setAiWorking] = useState(false);
  const meta = BLOCK_META[block.type];

  function body() {
    switch (block.type) {
      case "keyword_filter":
        return (
          <div style={editorGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Include</span>
              <TagListEditor values={block.include ?? []} onChange={(include) => onChange({ ...block, include })} placeholder="keyword" />
            </label>
            <label style={fieldStack}>
              <span style={fieldLabel}>Exclude</span>
              <TagListEditor values={block.exclude ?? []} onChange={(exclude) => onChange({ ...block, exclude })} placeholder="keyword" />
            </label>
          </div>
        );
      case "semantic_similarity":
        return (
          <div style={editorGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Query</span>
              <textarea value={block.query} onChange={(event) => onChange({ ...block, query: event.target.value })} rows={4} />
            </label>
            <div style={fieldGrid}>
              <label style={fieldStack}>
                <span style={fieldLabel}>Field</span>
                <FieldSelect value={block.field} onChange={(field) => onChange({ ...block, field })} />
              </label>
              <label style={fieldStack}>
                <span style={fieldLabel}>Threshold</span>
                <input type="number" min="0" max="1" step="0.05" value={block.threshold} onChange={(event) => onChange({ ...block, threshold: Number(event.target.value) })} />
              </label>
            </div>
          </div>
        );
      case "llm_filter":
        return (
          <div style={editorGrid}>
            <PromptEditor value={block.prompt} onChange={(prompt) => onChange(updateLLMFilterPrompt(block, prompt))} />
            <TierPicker value={block.tier ?? "mini"} onChange={(tier) => onChange({ ...block, tier })} />
          </div>
        );
      case "regex_filter":
        return (
          <div style={editorGrid}>
            <div style={fieldGrid}>
              <label style={fieldStack}>
                <span style={fieldLabel}>Field</span>
                <FieldSelect value={block.field} onChange={(field) => onChange({ ...block, field })} />
              </label>
              <label style={fieldStack}>
                <span style={fieldLabel}>Mode</span>
                <select value={block.mode ?? "include"} onChange={(event) => onChange({ ...block, mode: event.target.value as "include" | "exclude" })}>
                  <option value="include">Include (must match)</option>
                  <option value="exclude">Exclude (must not match)</option>
                </select>
              </label>
            </div>
            <label style={fieldStack}>
              <span style={fieldLabel}>Pattern</span>
              <input value={block.pattern} onChange={(event) => onChange({ ...block, pattern: event.target.value })} placeholder="(?i)review|analysis" />
            </label>
          </div>
        );
      case "conditional":
        return (
          <div style={editorGrid}>
            <div style={subsectionTitle}>When</div>
            <ConditionEditor condition={block.condition} onChange={(condition) => onChange({ ...block, condition })} />
            <div style={conditionalSequence}>
              <div style={nestedColumn}>
                <div style={subsectionTitle}>If true</div>
                <div style={conditionalIndented}>
                  <BlockListEditor
                    blocks={block.if_true ?? []}
                    onChange={(if_true) => onChange({ ...block, if_true })}
                    nested
                    customBlockOptions={customBlockOptions}
                    onAiEdit={onAiEditBlock}
                    containerPath={`${blockPath}.if_true`}
                    parentBlock={block}
                  />
                </div>
              </div>
              <div style={nestedColumn}>
                <div style={subsectionTitle}>If false</div>
                <div style={conditionalIndented}>
                  <BlockListEditor
                    blocks={block.if_false ?? []}
                    onChange={(if_false) => onChange({ ...block, if_false })}
                    nested
                    customBlockOptions={customBlockOptions}
                    onAiEdit={onAiEditBlock}
                    containerPath={`${blockPath}.if_false`}
                    parentBlock={block}
                  />
                </div>
              </div>
            </div>
          </div>
        );
      case "switch":
        return (
          <div style={editorGrid}>
            <div style={subsectionTitle}>Branches</div>
            <div style={{ display: "grid", gap: 12 }}>
              {block.branches.map((branch, branchIndex) => (
                <div key={branchIndex} style={branchCard}>
                  <div style={branchHeader}>
                    <div style={branchLabel}>Branch {branchIndex + 1}</div>
                    <button type="button" style={iconGhost} onClick={() => onChange({ ...block, branches: removeAt(block.branches, branchIndex) })}>
                      ×
                    </button>
                  </div>
                  <ConditionEditor
                    condition={branch.condition}
                    onChange={(condition) =>
                      onChange({
                        ...block,
                        branches: updateAt(block.branches, branchIndex, { ...branch, condition }),
                      })
                    }
                  />
                  <BlockListEditor
                    blocks={branch.blocks ?? []}
                    onChange={(blocks) =>
                      onChange({
                        ...block,
                        branches: updateAt(block.branches, branchIndex, { ...branch, blocks }),
                      })
                    }
                    nested
                    customBlockOptions={customBlockOptions}
                    onAiEdit={onAiEditBlock}
                    containerPath={`${blockPath}.branches[${branchIndex}].blocks`}
                    parentBlock={block}
                  />
                </div>
              ))}
            </div>
            <button
              type="button"
              style={ghostAction}
              onClick={() => onChange({ ...block, branches: [...block.branches, { condition: defaultCondition("source_type"), blocks: [] }] })}
            >
              + add branch
            </button>
            <div style={subsectionTitle}>Default</div>
            <BlockListEditor
              blocks={block.default ?? []}
              onChange={(defaultBlocks) => onChange({ ...block, default: defaultBlocks })}
              nested
              customBlockOptions={customBlockOptions}
              onAiEdit={onAiEditBlock}
              containerPath={`${blockPath}.default`}
              parentBlock={block}
            />
          </div>
        );
      case "custom_block":
        const selectedOption = customBlockOptions.find((option) => option.name === block.name) ?? null;
        return (
          <div style={editorGrid}>
            <label style={fieldStack}>
              <span style={fieldLabel}>Block name</span>
              <select
                value={selectedOption ? block.name : ""}
                onChange={(event) => onChange({ type: "custom_block", name: event.target.value })}
              >
                <option value="" disabled>
                  Select a custom block
                </option>
                {customBlockOptions.map((option) => (
                  <option key={option.name} value={option.name}>
                    {option.title ? `${option.title} (${option.name})` : option.name}
                  </option>
                ))}
              </select>
            </label>
            {selectedOption?.description ? <div style={sectionDescription}>{selectedOption.description}</div> : null}
            {!selectedOption && block.name ? (
              <div style={sectionDescription}>Saved block <strong>{block.name}</strong> is not in the current custom block registry.</div>
            ) : null}
          </div>
        );
    }
  }

  async function handleAiEditSubmit() {
    const instruction = aiInstruction.trim();
    if (!instruction) return;
    setAiWorking(true);
    try {
      await onAiEditSelf(instruction);
      setAiInstruction("");
      setAiOpen(false);
    } finally {
      setAiWorking(false);
    }
  }

  return (
    <div style={{ ...blockCard, borderTopColor: meta.accent }}>
      <div style={blockHeader}>
        <div {...dragHandleProps} style={dragHandle}>
          ⋮⋮
        </div>
        <div style={{ ...blockIcon, background: `${meta.accent}15`, color: meta.accent }}>{meta.icon}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={blockHeaderTop}>
            <select value={block.type} onChange={(event) => onChange(defaultBlock(event.target.value as PipelineBlock["type"]))} style={blockTypeSelect}>
              {BLOCK_TYPES.map((type) => (
                <option key={type} value={type}>
                  {BLOCK_META[type].label}
                </option>
              ))}
            </select>
            <span style={{ ...stepPill, borderColor: `${meta.accent}30`, color: meta.accent }}>step {index + 1}</span>
          </div>
          <div style={blockHint}>{meta.hint}</div>
        </div>
        <button type="button" style={iconGhost} onClick={() => setCollapsed((current) => !current)}>
          {collapsed ? "+" : "–"}
        </button>
        <button type="button" style={aiGhost} onClick={() => setAiOpen((current) => !current)}>
          AI edit
        </button>
        <button type="button" style={{ ...iconGhost, color: "#dc2626" }} onClick={onDelete}>
          ×
        </button>
      </div>
      {!collapsed ? (
        <div style={blockBody}>
          {body()}
          {aiOpen ? (
            <div style={aiEditBox}>
              <input
                value={aiInstruction}
                onChange={(event) => setAiInstruction(event.target.value)}
                placeholder='E.g. "Break this block into a conditional with 4 blocks"'
                style={aiEditInput}
              />
              <div style={heroActions}>
                <button type="button" style={toolbarSecondaryButton} onClick={() => setAiOpen(false)} disabled={aiWorking}>
                  Cancel
                </button>
                <button
                  type="button"
                  style={toolbarPrimaryButton}
                  onClick={() => void handleAiEditSubmit()}
                  disabled={!aiInstruction.trim() || aiWorking}
                >
                  {aiWorking ? "Applying..." : "Apply"}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function AddBlockRow({ onAdd }: { onAdd: (type: PipelineBlock["type"]) => void }) {
  return (
    <div style={addBlockStrip}>
      {BLOCK_TYPES.map((type) => (
        <button key={type} type="button" style={addBlockChoice} onClick={() => onAdd(type)}>
          <span style={{ ...addBlockIcon, color: BLOCK_META[type].accent }}>{BLOCK_META[type].icon}</span>
          {BLOCK_META[type].label}
        </button>
      ))}
    </div>
  );
}

function BlockListEditor({
  blocks,
  onChange,
  nested = false,
  customBlockOptions,
  onAiEdit,
  containerPath,
  parentBlock = null,
}: {
  blocks: PipelineBlock[];
  onChange: (blocks: PipelineBlock[]) => void;
  nested?: boolean;
  customBlockOptions: CustomBlockOption[];
  onAiEdit: (block: PipelineBlock, instruction: string, context: BlockEditContext) => Promise<PipelineBlock[]>;
  containerPath: string;
  parentBlock?: PipelineBlock | null;
}) {
  const droppableId = useRef(`droppable-${Math.random().toString(36).slice(2)}`).current;

  function handleDragEnd(result: DropResult) {
    if (!result.destination) return;
    onChange(reorder(blocks, result.source.index, result.destination.index));
  }

  const content = (
    <Droppable droppableId={droppableId}>
      {(provided, snapshot) => (
        <div
          ref={provided.innerRef}
          {...provided.droppableProps}
          style={{
            ...listShell,
            ...(nested ? nestedListShell : null),
            background: snapshot.isDraggingOver ? "#eef4ff" : nested ? "#f8fbff" : "transparent",
          }}
        >
          {blocks.map((block, index) => (
            <Draggable key={`${droppableId}-${index}`} draggableId={`${droppableId}-${index}`} index={index}>
              {(dragProvided, dragSnapshot) => (
                <div
                  ref={dragProvided.innerRef}
                  {...dragProvided.draggableProps}
                  style={{
                    ...dragProvided.draggableProps.style,
                    opacity: dragSnapshot.isDragging ? 0.92 : 1,
                  }}
                >
                  {(() => {
                    const context: BlockEditContext = {
                      blockPath: `${containerPath}[${index}]`,
                      parentContext: describeContainerContext(containerPath, parentBlock),
                      siblingBlocks: blocks.filter((_, itemIndex) => itemIndex !== index),
                    };
                    return (
                  <BlockEditor
                    block={block}
                    index={index}
                    onChange={(next) => onChange(updateAt(blocks, index, next))}
                    onDelete={() => onChange(removeAt(blocks, index))}
                    onAiEditSelf={async (instruction) => {
                      const replacementBlocks = await onAiEdit(block, instruction, context);
                      onChange(replaceAt(blocks, index, replacementBlocks));
                    }}
                    onAiEditBlock={onAiEdit}
                    dragHandleProps={dragProvided.dragHandleProps}
                    customBlockOptions={customBlockOptions}
                    blockPath={context.blockPath}
                  />
                    );
                  })()}
                </div>
              )}
            </Draggable>
          ))}
          {provided.placeholder}
          <AddBlockRow onAdd={(type) => onChange([...blocks, defaultBlock(type)])} />
        </div>
      )}
    </Droppable>
  );

  return <DragDropContext onDragEnd={handleDragEnd}>{content}</DragDropContext>;
}

interface Props {
  feedId: string;
  sources: SourceSpec[];
  pipeline: PipelineBlock[];
  onSave: (payload: { sources: SourceSpec[]; pipeline: PipelineBlock[]; versionLabel?: string }) => Promise<void>;
  onFeedConfigChanged?: (config: { sources: SourceSpec[]; blocks: PipelineBlock[] }) => void;
}

export function PipelineEditor({ feedId, sources: initialSources, pipeline: initialPipeline, onSave, onFeedConfigChanged }: Props) {
  const normalizedInitialSources = useMemo(() => normalizeSources(initialSources), [initialSources]);
  const normalizedInitialPipeline = useMemo(() => normalizePipeline(initialPipeline), [initialPipeline]);

  const [sources, setSources] = useState<SourceSpec[]>(normalizedInitialSources);
  const [pipeline, setPipeline] = useState<PipelineBlock[]>(normalizedInitialPipeline);
  const [customBlockOptions, setCustomBlockOptions] = useState<CustomBlockOption[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [sourcesCollapsed, setSourcesCollapsed] = useState(false);
  const [pipelineCollapsed, setPipelineCollapsed] = useState(false);
  const [versionLabel, setVersionLabel] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<PipelineVersion[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [reverting, setReverting] = useState<string | null>(null);

  useEffect(() => {
    setSources(normalizedInitialSources);
    setPipeline(normalizedInitialPipeline);
  }, [normalizedInitialSources, normalizedInitialPipeline]);

  useEffect(() => {
    let cancelled = false;
    api.feeds
      .listCustomBlocks()
      .then((items) => {
        if (!cancelled) {
          setCustomBlockOptions(items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCustomBlockOptions([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const dirty =
    JSON.stringify(sources) !== JSON.stringify(normalizedInitialSources) ||
    JSON.stringify(pipeline) !== JSON.stringify(normalizedInitialPipeline);

  const loadVersions = useCallback(async () => {
    setLoadingVersions(true);
    try {
      const result = await api.pipelineVersions.list(feedId);
      setVersions(result);
    } catch {
      // non-critical
    } finally {
      setLoadingVersions(false);
    }
  }, [feedId]);

  useEffect(() => {
    if (historyOpen) loadVersions();
  }, [historyOpen, loadVersions]);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await onSave({ sources, pipeline, versionLabel: versionLabel.trim() || undefined });
      setVersionLabel("");
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
      if (historyOpen) loadVersions();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setSaving(false);
    }
  }

  async function handleRevert(version: PipelineVersion) {
    setReverting(version.id);
    setError(null);
    try {
      const result = await api.pipelineVersions.revert(feedId, version.id);
      const config = result.feed.config;
      if (config) {
        onFeedConfigChanged?.({ sources: config.sources, blocks: config.blocks });
      }
      await loadVersions();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setReverting(null);
    }
  }

  async function handleBlockAiEdit(block: PipelineBlock, instruction: string, context: BlockEditContext): Promise<PipelineBlock[]> {
    setError(null);
    try {
      const result = await api.feeds.aiEditBlock(
        feedId,
        block,
        sources,
        context,
        instruction,
      );
      return normalizePipeline(result.replacement_blocks);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
      throw nextError;
    }
  }

  function resetAll() {
    setSources(normalizedInitialSources);
    setPipeline(normalizedInitialPipeline);
    setError(null);
  }

  return (
    <div style={pageShell}>
      <div style={toolbar}>
        <div style={metricsRow}>
          <span style={metricPill}>{pipeline.length} blocks</span>
          <span style={metricPill}>{sources.length} sources</span>
          {dirty ? <span style={{ ...metricPill, color: "#3154d3", borderColor: "#cdd9ff" }}>unsaved changes</span> : null}
        </div>
        <div style={heroActions}>
          <input
            type="text"
            placeholder="Describe this change (optional)"
            value={versionLabel}
            onChange={(e) => setVersionLabel(e.target.value)}
            style={{
              fontSize: 13,
              padding: "6px 12px",
              borderRadius: 10,
              border: "1px solid #d6deeb",
              background: "#fff",
              color: "#1c1c1e",
              outline: "none",
              width: 220,
              opacity: dirty ? 1 : 0.45,
            }}
            disabled={!dirty || saving}
          />
          <button type="button" style={toolbarSecondaryButton} onClick={resetAll} disabled={!dirty || saving}>
            Reset
          </button>
          <button type="button" style={toolbarPrimaryButton} onClick={handleSave} disabled={!dirty || saving}>
            {saving ? "Saving..." : saved ? "Saved" : "Save"}
          </button>
        </div>
      </div>

      {error ? <div style={errorBanner}>{error}</div> : null}

      <div style={contentShell}>
        <section style={sectionShell}>
          <div style={sectionHeader}>
            <div>
              <p style={eyebrow}>Sources</p>
              <p style={sectionDescription}>Edit the inputs feeding your pipeline. Sources are grouped by ingestion type so coverage is easier to reason about.</p>
            </div>
            <button type="button" style={sectionToggleButton} onClick={() => setSourcesCollapsed((current) => !current)}>
              {sourcesCollapsed ? "Expand" : "Collapse"}
            </button>
          </div>
          {!sourcesCollapsed ? <SourceGroupsEditor sources={sources} onChange={setSources} embedded /> : null}
        </section>

        <section style={sectionShell}>
          <div style={sectionHeader}>
            <div>
              <p style={eyebrow}>Pipeline</p>
              <p style={sectionDescription}>Arrange the block sequence, branch where needed, and use prompt mentions to reference article properties cleanly.</p>
            </div>
            <button type="button" style={sectionToggleButton} onClick={() => setPipelineCollapsed((current) => !current)}>
              {pipelineCollapsed ? "Expand" : "Collapse"}
            </button>
          </div>
          {!pipelineCollapsed ? (
            <BlockListEditor
              blocks={pipeline}
              onChange={setPipeline}
              customBlockOptions={customBlockOptions}
              onAiEdit={handleBlockAiEdit}
              containerPath="pipeline"
              parentBlock={null}
            />
          ) : null}
        </section>

        <section style={sectionShell}>
          <div style={sectionHeader}>
            <div>
              <p style={eyebrow}>Version History</p>
              <p style={sectionDescription}>Every save creates a new version. Revert to any previous configuration.</p>
            </div>
            <button
              type="button"
              style={sectionToggleButton}
              onClick={() => setHistoryOpen((v) => !v)}
            >
              {historyOpen ? "Collapse" : "Expand"}
            </button>
          </div>
          {historyOpen ? (
            loadingVersions ? (
              <p style={{ fontSize: 13, color: "#8e8e93", margin: "8px 0" }}>Loading…</p>
            ) : versions.length === 0 ? (
              <p style={{ fontSize: 13, color: "#8e8e93", margin: "8px 0" }}>No versions saved yet.</p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {versions.map((v) => (
                  <div
                    key={v.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      padding: "10px 14px",
                      borderRadius: 12,
                      border: v.is_active ? "1.5px solid #4c6fff" : "1px solid #e5e5ea",
                      background: v.is_active ? "#f0f4ff" : "#fff",
                      gap: 12,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                      <span style={{
                        fontSize: 12,
                        fontWeight: 700,
                        color: v.is_active ? "#4c6fff" : "#8e8e93",
                        whiteSpace: "nowrap",
                      }}>
                        v{v.version_number}
                      </span>
                      {v.is_active ? (
                        <span style={{
                          fontSize: 10,
                          fontWeight: 700,
                          padding: "2px 7px",
                          borderRadius: 999,
                          background: "#4c6fff",
                          color: "#fff",
                        }}>
                          ACTIVE
                        </span>
                      ) : null}
                      <span style={{ fontSize: 13, color: "#3a3a3c", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {v.label ?? "No description"}
                      </span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                      <span style={{ fontSize: 11, color: "#8e8e93", whiteSpace: "nowrap" }}>
                        {v.created_at ? new Date(v.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""}
                      </span>
                      {!v.is_active ? (
                        <button
                          type="button"
                          disabled={reverting === v.id}
                          onClick={() => handleRevert(v)}
                          style={{
                            fontSize: 12,
                            fontWeight: 600,
                            padding: "4px 12px",
                            borderRadius: 8,
                            border: "1px solid #d6deeb",
                            background: "#fff",
                            color: "#3154d3",
                            cursor: "pointer",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {reverting === v.id ? "Reverting…" : "Revert"}
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            )
          ) : null}
        </section>
      </div>
    </div>
  );
}

const pageShell: CSSProperties = {
  display: "grid",
  gridTemplateRows: "auto auto 1fr",
  gap: 18,
  height: "100%",
  padding: 24,
  overflow: "auto",
  background:
    "radial-gradient(circle at top left, rgba(76,111,255,0.12), transparent 32%), radial-gradient(circle at top right, rgba(0,166,180,0.1), transparent 28%), #f5f7fb",
};

const toolbar: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 16,
  alignItems: "center",
  flexWrap: "wrap",
  background: "rgba(255,255,255,0.85)",
  border: "1px solid rgba(214, 222, 235, 0.95)",
  borderRadius: 18,
  padding: "14px 16px",
  backdropFilter: "blur(12px)",
};

const heroActions: CSSProperties = {
  display: "flex",
  gap: 10,
  alignItems: "center",
  flexWrap: "wrap",
};

const aiEditBox: CSSProperties = {
  display: "grid",
  gap: 14,
  marginTop: 14,
  paddingTop: 14,
  borderTop: "1px solid rgba(214, 222, 235, 0.95)",
};

const aiEditInput: CSSProperties = {
  width: "100%",
  border: "1px solid #d6deeb",
  borderRadius: 12,
  padding: "10px 12px",
  fontSize: 14,
  fontFamily: "inherit",
  background: "#fff",
};

const aiGhost: CSSProperties = {
  border: "1px solid rgba(49, 84, 211, 0.18)",
  background: "rgba(49, 84, 211, 0.08)",
  color: "#3154d3",
  borderRadius: 12,
  minWidth: "auto",
  minHeight: 36,
  paddingInline: 10,
  fontWeight: 600,
};

const toolbarPrimaryButton: CSSProperties = {
  background: "#3154d3",
  color: "#ffffff",
  padding: "10px 16px",
  borderRadius: 14,
  fontWeight: 700,
};

const toolbarSecondaryButton: CSSProperties = {
  background: "#ffffff",
  color: "#264066",
  border: "1px solid #d9dfeb",
  padding: "10px 16px",
  borderRadius: 14,
  fontWeight: 600,
};

const sectionToggleButton: CSSProperties = {
  background: "rgba(255,255,255,0.92)",
  color: "#4b5d79",
  border: "1px solid rgba(214, 222, 235, 0.95)",
  padding: "6px 10px",
  borderRadius: 999,
  fontSize: 12,
  fontWeight: 700,
  lineHeight: 1,
  alignSelf: "flex-start",
};

const errorBanner: CSSProperties = {
  background: "#fff3f2",
  color: "#b42318",
  border: "1px solid #f3c7c3",
  borderRadius: 16,
  padding: "12px 16px",
  fontSize: 14,
};

const contentShell: CSSProperties = {
  display: "grid",
  gap: 18,
  alignContent: "start",
  paddingBottom: 32,
};

const sectionShell: CSSProperties = {
  background: "rgba(255,255,255,0.82)",
  border: "1px solid rgba(214, 222, 235, 0.95)",
  borderRadius: 24,
  padding: 22,
  backdropFilter: "blur(12px)",
  boxShadow: "0 10px 30px rgba(15, 23, 42, 0.06)",
};

const embeddedSectionShell: CSSProperties = {
  display: "grid",
  gap: 0,
};

const sectionHeader: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: 16,
  alignItems: "flex-start",
  marginBottom: 18,
  flexWrap: "wrap",
};

const eyebrow: CSSProperties = {
  fontSize: 12,
  color: "#4c6fff",
  textTransform: "uppercase",
  letterSpacing: "0.12em",
  marginBottom: 6,
  fontWeight: 700,
};

const sectionDescription: CSSProperties = {
  color: "#5d6b82",
  fontSize: 14,
  lineHeight: 1.55,
  maxWidth: 760,
};

const metricsRow: CSSProperties = {
  display: "flex",
  gap: 10,
  flexWrap: "wrap",
};

const metricPill: CSSProperties = {
  borderRadius: 999,
  border: "1px solid #d9dfeb",
  padding: "7px 12px",
  fontSize: 12,
  color: "#4d5d77",
  background: "#fff",
};

const addSourceSelect: CSSProperties = {
  minWidth: 180,
  maxWidth: 220,
};

const sourceGroupList: CSSProperties = {
  display: "grid",
  gap: 14,
};

const sourceGroupCard: CSSProperties = {
  display: "grid",
  gap: 14,
  background: "#fff",
  border: "1px solid #dce4ef",
  borderTop: "4px solid #4c6fff",
  borderRadius: 20,
  padding: 16,
};

const sourceRows: CSSProperties = {
  display: "grid",
  gap: 10,
};

const sourceGroupHeader: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  gap: 10,
};

const sourceGroupBadge: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  borderRadius: 999,
  padding: "6px 10px",
  fontWeight: 700,
  fontSize: 12,
};

const sourceCount: CSSProperties = {
  marginTop: 8,
  color: "#718096",
  fontSize: 12,
};

const sourceRowCard: CSSProperties = {
  display: "grid",
  gap: 10,
  background: "#f8fbff",
  border: "1px solid #dce4ef",
  borderRadius: 16,
  padding: 12,
};

const sourceRowMeta: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
};

const sourceTypeInline: CSSProperties = {
  fontSize: 12,
  color: "#5d6b82",
};

const sourceRowBody: CSSProperties = {
  display: "flex",
  gap: 12,
  alignItems: "flex-end",
  flexWrap: "wrap",
};

const sourceOrdinal: CSSProperties = {
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.12em",
  color: "#718096",
  fontWeight: 700,
};

const fieldGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
  gap: 12,
};

const fieldStack: CSSProperties = {
  display: "grid",
  gap: 6,
};

const fieldLabel: CSSProperties = {
  fontSize: 12,
  color: "#516179",
  fontWeight: 700,
};

const listShell: CSSProperties = {
  display: "grid",
  gap: 14,
  borderRadius: 20,
  transition: "background 0.18s ease",
};

const nestedListShell: CSSProperties = {
  padding: 12,
  border: "1px dashed #cdd9eb",
};

const blockCard: CSSProperties = {
  border: "1px solid #dce4ef",
  borderTop: "4px solid #4c6fff",
  borderRadius: 22,
  background: "#fff",
  boxShadow: "0 12px 24px rgba(15, 23, 42, 0.05)",
  overflow: "hidden",
};

const blockHeader: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 12,
  padding: "16px 18px",
  background: "linear-gradient(180deg, rgba(248,251,255,0.9), rgba(255,255,255,0.95))",
};

const dragHandle: CSSProperties = {
  width: 28,
  height: 28,
  display: "grid",
  placeItems: "center",
  color: "#8090a8",
  fontSize: 14,
  borderRadius: 10,
  cursor: "grab",
  userSelect: "none",
  background: "#edf2fa",
  flexShrink: 0,
};

const blockIcon: CSSProperties = {
  width: 38,
  height: 38,
  borderRadius: 14,
  display: "grid",
  placeItems: "center",
  fontWeight: 800,
  flexShrink: 0,
};

const blockHeaderTop: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  flexWrap: "wrap",
  marginBottom: 6,
};

const blockTypeSelect: CSSProperties = {
  border: "none",
  padding: 0,
  background: "transparent",
  fontSize: 16,
  fontWeight: 700,
  color: "#10203a",
  width: "auto",
};

const stepPill: CSSProperties = {
  borderRadius: 999,
  border: "1px solid #d9dfeb",
  padding: "4px 8px",
  fontSize: 11,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
};

const blockHint: CSSProperties = {
  fontSize: 13,
  color: "#617189",
  lineHeight: 1.5,
};

const blockBody: CSSProperties = {
  padding: "18px",
};

const editorGrid: CSSProperties = {
  display: "grid",
  gap: 14,
};

const nestedColumn: CSSProperties = {
  display: "grid",
  gap: 10,
};

const conditionalSequence: CSSProperties = {
  display: "grid",
  gap: 16,
};

const conditionalIndented: CSSProperties = {
  marginLeft: 14,
};

const subsectionTitle: CSSProperties = {
  fontSize: 12,
  color: "#5b6b83",
  textTransform: "uppercase",
  letterSpacing: "0.12em",
  fontWeight: 700,
};

const branchCard: CSSProperties = {
  display: "grid",
  gap: 12,
  borderRadius: 18,
  border: "1px solid #dce4ef",
  background: "#f8fbff",
  padding: 14,
};

const branchHeader: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
};

const branchLabel: CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: "#26344e",
};

const conditionCard: CSSProperties = {
  display: "grid",
  gap: 10,
  padding: 12,
  borderRadius: 16,
  border: "1px solid #dce4ef",
  borderLeft: "4px solid #4c6fff",
  background: "#fff",
};

const conditionHeader: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  gap: 12,
};

const conditionTypeSelect: CSSProperties = {
  width: "auto",
  fontSize: 13,
  fontWeight: 700,
};

const nestedStack: CSSProperties = {
  display: "grid",
  gap: 10,
};

const tagList: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
};

const tagChip: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  borderRadius: 999,
  border: "1px solid #d9dfeb",
  background: "#fff",
  padding: "4px 8px",
};

const tagChipInput: CSSProperties = {
  border: "none",
  padding: 0,
  minWidth: 90,
  background: "transparent",
};

const tagChipButton: CSSProperties = {
  background: "transparent",
  color: "#7a8798",
  padding: 0,
  borderRadius: 999,
  minWidth: 18,
};

const softPillButton: CSSProperties = {
  padding: "6px 10px",
  borderRadius: 999,
  border: "1px dashed #b8c6dd",
  background: "#f8fbff",
  color: "#3154d3",
  fontSize: 12,
  fontWeight: 700,
};

const tierButton: CSSProperties = {
  background: "#fff",
  border: "1px solid #d9dfeb",
  color: "#5c6f86",
  padding: "8px 12px",
  borderRadius: 999,
  textTransform: "capitalize",
};

const tierButtonActive: Record<PipelineTier, CSSProperties> = {
  mini: { background: "#e9fbff", color: "#0d7f92", borderColor: "#a9e9f2" },
  medium: { background: "#fff4e5", color: "#b86707", borderColor: "#ffd399" },
  high: { background: "#fff0f0", color: "#c03232", borderColor: "#f0bbbb" },
};

const addBlockStrip: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 10,
  paddingTop: 4,
};

const addBlockChoice: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  background: "#f8fbff",
  border: "1px dashed #bfd0ea",
  color: "#284268",
  borderRadius: 14,
  padding: "10px 12px",
};

const addBlockIcon: CSSProperties = {
  width: 22,
  height: 22,
  display: "grid",
  placeItems: "center",
  borderRadius: 999,
  background: "#fff",
  fontWeight: 800,
};

const ghostAction: CSSProperties = {
  background: "transparent",
  color: "#3154d3",
  padding: 0,
  borderRadius: 0,
  fontWeight: 700,
  fontSize: 13,
};

const iconGhost: CSSProperties = {
  background: "#f3f7fd",
  color: "#6a7a92",
  width: 28,
  height: 28,
  borderRadius: 10,
  padding: 0,
  display: "grid",
  placeItems: "center",
};

const numberPair: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: 8,
};
