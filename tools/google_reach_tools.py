"""tools/google_reach_tools.py — Google Workspace read tools for Jarv. SCRUM-211/212/210 wire-in.

Exposes Dre's already-built, boundary-screened Google read capability (Gmail,
Calendar, Drive) to the live agent as callable tools — so Jarv can actually read
mail/calendar/drive in conversation, not just have the code in the repo. Every
field is screened by the inbound boundary (SCRUM-217) before it reaches Jarv.

Why subprocess: the capability needs the jarvarious venv (lancedb / google libs /
boundary classifier) which the gateway venv lacks, and the hermes `tools` package
shadows jarvarious's. So each tool shells out to the jarvarious venv. Stdlib-only
in the gateway; inputs passed via env (injection-safe). Durable copy lives in
~/jarvarious/agent/patches/hermes-agent/.

NOTE: each untrusted field is screened by a live Haiku call (~2.5s/field), so
these tools favor small result counts and use a generous timeout. Batching the
classifier is a tracked perf follow-up.
"""

from __future__ import annotations

import os
import subprocess

from tools.registry import registry

_JARV_ROOT = os.path.expanduser("~/jarvarious/agent")
_JARV_PY = os.path.join(_JARV_ROOT, ".venv", "bin", "python")


def _run(snippet: str, env_extra: dict, timeout: int = 120) -> str:
    """Run *snippet* in the jarvarious venv with JV_* env inputs; return stdout."""
    env = {**os.environ, "JV_ROOT": _JARV_ROOT, **env_extra}
    # use the valid key from the running gateway env, never a shell-shadowed one
    runner = "import os,sys\nsys.path.insert(0, os.environ['JV_ROOT'])\n" + snippet
    try:
        r = subprocess.run(
            [_JARV_PY, "-c", runner],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except Exception as exc:
        return f"error: {exc}"
    out = (r.stdout or "").strip()
    return out if out else f"no result ({(r.stderr or '').strip()[:300]})"


def _available() -> bool:
    return os.path.exists(_JARV_PY)


# --------------------------------------------------------------------------- #
# Gmail triage
# --------------------------------------------------------------------------- #
def gmail_triage_tool(args, **kw) -> str:
    n = str(int((args or {}).get("max_results", 6) or 6))
    return _run(
        "import json, os\n"
        "from scripts.gmail_triage import build_triage\n"
        "p = build_triage(max_results=int(os.environ['JV_N']))\n"
        "print(f\"Unread: {p['unread_count']} | flagged: {p['flagged_count']} | quarantined: {p['quarantined_count']}\")\n"
        "for e in p['emails']:\n"
        "    flag = ' [ACTION]' if e['needs_action_hint'] else ''\n"
        "    print(f\"- {e['sender'][:40]} :: {e['subject'][:60]}{flag}\")\n",
        {"JV_N": n}, timeout=150,
    )


# --------------------------------------------------------------------------- #
# Calendar (today + tomorrow)
# --------------------------------------------------------------------------- #
def calendar_today_tool(args, **kw) -> str:
    n = str(int((args or {}).get("max_results", 10) or 10))
    return _run(
        "import os\n"
        "from scripts.morning_brief import get_calendar_today\n"
        "from tools.google_reach import fetch_calendar_events\n"
        "evs = get_calendar_today(lambda: fetch_calendar_events(max_results=int(os.environ['JV_N'])))\n"
        "print(f\"{len(evs) if evs else 0} event(s):\")\n"
        "for e in (evs or []):\n"
        "    print(f\"- {e['start'][:16]}  {e['summary'][:55]}\")\n",
        {"JV_N": n}, timeout=150,
    )


# --------------------------------------------------------------------------- #
# Drive search + read
# --------------------------------------------------------------------------- #
def drive_search_tool(args, **kw) -> str:
    q = (args or {}).get("query", "") or ""
    if not q.strip():
        return "drive_search: empty query."
    return _run(
        "import os, json\n"
        "from tools.drive_tool import drive_search\n"
        "r = drive_search(os.environ['JV_Q'], limit=int(os.environ.get('JV_N','8')))\n"
        "for f in r['results']:\n"
        "    print(f\"- [{f['file_id']}] {f['name'][:60]} ({f['mime_type']})\")\n"
        "if not r['results']: print('no files found.')\n",
        {"JV_Q": q, "JV_N": str(int((args or {}).get("max_results", 8) or 8))}, timeout=120,
    )


def drive_read_tool(args, **kw) -> str:
    fid = (args or {}).get("file_id", "") or ""
    if not fid.strip():
        return "drive_read: empty file_id."
    return _run(
        "import os\n"
        "from tools.drive_tool import drive_read\n"
        "r = drive_read(os.environ['JV_FID'])\n"
        "print(r['text'][:4000] if r['screened_ok'] else r['text'])\n",
        {"JV_FID": fid}, timeout=120,
    )


# Only the FAST, single-field-screened tools are wired live. gmail_triage and
# calendar_today screen many fields (~4s/field of live Haiku), so a single call
# can take 60-150s — too slow/unreliable as interactive tools. They are delivered
# instead by the async morning-brief cron (latency-irrelevant there). Making them
# interactive needs batched screening (one classifier call per item, not per
# field) — tracked as a perf follow-up. gmail_triage_tool / calendar_today_tool
# remain defined above for that future wire-in.
_TOOLS = [
    ("drive_search", "drive", "🗂️", drive_search_tool,
     "Search Dre's Google Drive by name/content; returns file names + file IDs (boundary-screened). Read-only.",
     {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
    ("drive_read", "drive", "📄", drive_read_tool,
     "Read the text of a Google Drive document by file_id (boundary-screened). Read-only.",
     {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}),
]

for name, toolset, emoji, handler, desc, schema in _TOOLS:
    registry.register(
        name=name,
        toolset=toolset,
        schema={"name": name, "description": desc, "input_schema": schema},
        handler=(lambda h: (lambda args, **kw: h(args, **kw)))(handler),
        check_fn=_available,
        emoji=emoji,
        description=desc,
    )
