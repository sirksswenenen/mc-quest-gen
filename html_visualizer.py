"""
Render an FTB-Quests-styled HTML preview from a quests.json file.

Output is a single self-contained .html file:
  - left sidebar lists chapters (mods) with their icons
  - center canvas draws quests as hexagonal nodes with dependency lines
  - clicking a quest opens a side panel with title / description / item icons
  - dark theme, mimics in-game FTB Quests UI

No external JS/CSS dependencies: everything is embedded inline.
Item icons are loaded directly from public CDNs (Modrinth + InventivetalentDev/minecraft-assets).
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import scraper

MC_ASSETS_BASE = (
    "https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/"
    "1.12.2/assets/minecraft/textures/items/"
)
MC_ASSETS_BLOCKS = (
    "https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/"
    "1.12.2/assets/minecraft/textures/blocks/"
)


# ─────────────────────────────────────────────
# Icon resolution
# ─────────────────────────────────────────────

_mod_icon_cache: dict[str, Optional[str]] = {}


def _modrinth_icon_for(modid: str, display_name: str = "") -> Optional[str]:
    """Best-effort lookup of a mod's icon URL — try CurseForge first (if
    a key is configured) then Modrinth. Result is cached per modid."""
    if modid in _mod_icon_cache:
        return _mod_icon_cache[modid]
    url: Optional[str] = None
    # Display name is usually richer than the bare modid, prefer it for search
    queries = [q for q in (display_name, modid,
                           modid.replace("_", " "),
                           modid.replace("-", " ")) if q]
    for q in queries:
        hit = scraper.search_curseforge(q, modid=modid)
        if hit and hit.get("icon_url"):
            url = hit["icon_url"]
            break
    if not url:
        for q in queries:
            hit = scraper.search_modrinth(q, modid=modid)
            if hit and hit.get("icon_url"):
                url = hit["icon_url"]
                break
    _mod_icon_cache[modid] = url
    return url


def _icon_url_for_item(item_id: str, mod_icon_url: Optional[str] = None) -> Optional[str]:
    """Return a URL for the item's icon (best effort), or None."""
    if not item_id or ":" not in item_id:
        return mod_icon_url
    modid, name = item_id.split(":", 1)
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if modid == "minecraft":
        return f"{MC_ASSETS_BASE}{name}.png"
    return mod_icon_url


# ─────────────────────────────────────────────
# Quest layout helpers
# ─────────────────────────────────────────────

NODE_SIZE = 56
NODE_GAP_X = 90
NODE_GAP_Y = 70
PAD_X = 80
PAD_Y = 80


def _bounds(quests: list[dict]) -> tuple[float, float, float, float]:
    if not quests:
        return 0.0, 0.0, 200.0, 200.0
    xs = [float(q.get("x", 0.0)) for q in quests]
    ys = [float(q.get("y", 0.0)) for q in quests]
    return min(xs), min(ys), max(xs), max(ys)


def _node_center(q: dict, min_x: float, min_y: float) -> tuple[float, float]:
    qx = float(q.get("x", 0.0))
    qy = float(q.get("y", 0.0))
    cx = PAD_X + (qx - min_x) * NODE_GAP_X
    cy = PAD_Y + (qy - min_y) * NODE_GAP_Y
    return cx, cy


# ─────────────────────────────────────────────
# Data model for the JS-side
# ─────────────────────────────────────────────

