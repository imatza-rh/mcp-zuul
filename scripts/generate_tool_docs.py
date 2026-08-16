#!/usr/bin/env python3
"""Generate docs/tools.md from registered MCP tool metadata."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import mcp_zuul  # noqa: E402,F401
from mcp_zuul.server import mcp  # noqa: E402


def _get_tools() -> dict[str, object]:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def _mode(tool: object) -> str:
    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "read_only_hint", True)
    return "read" if read_only is not False else "write"


def generate() -> str:
    tools = _get_tools()
    lines = [
        "# Tools Reference",
        "",
        "Auto-generated from the registered MCP tools. Do not edit by hand.",
        "",
        "| Tool | Mode | Description |",
        "| --- | --- | --- |",
    ]
    for name in sorted(tools):
        tool = tools[name]
        description = (getattr(tool, "description", "") or "").splitlines()[0].strip()
        description = description.replace("|", "\\|")
        lines.append(f"| `{name}` | {_mode(tool)} | {description} |")
    lines.extend([
        "",
        "---",
        "",
        "Write tools are disabled by default with `ZUUL_READ_ONLY=true`.",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    output = Path(__file__).parent.parent / "docs" / "tools.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate(), encoding="utf-8")
    print(f"Wrote {output}")
