# AGENTS.md template for infrastructure repos (builder, workflows, ci, hub, control, site, docs, stores)

Infrastructure repos write their own AGENTS.md by hand, with these headings in this order, so agents find the
same things in the same places everywhere:

```
# AGENTS.md - <repo>
## Purpose            one paragraph: what this repo produces and who consumes it
## Layout             a table of top-level paths and what lives there
## What you may edit  explicit list; name generated or data directories that are off limits
## How to validate    the exact commands (lint, types, tests, build) that CI runs
## Contracts          which schemas in linuxserver-playground/builder this repo produces or consumes
## Pull request rules disclosure of AI use, one change per PR, commit subject style, no trailers
```

Keep it under 120 lines. Link to CONVENTIONS.md in the builder repo rather than repeating it.
