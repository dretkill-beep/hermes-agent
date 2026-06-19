"""
gateway/inbound_screen.py — inbound push-screening chokepoint for the live gateway.

The symmetric counterpart to the egress allowlist (db/egress_guard.py): egress stops
the agent from *sending* data to an arbitrary destination; this stops *untrusted
external content* (a Telegram/Discord/… message body, sender display name, quoted
reply, attachment filename) from reaching the agent's context unscreened — the
inbound prompt-injection surface.

It reuses the existing inbound boundary (SCRUM-217/218,
``~/jarvarious/agent/tools/inbound_boundary.py``): trust-tag -> injection deny-list ->
Haiku batch classifier -> quarantine, fail-closed. This module is only the gateway
*wiring*: it adapts a ``MessageEvent`` into the boundary's batch screen and applies the
verdict back onto the event.

**Config-gated and defaults to OFF** (``inbound_screening.mode``), exactly like the
egress guard and the receipt gate — landing it changes nothing until flipped:
  - off     (default): no-op, returns the event untouched at zero cost.
  - observe: screen + log would-quarantine fields, but deliver the event UNMODIFIED.
  - block:   redact not-allowed fields in place (body -> placeholder; name/quote ->
             blanked) and deliver — the message is NEVER dropped, only sanitized.

Fail-safe everywhere: if the boundary import fails, or any screening step errors, the
event passes through unmodified and we log — screening a message must never crash the
gateway or lose a message. (We prefer a missed screen over taking Jarv offline; that is
the same posture as the egress guard's fail-open and the receipt gate's try/except.)
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("inbound_screen")

# Dedicated, grep-able observe/enforce trail — independent of the gateway's logging
# config (mirrors egress_guard's _EGRESS_LOG). This is the file the observe->block
# validation reads. Best-effort; never raises.
_INBOUND_LOG = os.path.expanduser("~/.hermes/logs/inbound_screen.log")

# The boundary + its config live in the jarvarious repo, not this one.
_JARV_AGENT = os.path.expanduser("~/jarvarious/agent")
_CONFIG_PATH = os.path.join(_JARV_AGENT, "hermes", "config.yaml")


def _inbound_log(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(_INBOUND_LOG), exist_ok=True)
        with open(_INBOUND_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{line}\n")
    except Exception:
        pass


# --- Fail-safe boundary import ---------------------------------------------
# If the jarvarious boundary can't be imported (missing repo, dependency, etc.)
# screening degrades to a no-op pass-through — it must NEVER crash the gateway.
# _IMPORT_OK lets tests assert the real wiring resolved instead of silently
# passing on a no-op.
#
# COLLISION NOTE: the live gateway ALREADY binds a top-level ``tools`` package
# (its own core tools) at startup, so a plain ``from tools.inbound_boundary
# import ...`` would resolve against the LIVE tools dir and fail — silently
# turning screening into a no-op the moment mode is flipped. So we load the
# boundary file DIRECTLY by path under a unique module name (never binding the
# bare ``tools`` name), exactly the "collision-free shim" posture the receipt
# gate uses for jarvarious_gate. ``~/jarvarious/agent`` stays on sys.path so the
# boundary's OWN transitive imports (hermes.guards.rules, dotenv) resolve.
_IMPORT_OK = False
try:
    import importlib.util as _ilu
    import sys as _sys

    if _JARV_AGENT not in _sys.path:
        _sys.path.insert(0, _JARV_AGENT)

    _bmod_name = "_jarv_inbound_boundary"
    _bmod_path = os.path.join(_JARV_AGENT, "tools", "inbound_boundary.py")
    _spec = _ilu.spec_from_file_location(_bmod_name, _bmod_path)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"cannot build spec for {_bmod_path}")
    _bmod = _ilu.module_from_spec(_spec)
    # Register under its unique name BEFORE exec so the module's own @dataclass
    # decorators (which look up cls.__module__ in sys.modules) resolve.
    _sys.modules[_bmod_name] = _bmod
    _spec.loader.exec_module(_bmod)

    ClassifierUnavailable = _bmod.ClassifierUnavailable  # type: ignore
    ClassifierVerdict = _bmod.ClassifierVerdict  # type: ignore
    haiku_batch_classifier = _bmod.haiku_batch_classifier  # type: ignore
    screen_inbound_batch = _bmod.screen_inbound_batch  # type: ignore

    # Stable handle to the loaded boundary module (for tests / introspection).
    boundary = _bmod  # type: ignore

    _IMPORT_OK = True
except Exception as _exc:  # pragma: no cover - environment-dependent
    logger.warning(
        "inbound_screen: boundary import failed — screening is a no-op pass-through: %s",
        _exc,
    )

    # Stubs so the rest of the module references resolve even when import failed.
    class ClassifierUnavailable(RuntimeError):  # type: ignore
        pass

    class ClassifierVerdict:  # type: ignore
        def __init__(self, safe: bool, reason: str) -> None:
            self.safe = safe
            self.reason = reason

    def haiku_batch_classifier(contents):  # type: ignore
        raise ClassifierUnavailable("boundary import failed")

    def screen_inbound_batch(fields, *, source, classifier=None):  # type: ignore
        return {}


# --- Config (mtime-cached, default OFF, safe on any error) ------------------
# screen_event runs on the inbound message hot path, so re-parsing YAML each
# message is wasteful. Cache the parsed config keyed on config.yaml's mtime —
# a config flip still takes effect without a gateway restart (mtime changes),
# but a steady config costs one stat() per message. Mirrors egress_guard.
_CONFIG_CACHE: tuple | None = None  # (mtime, config-dict)

_DEFAULT_CONFIG = {
    "mode": "off",
    "degrade_open": True,
    "exempt_platforms": [],
    "screen_reply_quote": True,
    "screen_attachment_names": True,
}


def _load_inbound_config() -> dict:
    """Return the ``inbound_screening`` config from ~/jarvarious/agent/hermes/config.yaml,
    with safe defaults (mode='off'). Cached on the config file's mtime.

    Any error -> defaults (mode off): a config read must never break the gateway."""
    global _CONFIG_CACHE
    cfg = dict(_DEFAULT_CONFIG)
    try:
        import yaml

        if os.path.exists(_CONFIG_PATH):
            mtime = os.path.getmtime(_CONFIG_PATH)
            if _CONFIG_CACHE is not None and _CONFIG_CACHE[0] == mtime:
                return _CONFIG_CACHE[1]
            raw = yaml.safe_load(open(_CONFIG_PATH)) or {}
            section = raw.get("inbound_screening", {}) or {}
            cfg["mode"] = str(section.get("mode", cfg["mode"])).lower()
            cfg["degrade_open"] = bool(section.get("degrade_open", cfg["degrade_open"]))
            cfg["exempt_platforms"] = set(section.get("exempt_platforms", []) or [])
            cfg["screen_reply_quote"] = bool(
                section.get("screen_reply_quote", cfg["screen_reply_quote"])
            )
            cfg["screen_attachment_names"] = bool(
                section.get("screen_attachment_names", cfg["screen_attachment_names"])
            )
            _CONFIG_CACHE = (mtime, cfg)
    except Exception as exc:  # config read must never break the gateway
        logger.debug("inbound config read failed (using defaults, mode=off): %s", exc)
        cfg = dict(_DEFAULT_CONFIG)
    # Normalize exempt_platforms to a set even on the default path.
    if not isinstance(cfg.get("exempt_platforms"), set):
        cfg["exempt_platforms"] = set(cfg.get("exempt_platforms") or [])
    return cfg


# --- Degrading classifier --------------------------------------------------
def _degrading_batch_classifier(contents):
    """Batch classifier with a degrade-open option.

    Calls the real Haiku batch classifier. On ``ClassifierUnavailable`` (no key,
    SDK, network, parse failure): if config ``degrade_open`` is true, return an
    all-SAFE verdict list (one per content — the count MUST match or
    screen_inbound_batch fails closed) with a loud WARNING. This is safe because
    the injection deny-list already ran *inside* screen_inbound_batch BEFORE the
    classifier — so the worst literal injections are still caught; degrade-open
    only forgoes the LLM judgement on deny-clear content rather than quarantining
    every external message when Haiku is down.

    If ``degrade_open`` is false, re-raise (fail closed — quarantine on
    uncertainty)."""
    try:
        return haiku_batch_classifier(contents)
    except ClassifierUnavailable as exc:
        cfg = _load_inbound_config()
        if cfg.get("degrade_open"):
            logger.warning(
                "inbound_screen: classifier UNAVAILABLE (%s) — degrade_open=true, "
                "passing %d deny-clear field-chunk(s) as SAFE (deny-list already ran)",
                exc,
                len(contents),
            )
            _inbound_log(f"DEGRADE-OPEN\tclassifier-down\t{exc}")
            return [
                ClassifierVerdict(safe=True, reason="degrade-open: classifier down")
                for _ in contents
            ]
        raise


# --- Main entry ------------------------------------------------------------
# Block-mode redaction: body becomes a placeholder that tells the agent content
# was removed and why; other fields are blanked.
def _body_placeholder(reason: str) -> str:
    return f"[inbound screening removed external content — {reason}]"


def screen_event(event):
    """Screen one inbound ``MessageEvent``. Returns ``(event, quarantined_fields)``.

    Mode off -> returns ``(event, [])`` immediately, BEFORE touching any event
    attribute or assembling any field: zero cost, byte-identical delivery.

    Exempt (pass-through unchanged, [] quarantined): internal/synthetic events,
    slash commands, the LOCAL (CLI) platform, and any configured exempt platform.

    observe -> screen + log would-quarantine fields, return event UNMODIFIED.
    block   -> redact each not-allowed field in place, return ``(event, [names])``.
    The message is NEVER dropped — only sanitized.

    Fail-safe: any internal error returns ``(event, [])`` (original event).
    """
    try:
        cfg = _load_inbound_config()
        mode = cfg.get("mode", "off")

        # OFF: zero cost, no attribute access, no field assembly — identical event.
        if mode == "off":
            return event, []

        # --- Exemptions (pass-through unmodified) ---
        if getattr(event, "internal", False):
            return event, []
        try:
            if event.is_command():
                return event, []
        except Exception:
            pass

        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        platform_value = getattr(platform, "value", None)
        # LOCAL (CLI) is the trusted operator terminal — never screen it.
        if platform_value == "local":
            return event, []
        if platform_value in cfg.get("exempt_platforms", set()):
            return event, []

        # --- Assemble the screenable fields (only non-empty) ---
        fields: dict[str, str] = {}

        body = getattr(event, "text", None)
        if body:
            fields["body"] = body

        sender_name = getattr(source, "user_name", None)
        if sender_name:
            fields["sender_name"] = sender_name

        if cfg.get("screen_reply_quote"):
            reply_quote = getattr(event, "reply_to_text", None)
            if reply_quote:
                fields["reply_quote"] = reply_quote

        if cfg.get("screen_attachment_names"):
            media_urls = getattr(event, "media_urls", None) or []
            names = [os.path.basename(str(u)) for u in media_urls if u]
            names = [n for n in names if n]
            if names:
                fields["attachment_names"] = " ".join(names)

        if not fields:
            return event, []

        results = screen_inbound_batch(
            fields,
            source=(platform_value or "unknown"),
            classifier=_degrading_batch_classifier,
        )

        not_allowed = [
            name
            for name, res in results.items()
            if not getattr(res, "allowed", True)
        ]
        if not not_allowed:
            return event, []

        # --- OBSERVE: log only, deliver unmodified ---
        if mode == "observe":
            for name in not_allowed:
                reason = getattr(results[name], "reason", "?")
                logger.error(
                    "inbound[observe] WOULD-QUARANTINE field=%s source=%s — %s",
                    name,
                    platform_value,
                    reason,
                )
                _inbound_log(
                    f"WOULD-QUARANTINE\t{platform_value}\t{name}\t{reason}"
                )
            return event, []

        # --- BLOCK: redact in place, still deliver ---
        quarantined: list[str] = []
        for name in not_allowed:
            reason = getattr(results[name], "reason", "screened")
            if name == "body":
                event.text = _body_placeholder(reason)
            elif name == "sender_name":
                if source is not None:
                    source.user_name = ""
            elif name == "reply_quote":
                event.reply_to_text = ""
            elif name == "attachment_names":
                # No 1:1 event field: the filename is the payload, but media_urls
                # is the actual file the agent needs. Blanking media_urls would
                # DROP the attachment. Least-destructive: log + record, but leave
                # media_urls intact (treat like observe for this one field).
                pass
            quarantined.append(name)
            logger.error(
                "inbound[block] QUARANTINED field=%s source=%s — %s",
                name,
                platform_value,
                reason,
            )
            _inbound_log(f"QUARANTINED\t{platform_value}\t{name}\t{reason}")
        return event, quarantined
    except Exception as exc:
        # Fail-safe: screening must never crash the gateway or drop a message.
        logger.warning("inbound_screen error (fail-safe pass-through): %s", exc)
        try:
            _inbound_log(f"ERROR\tfail-safe-passthrough\t{exc}")
        except Exception:
            pass
        return event, []
