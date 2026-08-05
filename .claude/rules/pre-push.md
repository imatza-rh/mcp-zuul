---
globs: "**"
---
## Pre-Push Gate (RENDERED — before ANY git push)

Before pushing to mcp-zuul, verify ALL of these:

```
Pre-push check:
- [ ] Uncommitted changes that affect CI? `git diff --name-only` — if __init__.py, CLAUDE.md, or README.md are modified, commit them FIRST (test_tool_counts_match checks all three agree)
- [ ] Full test suite passes LOCALLY: `uv run pytest tests/ -q`
- [ ] Lint + format clean: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
- [ ] New regex patterns tested against realistic CI log samples (not just simple strings)
- [ ] Real data test: if formatters or tools changed, run the ACTUAL function with real Zuul API data (via MCP tool or captured sample), not just mocked unit tests. Compare output to expected structure. Measure any claimed percentages. (2026-08-05: "~75% smaller" claim survived unit tests but real data showed 42-58%. Docstring shipped with wrong number.)
```

Incident (2026-07-30): Pushed 6 commits, CI failed 3 times because:
1. `__init__.py` had stale tool count (uncommitted from prior session)
2. `CLAUDE.md` had stale tool count (excluded from git, out of sync)
3. `README.md` had stale tool count (uncommitted from prior session)

Each push-fix-push cycle wasted ~5 minutes of CI time.