def _chapter_payload(chapter: dict) -> dict:
    quests = chapter.get("quests", [])
    min_x, min_y, max_x, max_y = _bounds(quests)
    width = PAD_X * 2 + (max_x - min_x) * NODE_GAP_X
    height = PAD_Y * 2 + (max_y - min_y) * NODE_GAP_Y

    # Resolve a single mod icon per chapter
    modid_guess = (chapter.get("_modid") or "").strip()
    if not modid_guess:
        first_item = ""
        for q in quests:
            for t in q.get("tasks", []):
                it = t.get("item") or ""
                if ":" in it:
                    first_item = it
                    break
            if first_item:
                break
        if first_item and ":" in first_item:
            modid_guess = first_item.split(":", 1)[0]
    chapter_title = chapter.get("title", "") or chapter.get("_mod_source_name", "")
    mod_icon = _modrinth_icon_for(modid_guess, chapter_title) if modid_guess else None
    if not mod_icon:
        ic = chapter.get("icon", {}).get("item", "")
        if ic.startswith("minecraft:"):
            mod_icon = _icon_url_for_item(ic)

    nodes = []
    for q in quests:
        cx, cy = _node_center(q, min_x, min_y)
        items: list[dict] = []
        for t in q.get("tasks", []):
            it = t.get("item") or ""
            url = _icon_url_for_item(it, mod_icon)
            items.append({
                "id": it,
                "count": t.get("count", 1),
                "icon": url,
            })
        nodes.append({
            "id": q["id"],
            "title": q.get("title", ""),
            "text": q.get("text") or [],
            "x": cx,
            "y": cy,
            "items": items,
            "deps": q.get("dependencies") or [],
            "icon": (items[0]["icon"] if items else None) or mod_icon,
        })

    edges = []
    for q in quests:
        cx, cy = _node_center(q, min_x, min_y)
        for dep in q.get("dependencies") or []:
            parent = next((p for p in quests if p.get("id") == dep), None)
            if not parent:
                continue
            px, py = _node_center(parent, min_x, min_y)
            edges.append({"from": dep, "to": q["id"], "x1": px, "y1": py, "x2": cx, "y2": cy})

    return {
        "id": chapter["id"],
        "title": chapter.get("title", ""),
        "icon": mod_icon,
        "modid": modid_guess,
        "width": max(width, 600),
        "height": max(height, 400),
        "nodes": nodes,
        "edges": edges,
        "quest_count": len(quests),
    }


