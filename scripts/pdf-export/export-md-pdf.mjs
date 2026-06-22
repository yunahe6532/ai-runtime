#!/usr/bin/env node
/**
 * Markdown + Mermaid → PDF
 * Usage: node export-md-pdf.mjs <input.md> [output.pdf]
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";
import puppeteer from "puppeteer";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MMDC = path.join(__dirname, "node_modules", ".bin", "mmdc");
const MMDC_CONFIG = path.join(__dirname, "mermaid-config.json");
const NOTO_CSS = path.join(
  __dirname,
  "node_modules",
  "@fontsource",
  "noto-sans-kr",
  "400.css",
);

const inputMd = path.resolve(process.argv[2] || "../../docs/VISION.md");
const outputPdf = path.resolve(
  process.argv[3] || inputMd.replace(/\.md$/i, ".pdf"),
);

if (!fs.existsSync(inputMd)) {
  console.error(`Input not found: ${inputMd}`);
  process.exit(1);
}

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "md-pdf-"));
const md = fs.readFileSync(inputMd, "utf8");

function padSvg(svg, pad = 32) {
  const vbMatch = svg.match(/viewBox="([^"]+)"/);
  if (!vbMatch) return svg;
  const parts = vbMatch[1].trim().split(/[\s,]+/).map(Number);
  if (parts.length !== 4 || parts.some((n) => Number.isNaN(n))) return svg;
  const [x, y, w, h] = parts;
  return svg
    .replace(/viewBox="[^"]+"/, `viewBox="${x - pad} ${y - pad} ${w + pad * 2} ${h + pad * 2}"`)
    .replace(/\sstyle="max-width:[^"]*"/g, "");
}

function renderMermaid(code, index) {
  const mmdPath = path.join(tmpDir, `diagram-${index}.mmd`);
  const svgPath = path.join(tmpDir, `diagram-${index}.svg`);
  fs.writeFileSync(mmdPath, code.trim(), "utf8");
  execFileSync(
    MMDC,
    ["-i", mmdPath, "-o", svgPath, "-b", "transparent", "-c", MMDC_CONFIG],
    { stdio: "pipe" },
  );
  return padSvg(fs.readFileSync(svgPath, "utf8"));
}

function mdToHtmlBody(source) {
  const re = /```mermaid\n([\s\S]*?)```/g;
  const parts = [];
  let last = 0;
  let match;
  let diagramIndex = 0;

  while ((match = re.exec(source)) !== null) {
    if (match.index > last) {
      parts.push({ type: "md", text: source.slice(last, match.index) });
    }
    parts.push({ type: "mermaid", text: match[1] });
    last = re.lastIndex;
  }
  if (last < source.length) {
    parts.push({ type: "md", text: source.slice(last) });
  }

  let html = "";
  for (const part of parts) {
    if (part.type === "md") {
      html += marked.parse(part.text, { async: false });
      continue;
    }
    const svg = renderMermaid(part.text, diagramIndex++);
    html += `<figure class="diagram">${svg}</figure>`;
  }
  return { html, diagramCount: diagramIndex };
}

const { html: bodyHtml, diagramCount } = mdToHtmlBody(md);
const title = path.basename(inputMd, ".md");
const fontCss = fs.readFileSync(NOTO_CSS, "utf8").replace(
  /url\(([^)]+)\)/g,
  (_m, rel) => {
    const abs = path.resolve(path.dirname(NOTO_CSS), rel).replace(/\\/g, "/");
    return `url(file://${abs})`;
  },
);

const htmlDoc = `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>${title}</title>
  <style>
    ${fontCss}
    @page {
      size: A4;
      margin: 18mm 16mm 20mm 16mm;
    }
    * { box-sizing: border-box; }
    body {
      font-family: "Noto Sans KR", sans-serif;
      font-size: 10.5pt;
      line-height: 1.65;
      color: #1f2937;
      max-width: 100%;
    }
    h1 {
      font-size: 22pt;
      margin: 0 0 0.6em;
      color: #111827;
      border-bottom: 2px solid #48bb78;
      padding-bottom: 0.25em;
    }
    h2 {
      font-size: 15pt;
      margin: 1.6em 0 0.5em;
      color: #1a4731;
      page-break-after: avoid;
    }
    h3 {
      font-size: 12pt;
      margin: 1.2em 0 0.4em;
      page-break-after: avoid;
    }
    p, ul, ol { margin: 0.5em 0 0.8em; }
    ul, ol { padding-left: 1.4em; }
    hr {
      border: none;
      border-top: 1px solid #e5e7eb;
      margin: 1.5em 0;
    }
    blockquote {
      margin: 1em 0;
      padding: 0.6em 1em;
      border-left: 4px solid #48bb78;
      background: #f0fff4;
      color: #374151;
    }
    blockquote p { margin: 0.3em 0; }
    table {
      width: 100%;
      border-collapse: collapse;
      margin: 1em 0;
      font-size: 9.5pt;
      page-break-inside: avoid;
    }
    th, td {
      border: 1px solid #d1d5db;
      padding: 6px 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #f3f4f6;
      font-weight: 600;
    }
    tr:nth-child(even) td { background: #fafafa; }
    code {
      font-family: "JetBrains Mono", "Consolas", monospace;
      font-size: 0.9em;
      background: #f3f4f6;
      padding: 0.1em 0.35em;
      border-radius: 3px;
    }
    pre {
      background: #1f2937;
      color: #e5e7eb;
      padding: 12px 14px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 8.5pt;
      line-height: 1.45;
      page-break-inside: avoid;
    }
    pre code {
      background: transparent;
      color: inherit;
      padding: 0;
    }
    a { color: #2563eb; text-decoration: none; }
    figure.diagram {
      margin: 1.2em 0 1.6em;
      padding: 10px 0;
      text-align: center;
      page-break-inside: avoid;
    }
    figure.diagram svg {
      max-width: 100%;
      height: auto;
    }
    h2, h3 { page-break-inside: avoid; }
  </style>
</head>
<body>
${bodyHtml}
</body>
</html>`;

const htmlPath = path.join(tmpDir, "document.html");
fs.writeFileSync(htmlPath, htmlDoc, "utf8");

console.log(`Rendering ${diagramCount} Mermaid diagram(s)...`);

const browser = await puppeteer.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-setuid-sandbox", "--font-render-hinting=medium"],
});
const page = await browser.newPage();
await page.goto(`file://${htmlPath}`, { waitUntil: "networkidle0", timeout: 120_000 });
await page.pdf({
  path: outputPdf,
  format: "A4",
  printBackground: true,
  margin: { top: "18mm", bottom: "20mm", left: "16mm", right: "16mm" },
});
await browser.close();

fs.rmSync(tmpDir, { recursive: true, force: true });
console.log(`PDF saved: ${outputPdf}`);
