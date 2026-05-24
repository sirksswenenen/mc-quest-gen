"""
FTB Quests 1.12.2 file generator.

Output structure (drop into your Minecraft instance):

    config/ftbquests/
      quests.json          ← chapter list + quest objects (1.12.2 JSON format)
      rewards/             ← reward tables (empty by default)

1.12.2 JSON schema (reverse-engineered from real modpacks like
Project Ozone 3 and Stoneblock 2):

    {
      "version": 1,
      "default_reward_team": false,
      "default_consume_items": false,
      "emergency_items": [],
      "emergency_items_cooldown": 300,
      "chapters": [
        {
          "id": "HEXID",
          "title": "Chapter Title",
          "icon": { "item": "minecraft:book" },
          "quests": [ ... ],
          "quest_links": []
        }
      ],
      "reward_tables": []
    }

Quest object:

    {
      "id": "HEXID",
      "x": 0.0, "y": 0.0,
      "shape": "square",
      "title": "...",
      "text": ["line 1", "line 2"],
      "tasks": [ ... ],
      "rewards": [],
      "dependencies": ["HEXID_of_prerequisite"],
      "hide": false,
      "hide_dependency_lines": false,
      "min_required_dependencies": 0
    }

Task types we generate:

    item  → { "id": "HEXID", "type": "item",
              "item": "modid:item_name", "count": 1, "consume_items": false }
"""

from __future__ import annotations

import json
import random
import re
import textwrap
import time
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# ID generation
# ─────────────────────────────────────────────

_id_seen: set[str] = set()


def _new_id() -> str:
    """Generate a unique 8-char hex ID (FTB Quests style)."""
    for _ in range(64):
        candidate = format(random.randint(0x10000000, 0xFFFFFFFF), "08X")
        if candidate not in _id_seen:
            _id_seen.add(candidate)
            return candidate
    # Extremely unlikely, but bump entropy
    time.sleep(0.001)
    return format(random.randint(0, 0xFFFFFFFFFFFF), "012X")


# ─────────────────────────────────────────────
# Item ID sanity
# ─────────────────────────────────────────────

_ITEM_ID_RE = re.compile(r"^[a-z0-9_]+:[a-z0-9_./-]+$")


def _sanitize_item_id(raw: str, modid: str = "") -> str:
    """Coerce LLM-emitted item IDs to a valid modid:item form."""
    s = raw.strip().strip("`'\" .,;:").lower()
    s = re.sub(r"\s+", "_", s)
    if ":" not in s and modid:
        s = f"{modid}:{s}"
    s = re.sub(r"[^a-z0-9_:./-]", "", s)
    if _ITEM_ID_RE.match(s):
        return s
    if modid and ":" not in s:
        return f"{modid}:{re.sub(r'[^a-z0-9_]', '_', s) or 'item'}"
    return s or "minecraft:book"


def _icon_for_quest(items: list[str]) -> Optional[str]:
    for it in items:
        if _ITEM_ID_RE.match(it):
            return it
    return None


# ─────────────────────────────────────────────
# Layout (left-to-right dependency tree)
# ─────────────────────────────────────────────

def _layout_quests(quests: list[dict], start_x: float = 0.0, start_y: float = 0.0) -> None:
    """Assign x/y so dependencies flow left → right."""
    id_to_quest = {q["id"]: q for q in quests}
    children: dict[str, list[str]] = {q["id"]: [] for q in quests}
    roots: list[str] = []

    for q in quests:
        deps = q.get("dependencies", [])
        if not deps:
            roots.append(q["id"])
        for dep in deps:
            if dep in children:
                children[dep].append(q["id"])

    x = start_x
    current = roots
    visited: set[str] = set()

    while current:
        y = start_y
        next_level: list[str] = []
        for qid in current:
            if qid in visited:
                continue
            visited.add(qid)
            q = id_to_quest.get(qid)
            if q is None:
                continue
            q["x"] = x
            q["y"] = y
            y += 2.0
            next_level.extend(c for c in children.get(qid, []) if c not in visited)
        x += 3.0
        current = next_level

    # Orphans / cycles get parked at the end
    for q in quests:
        if "x" not in q:
            q["x"] = x
            q["y"] = start_y


# ─────────────────────────────────────────────
# Builders
# ─────────────────────────────────────────────

def make_item_task(item_id: str, count: int = 1, consume: bool = False) -> dict:
    return {
        "id": _new_id(),
        "type": "item",
        "item": item_id,
        "count": count,
        "consume_items": consume,
    }


def make_quest(
    title: str,
    description: list[str],
    tasks: list[dict],
    dependencies: Optional[list[str]] = None,
    icon_item: Optional[str] = None,
    shape: str = "square",
) -> dict:
    q: dict = {
        "id": _new_id(),
        "x": 0.0,
        "y": 0.0,
        "shape": shape,
        "title": title,
        "text": description,
        "tasks": tasks,
        "rewards": [],
        "dependencies": dependencies or [],
        "hide": False,
        "hide_dependency_lines": False,
        "min_required_dependencies": (
            1 if (dependencies and len(dependencies) > 1) else 0
        ),
    }
    if icon_item:
        q["icon"] = {"item": icon_item}
    return q


def make_chapter(title: str, quests: list[dict], icon_item: str = "minecraft:book") -> dict:
    _layout_quests(quests)
    return {
        "id": _new_id(),
        "title": title,
        "icon": {"item": icon_item},
        "quests": quests,
        "quest_links": [],
    }


