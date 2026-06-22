"""Message section classification and compression helpers for context indexing.

Extracted from legacy context_optimizer — used by context_cache only.
"""

from __future__ import annotations

import re
from enum import Enum

from capture import _content_text

_FILE_RE = re.compile(
    r"([\w./~-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|json|sh|go|rs|java|kt|cpp|h|toml))",
    re.I,
)
MAX_TOOL_SUMMARY_CHARS = 280
MAX_ERROR_KEEP_CHARS = 600


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


def _is_empty_assistant(msg: dict) -> bool:
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


def classify_message(msg: dict, index: int) -> Section:
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
