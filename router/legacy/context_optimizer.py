"""Context optimizer: Lite Pack (section classifier) + session cache.

.. deprecated::
    Legacy path (Tier 5). Prefer ``dynamic_context_scheduler`` + ``allocate_dynamic``
    when ``DYNAMIC_BUDGET=1``. See ``docs/MODULE_MAP.md`` and ``router/legacy/``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from capture import _content_text

LOG = logging.getLogger("router.optimizer")

OPTIMIZER_ENABLED = os.getenv("CONTEXT_OPTIMIZER", "1") == "1"
OPTIMIZER_MODE = os.getenv("OPTIMIZER_MODE", "lite_pack")  # lite_pack | hash
RECENT_USER_KEEP = int(os.getenv("OPTIMIZER_RECENT_USER_KEEP", "2"))
RECENT_ASSISTANT_KEEP = int(os.getenv("OPTIMIZER_RECENT_ASSISTANT_KEEP", "1"))
MAX_TOOL_SUMMARY_CHARS = int(os.getenv("OPTIMIZER_TOOL_SUMMARY_CHARS", "280"))
MAX_ERROR_KEEP_CHARS = int(os.getenv("OPTIMIZER_ERROR_KEEP_CHARS", "600"))
PLACEHOLDER_MAX_PREVIEW = int(os.getenv("OPTIMIZER_PREVIEW_CHARS", "120"))

_FILE_RE = re.compile(
    r"([\w./~-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|json|sh|go|rs|java|kt|cpp|h|toml))",
    re.I,
)


class Section(str, Enum):
    SYSTEM = "system"
    USER_INFO = "user_info"
    USER_RULES = "user_rules"
    AGENT_SKILLS = "agent_skills"
    WORKSPACE_STATE = "workspace_state"
    SUMMARY = "summary"
    USER_QUERY = "user_query"
    STALE_USER = "stale_user"
    TOOL = "tool"
    ASSISTANT = "assistant"
    EMPTY_ASSISTANT = "empty_assistant"
    OTHER = "other"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _message_hash(msg: dict[str, Any]) -> str:
    from message_index import stable_message_key

    return stable_message_key(msg)


def _est_tokens(text: str) -> int:
    return max(0, len(text) // 3)


def _est_message_tokens(body: dict[str, Any]) -> int:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 0
    return sum(_est_tokens(_content_text(m.get("content", ""))) for m in messages if isinstance(m, dict))


def _est_body_tokens(body: dict[str, Any]) -> int:
    total = _est_message_tokens(body)
    tools = body.get("tools")
    if isinstance(tools, list):
        total += _est_tokens(json.dumps(tools, ensure_ascii=False))
    return total


def _is_empty_assistant(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant":
        return False
    if msg.get("tool_calls"):
        return False
    text = _content_text(msg.get("content", "")).strip()
    return not text or text in ("[]", "{}")


def _extract_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def _extract_user_query(text: str) -> str:
    q = _extract_tag(text, "user_query")
    if q:
        return q
    if "<user_query>" in text:
        return text.split("<user_query>", 1)[-1].split("</user_query>", 1)[0].strip()
    return text.strip()


def classify_message(msg: dict[str, Any], index: int) -> Section:
    role = msg.get("role", "")
    text = _content_text(msg.get("content", ""))

    if role == "system":
        return Section.SYSTEM
    if role == "assistant":
        return Section.EMPTY_ASSISTANT if _is_empty_assistant(msg) else Section.ASSISTANT
    if role == "tool":
        return Section.TOOL

    if role == "user":
        if "<user_info>" in text:
            return Section.USER_INFO
        if "<user_rules>" in text or ("<rules>" in text and "user_rule" in text):
            return Section.USER_RULES
        if "<agent_skills>" in text or "<available_skills" in text:
            return Section.AGENT_SKILLS
        if "Your conversation was summarized" in text or "<summary_content>" in text:
            return Section.SUMMARY
        if "<open_and_recently_viewed_files>" in text:
            if "<user_query>" in text:
                return Section.USER_QUERY
            return Section.WORKSPACE_STATE
        if "<user_query>" in text:
            return Section.USER_QUERY
        if "<agent_transcripts>" in text and index <= 3:
            return Section.USER_INFO
        return Section.STALE_USER

    return Section.OTHER


def _compress_system(text: str) -> str:
    lines = [
        "You are a coding assistant in Cursor (local LLM).",
        "- Follow the latest <user_query> as the primary task.",
        "- Read files before editing; apply minimal, focused changes.",
        "- Verify changes when practical; do not narrate obvious code.",
        "- Use tools for real work; communicate in response text only.",
        "- Preserve OpenAI-compatible tool calling behavior.",
    ]
    if "citing_code" in text or "CODE REFERENCES" in text:
        lines.append("- Use ```startLine:endLine:filepath for existing code citations.")
    if "Korean" in text or "한국어" in text:
        lines.append("- Respond in Korean when user rules require it.")
    return "\n".join(lines)


def _compress_rules(text: str) -> str:
    rules: list[str] = []
    patterns = [
        (r"한국어", "한국어로 응답"),
        (r"최소 변경", "최소 변경 원칙"),
        (r"추측", "추측보다 확인 우선"),
        (r"main 직접", "main 브랜치 직접 작업 금지"),
        (r"WSL", "WSL/Linux CLI 우선"),
        (r"handoff", "handoff.md 확인/갱신"),
        (r"pytest|test", "변경 후 테스트 검증"),
        (r"시크릿|\.env", "시크릿/운영 설정 보호"),
    ]
    for pat, label in patterns:
        if re.search(pat, text, re.I):
            rules.append(f"- {label}")
    if not rules:
        rules = ["- 사용자 규칙 준수", "- 범위 밖 변경 금지", "- 기존 패턴 우선"]
    return "[고정 규칙 요약]\n" + "\n".join(rules[:12])


def _compress_skills(text: str, task_hint: str) -> str:
    paths = re.findall(r'fullPath="([^"]+SKILL\.md)"', text)
    if not paths:
        return ""
    relevant = [p for p in paths if any(k in task_hint.lower() for k in ("router", "docker", "benchmark", "cursor", "llm"))]
    chosen = relevant[:3] if relevant else []
    if not chosen:
        return "[skills] omitted (not directly relevant to current task)"
    return "[skills] relevant only:\n" + "\n".join(f"- {p}" for p in chosen)


def _extract_workspace_files(text: str) -> list[str]:
    files: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("- ").strip()
        if line.startswith("/") or line.startswith("~/") or "/" in line:
            m = _FILE_RE.search(line)
            if m:
                files.append(m.group(1))
            elif ". " in line:
                files.append(line.split(". ", 1)[-1].strip())
    return list(dict.fromkeys(files))[:12]


def _summarize_tool(text: str, msg_hash: str) -> str:
    short = msg_hash[:8]
    if text.strip().startswith("Error:") or "Traceback" in text[:400]:
        return f"[tool error hash={short}]\n{text[:MAX_ERROR_KEEP_CHARS]}"
    if "<workspace_result" in text:
        files = list(dict.fromkeys(_FILE_RE.findall(text)))[:5]
        lines = len(text.splitlines())
        preview = text[:120].replace("\n", " ")
        return f"[tool grep hash={short} files={files} lines={lines}] {preview}"
    if "Result of search in" in text[:200]:
        return f"[tool glob hash={short}]\n{text[:MAX_TOOL_SUMMARY_CHARS]}"
    if text.strip().startswith("#!/"):
        first = text.splitlines()[0][:80]
        return f"[tool file hash={short} lines={len(text.splitlines())}] {first}"
    preview = text[:MAX_TOOL_SUMMARY_CHARS].replace("\n", " ")
    return f"[tool observation hash={short} chars={len(text)}] {preview}"


def _compress_summary(text: str) -> str:
    if "<summary_content>" in text:
        body = _extract_tag(text, "summary_content")
        if body:
            return "[prior context summary]\n" + body[:900]
    m = re.search(r"Here is the summary.*?\n\n(.*)", text, re.S)
    if m:
        return "[prior context summary]\n" + m.group(1)[:900]
    return "[prior context summary]\n" + text[:900]


def session_id(body: dict[str, Any]) -> str:
    user = str(body.get("user", "anonymous"))
    model = str(body.get("model", "unknown"))
    tools = body.get("tools", [])
    tools_blob = json.dumps(tools, sort_keys=True, ensure_ascii=False) if isinstance(tools, list) else ""
    return _sha256(f"{user}|{model}|{_sha256(tools_blob)}")[:24]


@dataclass
class SessionCache:
    request_count: int = 0
    tool_cache: dict[str, str] = field(default_factory=dict)
    section_hashes: dict[str, str] = field(default_factory=dict)
    message_hashes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class OptimizeStats:
    raw_tokens: int = 0
    optimized_tokens: int = 0
    mode: str = OPTIMIZER_MODE
    cached_tool_msgs: int = 0
    kept_tool_msgs: int = 0
    fixed_blocks_replaced: int = 0
    stale_user_replaced: int = 0
    empty_assistants_removed: int = 0
    recent_msgs_kept: int = 0
    fallback: bool = False
    session_id: str = ""
    request_num: int = 0
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def saved_pct(self) -> float:
        if self.raw_tokens <= 0:
            return 0.0
        return round(100 * (1 - self.optimized_tokens / self.raw_tokens), 1)


class ContextOptimizer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionCache] = {}

    def _get_cache(self, sid: str) -> SessionCache:
        with self._lock:
            if sid not in self._sessions:
                self._sessions[sid] = SessionCache()
            return self._sessions[sid]

    def optimize(self, body: dict[str, Any]) -> tuple[dict[str, Any], OptimizeStats]:
        stats = OptimizeStats()
        if not OPTIMIZER_ENABLED:
            stats.raw_tokens = _est_body_tokens(body)
            stats.optimized_tokens = stats.raw_tokens
            return body, stats
        try:
            if OPTIMIZER_MODE == "lite_pack":
                return self._optimize_lite_pack(body, stats)
            return self._optimize_hash_mode(body, stats)
        except Exception:
            LOG.exception("optimizer fallback to original payload")
            stats.fallback = True
            stats.raw_tokens = _est_body_tokens(body)
            stats.optimized_tokens = stats.raw_tokens
            return body, stats

    def _optimize_lite_pack(self, body: dict[str, Any], stats: OptimizeStats) -> tuple[dict[str, Any], OptimizeStats]:
        out = copy.deepcopy(body)
        messages = out.get("messages")
        if not isinstance(messages, list):
            stats.raw_tokens = _est_body_tokens(body)
            stats.optimized_tokens = stats.raw_tokens
            return out, stats

        stats.raw_tokens = _est_body_tokens(body)
        sid = session_id(body)
        stats.session_id = sid
        cache = self._get_cache(sid)
        stats.request_num = cache.request_count + 1

        classified: list[tuple[Section, dict[str, Any], str, str]] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            section = classify_message(msg, i)
            if section == Section.EMPTY_ASSISTANT:
                stats.empty_assistants_removed += 1
                continue
            text = _content_text(msg.get("content", ""))
            classified.append((section, msg, text, _message_hash(msg)))

        user_queries = [(i, t) for i, (s, _, t, _) in enumerate(classified) if s == Section.USER_QUERY]
        last_query_text = _extract_user_query(user_queries[-1][1]) if user_queries else ""
        task_hint = last_query_text

        recent_user_idxs = {i for i, _ in user_queries[-RECENT_USER_KEEP:]}
        assistant_idxs = [i for i, (s, _, _, _) in enumerate(classified) if s == Section.ASSISTANT]
        recent_asst_idxs = set(assistant_idxs[-RECENT_ASSISTANT_KEEP:])

        workspace_files: list[str] = []
        tool_lines: list[str] = []
        cache_refs: list[str] = []
        project_state: list[str] = []
        kept_recent_blocks: list[str] = []

        section_actions: dict[str, str] = {}

        for i, (section, msg, text, msg_hash) in enumerate(classified):
            if section == Section.SYSTEM:
                section_actions["system"] = "compressed"
                continue
            if section == Section.USER_INFO:
                section_actions["user_info"] = "compressed"
                continue
            if section == Section.USER_RULES:
                section_actions["rules"] = "compressed"
                continue
            if section == Section.AGENT_SKILLS:
                section_actions["skills"] = "dropped"
                continue
            if section == Section.WORKSPACE_STATE:
                workspace_files.extend(_extract_workspace_files(text))
                section_actions.setdefault("workspace", "compressed")
                continue
            if section == Section.SUMMARY:
                project_state.append(_compress_summary(text))
                section_actions["summary"] = "kept"
                continue
            if section == Section.USER_QUERY:
                if i in recent_user_idxs or i == user_queries[-1][0]:
                    q = _extract_user_query(text)
                    if i == user_queries[-1][0]:
                        section_actions["current_query"] = "kept"
                    else:
                        kept_recent_blocks.append(f"[recent user]\n{q[:1500]}")
                        stats.recent_msgs_kept += 1
                else:
                    stats.stale_user_replaced += 1
                continue
            if section == Section.STALE_USER:
                stats.stale_user_replaced += 1
                continue
            if section == Section.ASSISTANT:
                if i in recent_asst_idxs:
                    kept_recent_blocks.append(f"[recent assistant]\n{text[:2000]}")
                    stats.recent_msgs_kept += 1
                    section_actions.setdefault("assistant", "kept")
                else:
                    section_actions.setdefault("assistant", "compressed")
                continue
            if section == Section.TOOL:
                if msg_hash in cache.tool_cache:
                    tool_lines.append(cache.tool_cache[msg_hash])
                    stats.cached_tool_msgs += 1
                    section_actions["tools"] = "cached"
                else:
                    summary = _summarize_tool(text, msg_hash)
                    cache.tool_cache[msg_hash] = summary
                    # Keep recent tool errors verbatim-ish
                    if i >= len(classified) - RECENT_USER_KEEP * 3 and (
                        text.strip().startswith("Error:") or "Traceback" in text[:300]
                    ):
                        tool_lines.append(summary)
                        stats.kept_tool_msgs += 1
                        section_actions["tools"] = "kept_errors"
                    else:
                        tool_lines.append(summary)
                        stats.cached_tool_msgs += 1
                        section_actions["tools"] = "cached"

        system_text = ""
        rules_text = ""
        skills_text = ""
        for section, _, text, h in classified:
            if section == Section.SYSTEM and not system_text:
                system_text = _compress_system(text)
                cache.section_hashes["system"] = h
            elif section == Section.USER_INFO:
                info = _extract_tag(text, "user_info")
                if info and "environment" not in "".join(project_state):
                    project_state.insert(0, f"[environment]\n{info[:500]}")
                if "<user_rules>" in text or "<rules>" in text:
                    rules_text = _compress_rules(text)
                    cache.section_hashes["rules"] = h
                    section_actions["rules"] = "compressed"
                if "<agent_skills>" in text or "<available_skills" in text:
                    skills_text = _compress_skills(text, task_hint)
                    section_actions["skills"] = "dropped" if not skills_text else "compressed"
                section_actions.setdefault("user_info", "compressed")
            elif section == Section.USER_RULES and not rules_text:
                rules_text = _compress_rules(text)
                cache.section_hashes["rules"] = h
            elif section == Section.AGENT_SKILLS and not skills_text:
                skills_text = _compress_skills(text, task_hint)

        if workspace_files:
            project_state.append("[recent files]\n" + "\n".join(f"- {f}" for f in dict.fromkeys(workspace_files)[:10]))

        for key, h in cache.section_hashes.items():
            cache_refs.append(f"- {key}: hash={h[:8]}")

        pack_parts = [
            rules_text,
            "",
            "[현재 작업 목표]",
            last_query_text or "(no explicit user query detected)",
            "",
            "[현재 프로젝트 상태]",
            "\n\n".join(project_state) if project_state else "- (no extra project state)",
            "",
            "[최근 관련 파일]",
            "\n".join(f"- {f}" for f in dict.fromkeys(workspace_files)[:8]) if workspace_files else "- (none)",
            "",
            "[tool observations]",
            "\n".join(tool_lines[-12:]) if tool_lines else "- (none)",
            "",
            "[필요 시 참조 가능한 캐시]",
            "\n".join(cache_refs) if cache_refs else "- (first request)",
        ]
        if skills_text:
            pack_parts.insert(1, skills_text)
        if kept_recent_blocks:
            pack_parts.extend(["", "[최근 대화]", "\n\n".join(kept_recent_blocks)])

        pack_parts.extend(["", "[이번 user_query 원문]", last_query_text or ""])

        task_pack = "\n".join(pack_parts).strip()
        stats.fixed_blocks_replaced = sum(
            1 for k in ("system", "rules", "user_info", "skills", "workspace") if k in section_actions
        )

        out["messages"] = [
            {"role": "system", "content": system_text or _compress_system("")},
            {"role": "user", "content": task_pack},
        ]
        stats.sections = section_actions
        cache.request_count += 1
        stats.optimized_tokens = _est_body_tokens(out)
        return out, stats

    # Legacy hash mode kept for rollback
    def _optimize_hash_mode(self, body: dict[str, Any], stats: OptimizeStats) -> tuple[dict[str, Any], OptimizeStats]:
        out = copy.deepcopy(body)
        messages = out.get("messages")
        if not isinstance(messages, list):
            stats.raw_tokens = _est_body_tokens(body)
            stats.optimized_tokens = stats.raw_tokens
            return out, stats

        stats.raw_tokens = _est_body_tokens(body)
        sid = session_id(body)
        stats.session_id = sid
        cache = self._get_cache(sid)
        stats.request_num = cache.request_count + 1
        is_first = cache.request_count == 0

        filtered = [m for m in messages if isinstance(m, dict) and not _is_empty_assistant(m)]
        stats.empty_assistants_removed = len(messages) - len(filtered)

        last_user = next((i for i in range(len(filtered) - 1, -1, -1) if filtered[i].get("role") == "user"), None)
        recent_start = max(0, len(filtered) - int(os.getenv("OPTIMIZER_RECENT_KEEP", "6")))
        optimized: list[dict[str, Any]] = []
        seen = set(cache.message_hashes.keys())
        for idx, msg in enumerate(filtered):
            h = _message_hash(msg)
            role = str(msg.get("role", ""))
            if is_first or idx >= recent_start or idx == last_user:
                optimized.append(msg)
                cache.message_hashes.setdefault(h, {"role": role})
                continue
            if role == "tool" and h in seen:
                optimized.append({"role": "tool", "content": f"[cached tool hash={h[:8]}]"})
                stats.cached_tool_msgs += 1
            else:
                optimized.append(msg)
                cache.message_hashes.setdefault(h, {"role": role})

        cache.request_count += 1
        out["messages"] = optimized
        stats.optimized_tokens = _est_body_tokens(out)
        return out, stats


OPTIMIZER = ContextOptimizer()


def maybe_optimize(body: dict[str, Any] | None) -> tuple[dict[str, Any] | None, OptimizeStats | None]:
    if not body or not OPTIMIZER_ENABLED:
        return body, None
    if not body.get("messages"):
        return body, None
    optimized, stats = OPTIMIZER.optimize(body)
    sections = " ".join(f"{k}={v}" for k, v in sorted(stats.sections.items()))
    LOG.info(
        "optimizer mode=%s req=%d raw_tokens=%d optimized_tokens=%d saved=%.1f%% "
        "cached_tool_msgs=%d kept_tool_msgs=%d fixed_blocks_replaced=%d stale_user_replaced=%d "
        "recent_msgs_kept=%d empty_assistants_removed=%d fallback=%s session=%s "
        "sections=%s",
        stats.mode,
        stats.request_num,
        stats.raw_tokens,
        stats.optimized_tokens,
        stats.saved_pct,
        stats.cached_tool_msgs,
        stats.kept_tool_msgs,
        stats.fixed_blocks_replaced,
        stats.stale_user_replaced,
        stats.recent_msgs_kept,
        stats.empty_assistants_removed,
        str(stats.fallback).lower(),
        stats.session_id[:12],
        sections or "-",
    )
    return optimized, stats
