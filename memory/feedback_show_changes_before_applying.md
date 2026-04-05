---
name: Show prompt/content changes before applying
description: User wants to review suggested content changes in chat before they are written to files
type: feedback
---

When making changes to human-authored text content (prompts, documentation, copy), show the proposed new version in the chat first so the user can review it before applying.

**Why:** User explicitly asked "can you put the new suggested prompt in our chat first so I can inspect it?" after a file edit was attempted without preview.

**How to apply:** For edits to string constants like PIPELINE_SCHEMA_PROMPT, or any other human-authored text that requires judgment, output the proposed text in a code block and wait for approval before calling Edit/Write.