def make_root_json(chapters: list[dict]) -> dict:
    return {
        "version": 1,
        "default_reward_team": False,
        "default_consume_items": False,
        "emergency_items": [],
        "emergency_items_cooldown": 300,
        "chapters": chapters,
        "reward_tables": [],
    }


# ─────────────────────────────────────────────
# AI-response parser
# ─────────────────────────────────────────────

_STAGE_HEADER_SPLIT = re.compile(r"(?m)^##\s*", re.UNICODE)
_STAGE_PREFIX_RE = re.compile(
    r"^(?:[Ss]tage|Этап|Шаг|Tier|Step)\s*\d+\s*[:.]?\s*",
    re.UNICODE,
)
_ITEM_LINE_RE = re.compile(r"(?i)^\s*item[s]?\s*[:：]")
_DESC_LINE_RE = re.compile(r"(?i)^\s*(description|desc|описание)\s*[:：]")
_TAG_LINE_RE = re.compile(
    r"(?i)^\s*(item[s]?|description|desc|описание|depends?\s*on|requires?|зависит\s*от|зависимост[иь])\s*[:：]"
)
_DEPS_LINE_RE = re.compile(
    r"(?i)^\s*(?:depends?\s*on|requires?|зависит\s*от|зависимост[иь])\s*[:：]\s*(.+)"
)


def parse_ai_quests(ai_text: str, mod_name: str, modid: str) -> list[dict]:
    """
    Parse the AI's quest chain into FTB Quests quest objects.

    Expected format (tolerated variations):

        ## Stage 1: Title
        Item: modid:item_name [, modid:item2]
        Description: One or two sentences.
        Depends on: (none) | Stage N | Title-of-prev

        ## Stage 2: ...
    """
    quests: list[dict] = []
    prev_id: Optional[str] = None
    stage_map: dict[str, str] = {}
    stage_num = 0

    blocks = _STAGE_HEADER_SPLIT.split(ai_text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if not lines:
            continue

        title_line = lines[0].strip().lstrip("0123456789.:) ")
        title_line = _STAGE_PREFIX_RE.sub("", title_line).strip()
        if not title_line:
            continue

        # Items
        items_raw: list[str] = []
        for line in lines:
            if _ITEM_LINE_RE.match(line):
                rest = re.sub(_ITEM_LINE_RE, "", line)
                for part in re.split(r"[,;]\s*", rest):
                    part = part.strip()
                    if part:
                        items_raw.append(part)
                break

        items = [_sanitize_item_id(it, modid) for it in items_raw if it]
        if not items and modid:
            slug = re.sub(r"[^a-z0-9_]", "_", title_line.lower())
            slug = re.sub(r"_+", "_", slug).strip("_") or "item"
            items = [f"{modid}:{slug}"]

        # Description
        desc_lines: list[str] = []
        in_desc = False
        for line in lines[1:]:
            if _DESC_LINE_RE.match(line):
                rest = re.sub(_DESC_LINE_RE, "", line).strip()
                if rest:
                    desc_lines.append(rest)
                in_desc = True
            elif _TAG_LINE_RE.match(line):
                in_desc = False
            elif in_desc and line.strip():
                desc_lines.append(line.strip())

        if not desc_lines:
            for line in lines[1:]:
                if line.strip() and not _TAG_LINE_RE.match(line):
                    desc_lines.append(line.strip())

        wrapped: list[str] = []
        for d in desc_lines[:6]:
            chunks = textwrap.wrap(d, 60) or [d]
            wrapped.extend(chunks)
        wrapped = wrapped[:8]
        if not wrapped:
            wrapped = [f"Craft {title_line}"]

        # Dependencies
        deps: list[str] = []
        dep_handled = False
        for line in lines:
            m = _DEPS_LINE_RE.match(line)
            if not m:
                continue
            dep_text = m.group(1).strip().lower()
            dep_handled = True
            if any(w in dep_text for w in ("none", "nothing", "нет", "—")) or dep_text in ("-", "()", "(none)"):
                break
            for label, qid in stage_map.items():
                if label and (label.lower() in dep_text or dep_text in label.lower()):
                    deps.append(qid)
            break

        if not dep_handled and prev_id is not None:
            deps = [prev_id]

        tasks = [make_item_task(it, count=1, consume=False) for it in items[:4]]
        if not tasks:
            tasks = [make_item_task("minecraft:book")]

        icon = _icon_for_quest(items) or "minecraft:book"

        q = make_quest(
            title=title_line[:80],
            description=wrapped,
            tasks=tasks,
            dependencies=deps,
            icon_item=icon,
        )
        stage_num += 1
        stage_map[f"stage {stage_num}"] = q["id"]
        stage_map[f"этап {stage_num}"] = q["id"]
        stage_map[title_line.lower()] = q["id"]
        prev_id = q["id"]
        quests.append(q)

    return quests


# ─────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────

def write_ftbquests_output(root_data: dict, output_dir: Path) -> Path:
    quests_dir = output_dir / "config" / "ftbquests"
    quests_dir.mkdir(parents=True, exist_ok=True)
    (quests_dir / "rewards").mkdir(exist_ok=True)
    out_file = quests_dir / "quests.json"
    out_file.write_text(json.dumps(root_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_file
