#!/usr/bin/env node
/**
 * Markdown → HTML (GitHub MD CSS + browser Mermaid, Cursor preview 방식)
 * Usage: node export-md-html.mjs <input.md> [output.html]
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const inputMd = path.resolve(process.argv[2] || "../../docs/VISION.md");
const outputHtml = path.resolve(
  process.argv[3] || inputMd.replace(/\.md$/i, ".html"),
);

if (!fs.existsSync(inputMd)) {
  console.error(`Input not found: ${inputMd}`);
  process.exit(1);
}

const md = fs.readFileSync(inputMd, "utf8");
const generatedAt = new Date().toISOString().slice(0, 10);
const title = path.basename(inputMd, ".md");

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function githubSlug(text) {
  return text
    .replace(/<[^>]*>/g, "")
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-");
}

marked.use({
  gfm: true,
  renderer: {
    heading({ text, depth }) {
      const id = githubSlug(text.replace(/<[^>]*>/g, ""));
      return `<h${depth} id="${id}">${text}</h${depth}>\n`;
    },
  },
});

function mdToHtmlBody(source) {
  const re = /```mermaid\n([\s\S]*?)```/g;
  const parts = [];
  let last = 0;
  let match;
  let n = 0;

  while ((match = re.exec(source)) !== null) {
    if (match.index > last) parts.push({ type: "md", text: source.slice(last, match.index) });
    parts.push({ type: "mermaid", text: match[1].trimEnd() });
    last = re.lastIndex;
  }
  if (last < source.length) parts.push({ type: "md", text: source.slice(last) });

  let html = "";
  for (const part of parts) {
    if (part.type === "md") {
      html += marked.parse(part.text, { async: false });
      continue;
    }
    n += 1;
    html += `<div class="mermaid-wrap"><pre class="mermaid" id="mmd-${n}">${escapeHtml(part.text)}</pre></div>\n`;
  }
  return { html, diagramCount: n };
}

const { html: bodyHtml, diagramCount } = mdToHtmlBody(md);

const htmlDoc = `<!DOCTYPE html>
<html lang="ko" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${title}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.8.1/github-markdown-light.min.css" id="gh-md-light" />
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.8.1/github-markdown-dark.min.css" id="gh-md-dark" disabled />
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    body {
      margin: 0;
      font-family: "Noto Sans KR", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #fff;
      color-scheme: light;
    }
    html[data-theme="dark"] body { background: #0d1117; color-scheme: dark; }

    .toolbar {
      position: sticky; top: 0; z-index: 10;
      display: flex; align-items: center; justify-content: space-between;
      padding: 0.55rem 1.25rem;
      border-bottom: 1px solid #d0d7de;
      background: #f6f8fa;
      font-size: 0.875rem;
    }
    html[data-theme="dark"] .toolbar {
      border-color: #30363d; background: #161b22; color: #e6edf3;
    }
    .toolbar button {
      font: inherit; cursor: pointer;
      border: 1px solid #d0d7de; background: #fff;
      border-radius: 6px; padding: 0.2rem 0.55rem;
    }
    html[data-theme="dark"] .toolbar button {
      border-color: #30363d; background: #21262d; color: #e6edf3;
    }
    .toolbar a { color: inherit; opacity: 0.65; text-decoration: none; }

    .page { max-width: 980px; margin: 0 auto; padding: 2rem 1.5rem 4rem; }
    .markdown-body { min-width: 0; }

    /* Cursor MD preview 스타일 Mermaid 카드 */
    .markdown-body .mermaid-wrap {
      margin: 0.85rem 0 1.25rem;
      padding: 0.75rem;
      border-radius: 6px;
      border: 1px solid #d0d7de;
      background: #f6f8fa;
      overflow-x: auto;
      overflow-y: hidden;
    }
    html[data-theme="dark"] .markdown-body .mermaid-wrap {
      border-color: #30363d; background: #161b22;
    }
    .markdown-body pre.mermaid {
      margin: 0; padding: 0; background: transparent; border: 0;
      font-family: inherit; text-align: center;
    }
    /* Mermaid가 그린 SVG — IDE처럼 컨테이너에 맞춤 (축소 눌림 없음) */
    .markdown-body .mermaid-wrap svg {
      display: block;
      margin: 0 auto;
      max-width: 100%;
      height: auto;
    }

    .doc-meta {
      margin-top: 2rem; padding-top: 1rem;
      border-top: 1px solid #d0d7de;
      font-size: 0.8rem; opacity: 0.6;
    }
    html[data-theme="dark"] .doc-meta { border-color: #30363d; }
  </style>
</head>
<body>
  <header class="toolbar">
    <strong>${title}</strong>
    <span>${generatedAt} · Mermaid ${diagramCount}개</span>
    <div>
      <button type="button" id="theme-toggle">🌓</button>
      <a href="#top">↑</a>
    </div>
  </header>
  <div class="page" id="top">
    <article class="markdown-body">
${bodyHtml}
      <p class="doc-meta">GitHub Markdown CSS + Mermaid.js (browser) · <code>${path.basename(inputMd)}</code></p>
    </article>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <script>
    const SOURCES = [];
    document.querySelectorAll("pre.mermaid").forEach((el, i) => {
      SOURCES[i] = el.textContent.trim();
    });

    function theme() {
      return document.documentElement.dataset.theme === "dark" ? "dark" : "default";
    }

    function applyTheme(t) {
      document.documentElement.dataset.theme = t;
      document.getElementById("gh-md-light").disabled = t === "dark";
      document.getElementById("gh-md-dark").disabled = t !== "dark";
    }

    const saved = localStorage.getItem("md-preview-theme");
    applyTheme(saved === "light" || saved === "dark"
      ? saved
      : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

    document.getElementById("theme-toggle").onclick = async () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      localStorage.setItem("md-preview-theme", next);
      await renderAll();
    };

    async function renderAll() {
      document.querySelectorAll("pre.mermaid").forEach((el, i) => {
        el.innerHTML = SOURCES[i];
        el.removeAttribute("data-processed");
      });

      mermaid.initialize({
        startOnLoad: false,
        theme: theme(),
        securityLevel: "loose",
        fontFamily: '"Noto Sans KR", "trebuchet ms", verdana, arial, sans-serif',
        flowchart: {
          htmlLabels: true,
          curve: "basis",
          padding: 10,
          nodeSpacing: 35,
          rankSpacing: 40,
          useMaxWidth: true,
          wrappingWidth: 180,
        },
        state: {
          nodeSpacing: 30,
          rankSpacing: 35,
          useMaxWidth: true,
        },
        sequence: {
          useMaxWidth: true,
          wrap: true,
          width: 120,
          height: 50,
          boxMargin: 8,
        },
        themeVariables: {
          fontSize: "14px",
        },
      });

      await mermaid.run({ querySelector: ".mermaid" });
    }

    renderAll().catch((e) => {
      console.error(e);
      document.body.insertAdjacentHTML("beforeend",
        '<p style="color:red;padding:1rem">Mermaid 로드 실패 — 인터넷 연결 확인 또는 <code>./scripts/serve-vision-html.sh</code></p>');
    });
  </script>
</body>
</html>`;

fs.writeFileSync(outputHtml, htmlDoc, "utf8");
console.log(`HTML saved: ${outputHtml}`);
console.log(`Mermaid ${diagramCount}개 → 브라우저 렌더 (Cursor 방식)`);
console.log(`Open: file://${outputHtml}`);
