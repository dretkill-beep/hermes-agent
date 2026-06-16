"""tools/vault_recall_tool.py — Jarvarious vault semantic-recall agent tool. SCRUM-216.

Exposes the LanceDB vault semantic search (tools/vault_search.py in the
jarvarious repo) to the agent as the `vault_recall` tool, so Jarv can recall past
projects/sessions/decisions at chunk granularity with citations — instead of
Obsidian-MCP whole-note reads.

Why a subprocess: the search needs lancedb + sentence-transformers, which live in
the jarvarious venv (3.12), NOT this gateway venv (3.11); and the hermes `tools`
package would shadow jarvarious's. So we shell out to the jarvarious venv and
return its stdout. This module is stdlib-only — safe to import in the gateway.

Jarvarious-owned addition to the upstream runtime. Durable copy lives at
~/jarvarious/agent/patches/hermes-agent/vault_recall_tool.py and must be
re-copied after any `hermes update` (see that folder's README).
"""

from __future__ import annotations

import os
import subprocess

from tools.registry import registry

_JARV_ROOT = os.path.expanduser("~/jarvarious/agent")
_JARV_PY = os.path.join(_JARV_ROOT, ".venv", "bin", "python")

# Runs in the jarvarious venv; reads inputs from the environment (injection-safe
# — no string interpolation into code).
_RUNNER = (
    "import os, sys\n"
    "sys.path.insert(0, os.environ['JV_ROOT'])\n"
    "from tools.vault_search import format_recall\n"
    "print(format_recall(os.environ['JV_QUERY'], "
    "limit=int(os.environ.get('JV_LIMIT', '5'))))\n"
)

VAULT_RECALL_SCHEMA = {
    "name": "vault_recall",
    "description": (
        "Semantic recall over Dre's curated Obsidian vault (MyBrain4Jarv). "
        "Returns the most relevant note chunks with source citations for a "
        "query — use it to recall past projects, decisions, app builds, and "
        "sessions instead of reading whole notes. Only recalls what was indexed "
        "into the vault; says so explicitly when there are no matches."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall from the vault.",
            },
            "limit": {
                "type": "integer",
                "description": "Max chunks to return (default 5).",
            },
        },
        "required": ["query"],
    },
}


def vault_recall_tool(args, **kwargs) -> str:
    """Handler: run the vault recall in the jarvarious venv, return its text."""
    query = (args or {}).get("query", "") or ""
    if not query.strip():
        return "vault_recall: empty query."
    try:
        limit = int((args or {}).get("limit", 5) or 5)
    except (TypeError, ValueError):
        limit = 5
    env = {
        **os.environ,
        "JV_ROOT": _JARV_ROOT,
        "JV_QUERY": query,
        "JV_LIMIT": str(limit),
    }
    try:
        result = subprocess.run(
            [_JARV_PY, "-c", _RUNNER],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except Exception as exc:  # subprocess launch/timeout — fail soft, never crash
        return f"vault_recall error: {exc}"
    out = (result.stdout or "").strip()
    if out:
        return out
    err = (result.stderr or "").strip()
    return f"vault_recall: no result ({err[:300]})" if err else "vault_recall: no result."


def check_vault_recall_requirements() -> bool:
    """Available only when the jarvarious venv python exists."""
    return os.path.exists(_JARV_PY)


registry.register(
    name="vault_recall",
    toolset="vault",
    schema=VAULT_RECALL_SCHEMA,
    handler=lambda args, **kw: vault_recall_tool(args, **kw),
    check_fn=check_vault_recall_requirements,
    emoji="🧠",
    description=VAULT_RECALL_SCHEMA["description"],
)
