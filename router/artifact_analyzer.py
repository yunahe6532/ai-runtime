"""Structured analysis of tool results (HTML, shell, grep) for plan state."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any


class _HTMLChecker(HTMLParser):
  def __init__(self) -> None:
    super().__init__(convert_charrefs=True)
    self.errors: list[str] = []
    self.tags: list[str] = []

  def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
    self.tags.append(tag.lower())

  def handle_endtag(self, tag: str) -> None:
    self.tags.append(f"/{tag.lower()}")

  def error(self, message: str) -> None:
    self.errors.append(str(message))


def analyze_content(text: str, path: str = "", tool_name: str = "") -> dict[str, Any]:
  """Return structured metadata for a tool result."""
  stripped = (text or "").strip()
  path_lower = (path or "").lower()
  lines = stripped.splitlines() if stripped else []

  base: dict[str, Any] = {
    "path": path,
    "chars": len(text or ""),
    "lines": len(lines) or (1 if stripped else 0),
    "kind": "unknown",
  }

  if stripped.startswith("Error:") or stripped.startswith("error:"):
    base["kind"] = "error"
    base["error"] = stripped[:300]
    return base

  if "Exit code:" in stripped[:400] or tool_name == "Shell":
    base["kind"] = "shell"
    m = re.search(r"Exit code:\s*(\d+)", stripped)
    base["exit_code"] = int(m.group(1)) if m else None
    base["success"] = base.get("exit_code") == 0
    if "html_parse_errors" in stripped or "mermaid_blocks" in stripped:
      for key in (
        "html_parse_errors",
        "mermaid_blocks",
        "has_doctype",
        "closes_html",
        "has_charset",
        "lang_ko",
        "lines",
        "chars",
      ):
        km = re.search(rf'"{key}"\s*:\s*([^,\n}}]+)', stripped)
        if km:
          val = km.group(1).strip().strip('"')
          try:
            base[key] = json.loads(val) if val.startswith(("{", "[")) else val
          except json.JSONDecodeError:
            if val in ("true", "false"):
              base[key] = val == "true"
            elif val.isdigit():
              base[key] = int(val)
            else:
              base[key] = val
      base["kind"] = "html_validation"
      base["validation_ok"] = base.get("html_parse_errors", 1) == 0 and base.get("success", False)
    return base

  is_html = (
    path_lower.endswith((".html", ".htm"))
    or stripped.startswith("<!DOCTYPE")
    or stripped.startswith("<!doctype")
    or "<html" in stripped[:800].lower()
  )
  if is_html:
    return _analyze_html(stripped, path)

  if "<workspace_result" in stripped[:500]:
    base["kind"] = "grep"
    m = re.search(r"(\d+)\s+lines?\s+of\s+output", stripped, re.I)
    base["match_lines"] = int(m.group(1)) if m else len(lines)
    head = [ln for ln in lines[:6] if ln.strip()]
    base["preview_lines"] = head
    return base

  base["kind"] = "file"
  ext = path_lower.rsplit(".", 1)[-1] if "." in path_lower else ""
  base["extension"] = ext
  if lines:
    base["preview_lines"] = lines[:8]
  if "docker-compose" in path_lower or re.search(r"^\s*ports:", stripped, re.M):
    base["kind"] = "docker_compose"
    base.update(extract_compose_port_evidence(stripped))
  elif "ROUTER_PORT" in stripped or "PORT=" in stripped or re.search(r"^\s*ports:", stripped, re.M):
    port_ev = extract_compose_port_evidence(stripped)
    if port_ev:
      base.update(port_ev)
      base["port_evidence"] = True
  return base


def extract_compose_port_evidence(text: str) -> dict[str, Any]:
  """Extract router/host port mapping from docker-compose or cached file snippets."""
  out: dict[str, Any] = {}
  if not text:
    return out

  for pat in (
    r'ports:\s*\n\s*-\s*["\']?\$\{PORT:-(\d+)\}:(\d+)["\']?',
    r'ports:\s*\n\s*-\s*["\']?(\d+):(\d+)["\']?',
    r'-\s*["\']?\$\{PORT:-(\d+)\}:(\d+)["\']?',
  ):
    m = re.search(pat, text, re.I)
    if m:
      out["host_port"] = m.group(1)
      out["container_port"] = m.group(2)
      out["router_port"] = m.group(1)
      out["port_evidence"] = True
      break

  for pat in (
    r"ROUTER_PORT\s*=\s*(\d+)",
    r"ROUTER_PORT\s*:\s*(\d+)",
    r'\bPORT\s*=\s*(\d+)',
  ):
    m = re.search(pat, text, re.I)
    if m:
      out["router_port"] = m.group(1)
      out["port_evidence"] = True
      if "host_port" not in out:
        out["host_port"] = m.group(1)
      break

  env_port = re.search(r'PORT["\']?\s*:\s*["\']?(\d+)', text, re.I)
  if env_port and "router_port" not in out:
    out["router_port"] = env_port.group(1)
    out["port_evidence"] = True

  return out


def _analyze_html(text: str, path: str) -> dict[str, Any]:
  lines = text.splitlines()
  lower = text.lower()
  mermaid = len(re.findall(r'<pre[^>]*class=["\']mermaid["\']', text, re.I))
  mermaid += len(re.findall(r'class=["\']mermaid["\']', text, re.I))
  mermaid += text.count("```mermaid")

  parse_errors = 0
  checker = _HTMLChecker()
  try:
    checker.feed(text)
    checker.close()
    parse_errors = len(checker.errors)
  except Exception as exc:
    parse_errors = 1
    checker.errors.append(str(exc)[:120])

  has_doctype = "<!doctype" in lower[:300]
  closes_html = "</html>" in lower
  has_charset = "charset=" in lower[:3000]
  lang_ko = 'lang="ko"' in lower[:3000] or "lang='ko'" in lower[:3000]
  has_mermaid_cdn = "mermaid" in lower and ("cdn" in lower or "unpkg" in lower or "jsdelivr" in lower)

  validation_ok = (
    parse_errors == 0
    and has_doctype
    and closes_html
    and has_charset
  )

  return {
    "kind": "html",
    "path": path,
    "chars": len(text),
    "lines": len(lines),
    "mermaid_blocks": mermaid,
    "html_parse_errors": parse_errors,
    "has_doctype": has_doctype,
    "closes_html": closes_html,
    "has_charset": has_charset,
    "lang_ko": lang_ko,
    "has_mermaid_cdn": has_mermaid_cdn,
    "read_status": "cached",
    "validation_ok": validation_ok,
    "tag_sample": checker.tags[:12],
  }


def format_analysis_compact(analysis: dict[str, Any], max_chars: int = 900) -> str:
  """Token-efficient summary for proxy prompts."""
  kind = analysis.get("kind", "unknown")
  path = analysis.get("path", "")
  header = f"[artifact {kind}"
  if path:
    header += f" path={path}"
  header += "]"

  if kind == "html":
    body = (
      f"chars={analysis.get('chars')} lines={analysis.get('lines')} "
      f"mermaid_blocks={analysis.get('mermaid_blocks')} "
      f"html_parse_errors={analysis.get('html_parse_errors')} "
      f"doctype={analysis.get('has_doctype')} closes_html={analysis.get('closes_html')} "
      f"charset={analysis.get('has_charset')} lang_ko={analysis.get('lang_ko')} "
      f"validation_ok={analysis.get('validation_ok')}"
    )
  elif kind == "html_validation":
    body = (
      f"exit_code={analysis.get('exit_code')} "
      f"mermaid_blocks={analysis.get('mermaid_blocks')} "
      f"html_parse_errors={analysis.get('html_parse_errors')} "
      f"validation_ok={analysis.get('validation_ok')}"
    )
  elif kind == "shell":
    body = f"exit_code={analysis.get('exit_code')} success={analysis.get('success')}"
  elif kind == "grep":
    body = f"match_lines={analysis.get('match_lines', '?')}"
    preview = analysis.get("preview_lines") or []
    if preview:
      body += "\n" + "\n".join(str(x) for x in preview[:4])
  elif kind == "docker_compose":
    body = (
      f"router_port={analysis.get('router_port', '?')} "
      f"host_port={analysis.get('host_port', '?')} "
      f"container_port={analysis.get('container_port', '?')} "
      f"port_evidence={analysis.get('port_evidence', False)}"
    )
  elif kind == "error":
    body = str(analysis.get("error", ""))[:400]
  else:
    preview = analysis.get("preview_lines") or []
    body = f"chars={analysis.get('chars')} lines={analysis.get('lines')}"
    if preview:
      body += "\n" + "\n".join(str(x) for x in preview[:6])

  out = f"{header}\n{body}"
  if len(out) > max_chars:
    return out[: max_chars - 20] + "\n...(analysis truncated)"
  return out


def html_validation_command(path: str) -> str:
  """Allowlisted one-shot HTML structure check."""
  safe = path.replace("'", "").replace('"', "")
  return (
    "python3 -c \""
    "import json,re,sys; from html.parser import HTMLParser; "
    "p=sys.argv[1]; t=open(p,encoding='utf-8',errors='replace').read(); "
    "l=t.lower(); "
    "class C(HTMLParser): "
    " e=[]; "
    " def error(self,m): self.e.append(str(m)); "
    "c=C(); "
    "exec('try:\\n c.feed(t); c.close()\\nexcept Exception as ex:\\n c.e.append(str(ex))',"
    "{'C':C,'c':c,'t':t}); "
    "m=len(re.findall(r'class=[\\\"\\']mermaid[\\\"\\']',t,re.I)); "
    "print(json.dumps({"
    "'html_parse_errors':len(c.e),"
    "'mermaid_blocks':m,"
    "'has_doctype':'<!doctype' in l[:300],"
    "'closes_html':'</html>' in l,"
    "'has_charset':'charset=' in l[:3000],"
    "'lang_ko':'lang=\\\"ko\\\"' in l[:3000],"
    "'chars':len(t),"
    "'lines':t.count(chr(10))+1"
    "}))\" "
    f"'{safe}'"
  )
