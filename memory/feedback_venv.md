---
name: Always use venv
description: User wants bash commands to run inside the project's venv at feed_builder/venv
type: feedback
---

Always activate the venv before running Python or pip commands for this project.

**Why:** The project uses a venv at `/Users/sukhmkang/Desktop/Postgrad Coding Projects/feed_builder/venv`. Running outside it uses the wrong Python/packages.

**How to apply:** Prefix Python/pip bash commands with `source "/Users/sukhmkang/Desktop/Postgrad Coding Projects/feed_builder/venv/bin/activate" &&`
