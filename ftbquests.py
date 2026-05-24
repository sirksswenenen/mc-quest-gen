"""
FTB Quests 1.12.2 JSON generator.

Output structure (config/ftbquests/):
  quests.json          ← chapter list + quest objects
  rewards/             ← reward tables (empty by default)

FTB Quests 1.12.2 JSON format reference
(reverse-engineered from real modpack files):

quests.json root:
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
    "x": 0.0,
    "y": 0.0,
    "shape": "square",
    "title": "Quest Title",
    "text": ["Description line 1", "Line 2"],
    "tasks": [ ... ],
    "rewards": [],
    "dependencies": ["HEXID_of_prerequisite"],
    "hide": false,
    "hide_dependency_lines": false,
    "min_required_dependencies": 1
  }

Task types:
  item  → { "id": "HEXID", "type": "item", "item": "modid:item_name", "count": 1, "consume_items": false }
  kill  → { "id": "HEXID", "type": "kill", "entity": "modid:entity", "value": 1 }
  stat  → { "id": "HEXID", "type": "stat", "stat": "stat.name", "value": 1 }
"""

import json
import random
import time
import re
import textwrap
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# ID generation
# ─────────────────────────────────────────────

def _new_id() -> str:
    """Generate a unique-ish hex ID (8 chars, like FTB Quests uses)."""
    time.sleep(0.001)  # tiny sleep to avoid collisions in tight loops
    return format(random.randint(0x10000000, 0xFFFFFFFF), "08X")


# ─────────────────────────────────────────────
# Quest layout helpers
# ─────────────────────────────────────────────

def _layout_quests(quests: list[dict], start_x: float = 0.0, start_y: float = 0.0) -> None:
    """
    Assign x/y positions to quests forming a left-to-right dependency tree.
    Mutates the quest dicts in place.
    """
    # Build dependency graph
    id_to_quest = {q["id"]: q for q in quests}
    children: dict[str, list[str]] = {q["id"]: [] for q in quests}
    roots: list[str] = []

    for q in quests:
        deps = q.get("dependencies", [])
        if not deps:
            roots.append(q["id"])
        else:
            for dep in deps:
                if dep in children:
                    children[dep].append(q["id"])

    # BFS layout: x = depth, y = position within depth level
    x = start_x
    current_level = roots
    visited: set = set()

    while current_level:
        y = start_y
        next_level: list = []
        for qid in current_level:
            if qid in visited:
                continue
            visited.add(qid)
            q = id_to_quest.get(qid)
            if q is not None:
                q["x"] = x
                q["y"] = y
                y += 2.0
                next_level.extend(children.get(qid, []))
        x += 3.0
        current_level = next_level

    # Any quests not reached (cycles/orphans) — place at end
    for q in quests:
        if "x" not in q:
            q["x"] = x
            q["y"] = start_y


# ─────────────────────────────────────────────
# Data classes (plain dicts)
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
        "min_required_dependencies": 1 if (dependencies and len(dependencies) > 1) else 0,
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

def parse_ai_quests(ai_text: str, mod_name: str, modid: str) -> list[dict]:
    """
    Parse AI-generated quest chain from text into FTB Quests quest objects.

    Expected AI format (flexible, we handle variations):
    ---
    ## Stage 1: Title
    Item: modid:item_name
    Description: Some text here.
    Depends on: (none) OR: Stage 0

    ## Stage 2: Title
    Item: modid:item2, modid:item3
    Description: ...
    Depends on: Stage 1
    ---
    """
    quests: list[dict] = []
    prev_id: Optional[str] = None
    stage_map: dict[str, str] = {}  # stage_label → quest_id

    # Split on stage headers
    blocks = re.split(r"(?m)^##\s*", ai_text)
    stage_num = 0

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract title (first line)
        lines = block.splitlines()
        title_line = lines[0].strip().lstrip("0123456789.:) ")
        # Remove leading "Stage N:" if present
        title_line = re.sub(r"^[Ss]tage\s*\d+\s*[:.]?\s*", "", title_line).strip()
        if not title_line:
            continue

        # Extract items
        items: list[str] = []
        for line in lines:
            if re.match(r"(?i)item[s]?\s*[:：]", line):
                raw = re.sub(r"(?i)item[s]?\s*[:：]\s*", "", line)
                for part in re.split(r"[,;]\s*", raw):
                    part = part.strip()
                    if part and ":" in part:
                        items.append(part)

        # If no items found, try to infer from title
        if not items and modid:
            slug = re.sub(r"[^a-z0-9_]", "_", title_line.lower())
            items = [f"{modid}:{slug}"]

        # Extract description
        desc_lines: list[str] = []
        in_desc = False
        for line in lines[1:]:
            if re.match(r"(?i)(description|desc)\s*[:：]", line):
                desc_text = re.sub(r"(?i)(description|desc)\s*[:：]\s*", "", line).strip()
                if desc_text:
                    desc_lines.append(desc_text)
                in_desc = True
            elif re.match(r"(?i)(item[s]?|depends? on|requires?)\s*[:：]", line):
                in_desc = False
            elif in_desc and line.strip():
                desc_lines.append(line.strip())

        if not desc_lines:
            # Use any non-tag lines as description
            for line in lines[1:]:
                if not re.match(r"(?i)(item[s]?|depends? on|requires?)\s*[:：]", line) and line.strip():
                    desc_lines.append(line.strip())

        # Wrap description to 60 chars per line (Minecraft book width)
        wrapped: list[str] = []
        for d in desc_lines[:6]:  # max 6 desc lines
            wrapped.extend(textwrap.wrap(d, 60) or [d])
        if not wrapped:
            wrapped = [f"Craft {title_line}"]

        # Build tasks
        tasks = [make_item_task(item, count=1, consume=False) for item in items[:4]]
        if not tasks:
            tasks = [make_item_task(f"minecraft:book")]  # placeholder

        # Dependency: by default chain previous quest
        deps: list[str] = []
        for line in lines:
            m = re.match(r"(?i)depends? on\s*[:：]\s*(.+)", line)
            if m:
                dep_text = m.group(1).strip().lower()
                if "none" in dep_text or "nothing" in dep_text or dep_text == "-":
                    prev_id = None
                    break
                # Try to match a stage label
                for label, qid in stage_map.items():
                    if label.lower() in dep_text or dep_text in label.lower():
                        deps.append(qid)
                break

        if not deps and prev_id is not None:
            deps = [prev_id]

        q = make_quest(
            title=title_line[:60],
            description=wrapped,
            tasks=tasks,
            dependencies=deps,
            icon_item=items[0] if items else None,
        )
        stage_num += 1
        label = f"stage {stage_num}"
        stage_map[label] = q["id"]
        stage_map[title_line.lower()] = q["id"]
        prev_id = q["id"]
        quests.append(q)

    return quests


# ─────────────────────────────────────────────
# File output
# ─────────────────────────────────────────────

def write_ftbquests_output(root_data: dict, output_dir: Path) -> Path:
    """Write the complete FTB Quests config directory."""
    quests_dir = output_dir / "config" / "ftbquests"
    quests_dir.mkdir(parents=True, exist_ok=True)

    out_file = quests_dir / "quests.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(root_data, f, indent=2, ensure_ascii=False)

    return out_file