# ─────────────────────────────────────────────
# HTML emission
# ─────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #1c1f25;
    --bg-alt: #23262d;
    --bg-darker: #14161a;
    --line: #3a3f47;
    --accent: #6cb8ff;
    --accent-2: #2c6fa6;
    --text: #e6e6e6;
    --text-dim: #999;
    --node: #2e333c;
    --node-hover: #3a414c;
    --edge: #4d5664;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100vh; overflow: hidden;
    background: var(--bg); color: var(--text);
    font: 14px/1.4 "Segoe UI", system-ui, -apple-system, sans-serif;
  }
  #app { display: flex; height: 100vh; }
  #sidebar {
    width: 260px; background: var(--bg-darker);
    border-right: 1px solid var(--line);
    display: flex; flex-direction: column;
    flex-shrink: 0;
  }
  #sidebar-header {
    padding: 14px 16px; border-bottom: 1px solid var(--line);
    font-weight: 600; color: var(--accent);
  }
  #sidebar-header .sub {
    font-weight: 400; color: var(--text-dim); font-size: 12px;
    margin-top: 4px;
  }
  #chapters-list { flex: 1; overflow-y: auto; padding: 8px 0; }
  .chapter-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 16px; cursor: pointer;
    border-left: 3px solid transparent;
    transition: background .12s, border-color .12s;
  }
  .chapter-item:hover { background: var(--bg-alt); }
  .chapter-item.active {
    background: var(--bg-alt); border-left-color: var(--accent);
  }
  .chapter-item img {
    width: 28px; height: 28px;
    image-rendering: pixelated;
    background: var(--node); border-radius: 4px;
    flex-shrink: 0;
  }
  .chapter-item .ch-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chapter-item .ch-count { color: var(--text-dim); font-size: 11px; }
  #footer {
    padding: 10px 16px; border-top: 1px solid var(--line);
    font-size: 11px; color: var(--text-dim);
  }
  #footer a { color: var(--accent); text-decoration: none; }
  #main { flex: 1; position: relative; overflow: hidden; }
  #canvas-wrap {
    position: absolute; inset: 0;
    overflow: auto; cursor: grab;
    background:
      linear-gradient(to right, rgba(255,255,255,.02) 1px, transparent 1px) 0 0 / 30px 30px,
      linear-gradient(to bottom, rgba(255,255,255,.02) 1px, transparent 1px) 0 0 / 30px 30px,
      var(--bg);
  }
  #canvas-wrap.grabbing { cursor: grabbing; }
  #canvas { position: relative; }
  #edges {
    position: absolute; left: 0; top: 0;
    pointer-events: none;
  }
  .node {
    position: absolute; width: __NODE_SIZE__px; height: __NODE_SIZE__px;
    transform: translate(-50%, -50%);
    background: var(--node);
    border: 2px solid var(--line);
    cursor: pointer;
    clip-path: polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%);
    transition: background .12s, transform .12s;
    display: flex; align-items: center; justify-content: center;
  }
  .node:hover, .node.selected {
    background: var(--node-hover);
    transform: translate(-50%, -50%) scale(1.08);
  }
  .node.selected { box-shadow: 0 0 0 3px var(--accent); }
  .node img {
    width: 32px; height: 32px;
    image-rendering: pixelated;
    pointer-events: none;
  }
  .node-title {
    position: absolute; left: 50%; top: calc(100% + 4px);
    transform: translateX(-50%);
    white-space: nowrap;
    font-size: 11px; color: var(--text-dim);
    pointer-events: none;
    text-shadow: 0 1px 2px rgba(0,0,0,.8);
  }
  #panel {
    position: absolute;
    top: 16px; right: 16px;
    width: 380px; max-height: calc(100vh - 32px);
    background: var(--bg-darker);
    border: 1px solid var(--line); border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0,0,0,.5);
    display: none; flex-direction: column;
    overflow: hidden;
  }
  #panel.open { display: flex; }
  #panel-header {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 14px; border-bottom: 1px solid var(--line);
    background: var(--bg-alt);
  }
  #panel-header .ico {
    width: 28px; height: 28px; image-rendering: pixelated;
    background: var(--node); border-radius: 3px;
  }
  #panel-header h3 { margin: 0; font-size: 15px; color: var(--accent); flex: 1; }
  #panel-close {
    background: transparent; border: 0; color: var(--text-dim);
    font-size: 18px; cursor: pointer; padding: 0 4px;
  }
  #panel-body { padding: 12px 14px; overflow-y: auto; }
  #panel-body .label {
    text-transform: uppercase; font-size: 11px; letter-spacing: .8px;
    color: var(--accent); margin: 12px 0 6px;
  }
  #panel-body .label:first-child { margin-top: 0; }
  #panel-body .desc { color: var(--text); white-space: pre-wrap; }
  #panel-body .tasks {
    display: flex; flex-wrap: wrap; gap: 6px;
  }
  .task-item {
    display: flex; align-items: center; gap: 8px;
    background: var(--bg-alt); padding: 6px 10px; border-radius: 4px;
    font-size: 12px; color: var(--text-dim);
  }
  .task-item img {
    width: 22px; height: 22px; image-rendering: pixelated;
  }
  .task-item code {
    color: var(--text); font-family: ui-monospace, Menlo, Consolas, monospace;
  }
  #empty {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; color: var(--text-dim); font-size: 18px;
  }
  #toolbar {
    position: absolute; left: 16px; top: 16px;
    display: flex; gap: 6px; z-index: 5;
  }
  #toolbar button {
    background: var(--bg-darker); color: var(--text);
    border: 1px solid var(--line); border-radius: 4px;
    padding: 6px 10px; cursor: pointer; font-size: 12px;
  }
  #toolbar button:hover { background: var(--bg-alt); }
  #zoom-info {
    position: absolute; left: 16px; bottom: 16px;
    background: var(--bg-darker); padding: 4px 10px;
    border: 1px solid var(--line); border-radius: 4px;
    font-size: 11px; color: var(--text-dim);
  }
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div id="sidebar-header">
      MC Quest Generator
      <div class="sub">__SUBTITLE__</div>
    </div>
    <div id="chapters-list"></div>
    <div id="footer">
      <a href="https://github.com/sirksswenenen/mc-quest-gen" target="_blank">github.com/sirksswenenen/mc-quest-gen</a>
    </div>
  </aside>
  <main id="main">
    <div id="toolbar">
      <button id="zoom-in">+</button>
      <button id="zoom-out">-</button>
      <button id="zoom-reset">100%</button>
    </div>
    <div id="canvas-wrap">
      <div id="canvas">
        <svg id="edges" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div id="empty">Select a mod from the left.</div>
    </div>
    <div id="zoom-info"></div>
    <aside id="panel">
      <header id="panel-header">
        <img class="ico" id="panel-icon" alt="">
        <h3 id="panel-title">Quest</h3>
        <button id="panel-close" title="Close">×</button>
      </header>
      <div id="panel-body"></div>
    </aside>
  </main>
</div>
<script>
const DATA = __DATA_JSON__;
const FALLBACK_ICON = "data:image/svg+xml;utf8," + encodeURIComponent(
  `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='4' fill='#3a414c'/><text x='12' y='17' font-family='Arial' font-size='14' fill='#e6e6e6' text-anchor='middle' font-weight='bold'>?</text></svg>`
);

const $ = (s) => document.querySelector(s);
const list = $("#chapters-list");
const canvas = $("#canvas");
const edgesSvg = $("#edges");
const empty = $("#empty");
const panel = $("#panel");
const panelTitle = $("#panel-title");
const panelIcon = $("#panel-icon");
const panelBody = $("#panel-body");
const panelClose = $("#panel-close");
const wrap = $("#canvas-wrap");
const zoomInfo = $("#zoom-info");
let currentChapter = null;
let zoom = 1.0;
let selectedNode = null;

function imgFallback(img) {
  img.onerror = null;
  img.src = FALLBACK_ICON;
}

function renderChaptersList() {
  list.innerHTML = "";
  DATA.chapters.forEach((ch, i) => {
    const div = document.createElement("div");
    div.className = "chapter-item";
    div.dataset.chapterId = ch.id;
    const icon = ch.icon || FALLBACK_ICON;
    div.innerHTML = `
      <img src="${icon}" alt="" onerror="this.onerror=null; this.src='${FALLBACK_ICON}'">
      <span class="ch-name">${escapeHtml(ch.title)}</span>
      <span class="ch-count">${ch.quest_count}</span>
    `;
    div.addEventListener("click", () => selectChapter(ch.id));
    list.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function selectChapter(chapterId) {
  const ch = DATA.chapters.find(c => c.id === chapterId);
  if (!ch) return;
  currentChapter = ch;
  document.querySelectorAll(".chapter-item").forEach(el => {
    el.classList.toggle("active", el.dataset.chapterId === chapterId);
  });
  panel.classList.remove("open");
  empty.style.display = "none";
  renderCanvas(ch);
}

function renderCanvas(ch) {
  canvas.style.width = (ch.width * zoom) + "px";
  canvas.style.height = (ch.height * zoom) + "px";
  canvas.style.transform = `scale(${zoom})`;
  canvas.style.transformOrigin = "top left";
  canvas.innerHTML = "";

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.id = "edges";
  svg.setAttribute("width", ch.width);
  svg.setAttribute("height", ch.height);
  ch.edges.forEach(e => {
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", e.x1);
    line.setAttribute("y1", e.y1);
    line.setAttribute("x2", e.x2);
    line.setAttribute("y2", e.y2);
    line.setAttribute("stroke", "var(--edge)");
    line.setAttribute("stroke-width", "2");
    svg.appendChild(line);
  });
  canvas.appendChild(svg);

  ch.nodes.forEach(n => {
    const node = document.createElement("div");
    node.className = "node";
    node.style.left = n.x + "px";
    node.style.top = n.y + "px";
    node.dataset.nodeId = n.id;
    const ico = n.icon || FALLBACK_ICON;
    const safeTitle = escapeHtml(n.title || "Quest");
    node.innerHTML = `
      <img src="${ico}" alt="${safeTitle}" onerror="this.onerror=null; this.src='${FALLBACK_ICON}'">
      <span class="node-title">${safeTitle}</span>
    `;
    node.addEventListener("click", () => openPanel(n));
    canvas.appendChild(node);
  });

  zoomInfo.textContent = `${ch.title} · ${ch.quest_count} quests · zoom ${Math.round(zoom*100)}%`;
}

function openPanel(node) {
  selectedNode = node.id;
  document.querySelectorAll(".node").forEach(el => {
    el.classList.toggle("selected", el.dataset.nodeId === node.id);
  });
  panelTitle.textContent = node.title || "Quest";
  panelIcon.src = node.icon || FALLBACK_ICON;
  panelIcon.onerror = function() { imgFallback(this); };
  const desc = (node.text || []).join(" ").trim() || "(no description)";
  const tasksHtml = (node.items || []).map(t => `
    <div class="task-item">
      <img src="${t.icon || FALLBACK_ICON}" alt="" onerror="this.onerror=null; this.src='${FALLBACK_ICON}'">
      <span><code>${escapeHtml(t.id)}</code>${t.count > 1 ? ` × ${t.count}` : ""}</span>
    </div>
  `).join("");
  const depsHtml = (node.deps || []).length
    ? `<div class="label">Requires</div><div style="color:var(--text-dim);font-size:12px">${node.deps.length} prerequisite quest${node.deps.length>1?"s":""}</div>`
    : "";
  panelBody.innerHTML = `
    <div class="label">Description</div>
    <div class="desc">${escapeHtml(desc)}</div>
    <div class="label">Tasks</div>
    <div class="tasks">${tasksHtml || '<span style="color:var(--text-dim);font-size:12px">No tasks.</span>'}</div>
    ${depsHtml}
  `;
  panel.classList.add("open");
}

panelClose.addEventListener("click", () => {
  panel.classList.remove("open");
  document.querySelectorAll(".node.selected").forEach(el => el.classList.remove("selected"));
});

$("#zoom-in").addEventListener("click", () => { zoom = Math.min(2.0, zoom + 0.1); if (currentChapter) renderCanvas(currentChapter); });
$("#zoom-out").addEventListener("click", () => { zoom = Math.max(0.4, zoom - 0.1); if (currentChapter) renderCanvas(currentChapter); });
$("#zoom-reset").addEventListener("click", () => { zoom = 1.0; if (currentChapter) renderCanvas(currentChapter); });

// Drag-to-pan
let isDragging = false, dragStart = null, scrollStart = null;
wrap.addEventListener("mousedown", (e) => {
  if (e.target.closest(".node") || e.target.closest("#panel") || e.target.closest("#toolbar")) return;
  isDragging = true;
  dragStart = { x: e.clientX, y: e.clientY };
  scrollStart = { x: wrap.scrollLeft, y: wrap.scrollTop };
  wrap.classList.add("grabbing");
});
window.addEventListener("mousemove", (e) => {
  if (!isDragging) return;
  wrap.scrollLeft = scrollStart.x - (e.clientX - dragStart.x);
  wrap.scrollTop = scrollStart.y - (e.clientY - dragStart.y);
});
window.addEventListener("mouseup", () => { isDragging = false; wrap.classList.remove("grabbing"); });

// Wheel zoom (with ctrl)
wrap.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  zoom = Math.max(0.4, Math.min(2.0, zoom + (e.deltaY < 0 ? 0.1 : -0.1)));
  if (currentChapter) renderCanvas(currentChapter);
}, { passive: false });

renderChaptersList();
if (DATA.chapters.length > 0) selectChapter(DATA.chapters[0].id);
</script>
</body>
</html>
"""


def render_html(quests_data: dict, output_path: Path, title: str = "MC Quest Preview") -> Path:
    chapters = quests_data.get("chapters", [])
    payload_chapters = [_chapter_payload(c) for c in chapters]
    payload = {"chapters": payload_chapters}

    total_quests = sum(c["quest_count"] for c in payload_chapters)
    subtitle = f"{len(payload_chapters)} mod chapter(s) · {total_quests} quest(s)"

    html_str = (
        _HTML_TEMPLATE
        .replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__NODE_SIZE__", str(NODE_SIZE))
        .replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_str, encoding="utf-8")
    return output_path


def render_from_file(quests_json_path: Path, output_path: Optional[Path] = None) -> Path:
    data = json.loads(quests_json_path.read_text(encoding="utf-8"))
    if output_path is None:
        output_path = quests_json_path.parent / "preview.html"
    return render_html(data, output_path, title=f"MC Quest Preview — {quests_json_path.name}")
