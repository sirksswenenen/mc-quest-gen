"""
jar_inventory.py
================

Extracts the *real* item inventory of a Minecraft mod from its .jar file:

  • recipe outputs from assets/<modid>/recipes/*.json
    and data/<modid>/recipes/*.json  → DEFINITIVE list of craftable items
  • lang entries from assets/<modid>/lang/en_us.lang / en_US.lang
    and en_us.json                   → display names for items / blocks
  • blockstate file names             → registered blocks
  • Patchouli book entries from
    assets/<modid>/patchouli_books/<book>/en_us/{categories,entries}/*.json
    → mod author's official progression backbone

This is consumed by mc_quest_gen.build_prompt() so the AI gets a list of
ITEMS THAT ACTUALLY EXIST and a structural backbone for the quest chain,
instead of guessing (and hallucinating things like `ic2:copper_furnace`).

Stdlib only — zipfile + json + re.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# ─────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────


@dataclass
class PatchouliEntry:
    category: str
    name: str            # display name of the entry
    icon_item: str       # e.g. "ic2:macerator"
    items: list[str]     # item ids referenced inside pages
    order: int = 0


@dataclass
class PatchouliBook:
    book_id: str         # e.g. "botania:lexicon"
    title: str
    categories: list[str] = field(default_factory=list)   # ordered
    entries: list[PatchouliEntry] = field(default_factory=list)


@dataclass
class JarInventory:
    modid: str = ""
    recipe_outputs: dict[str, int] = field(default_factory=dict)     # item_id -> craft count
    item_display_names: dict[str, str] = field(default_factory=dict) # item_id -> "Macerator"
    block_ids: set[str] = field(default_factory=set)                 # ids inferred from blockstates
    raw_lang: dict[str, str] = field(default_factory=dict)           # lang_key -> value (raw)
    patchouli_books: list[PatchouliBook] = field(default_factory=list)

    def has_signal(self) -> bool:
        return bool(self.recipe_outputs or self.item_display_names or
                    self.block_ids or self.patchouli_books)

    def all_item_ids(self) -> set[str]:
        out: set[str] = set(self.recipe_outputs)
        out |= set(self.item_display_names)
        out |= self.block_ids
        return out


# ─────────────────────────────────────────────
# Recipe extraction
# ─────────────────────────────────────────────


_RECIPE_PATHS = ("assets/", "data/")     # prefixes inside the jar


def _extract_result_item(recipe: dict) -> tuple[str, int]:
    """Return (item_id, count) from a Forge/vanilla recipe JSON.

    Returns ("", 0) if no recognizable result item.
    Handles all common shapes:
      result: "modid:item"
      result: {"item": "modid:item", "count": 3}
      result: {"id":   "modid:item"}
      output: "modid:item"          (some mods)
      output: {"item": "modid:item"}
    """
    for key in ("result", "output"):
        r = recipe.get(key)
        if isinstance(r, str) and ":" in r:
            return r, 1
        if isinstance(r, dict):
            item = r.get("item") or r.get("id") or r.get("name")
            if isinstance(item, str) and ":" in item:
                count = r.get("count") or r.get("amount") or 1
                try:
                    return item, int(count)
                except (ValueError, TypeError):
                    return item, 1
    return "", 0


def _parse_recipes(zf: zipfile.ZipFile, modid_filter: str | None,
                   inv: JarInventory) -> None:
    """Scan recipes/*.json files and record output items."""
    for name in zf.namelist():
        if not name.endswith(".json"):
            continue
        # accept   assets/<modid>/recipes/...    and    data/<modid>/recipes/...
        parts = name.split("/")
        if len(parts) < 4:
            continue
        if parts[0] not in ("assets", "data"):
            continue
        if parts[2] != "recipes":
            continue
        modid_in_path = parts[1]
        if modid_filter and modid_in_path != modid_filter:
            # still allow it but only when it really belongs to this mod
            continue
        try:
            data = json.loads(zf.read(name).decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        item_id, count = _extract_result_item(data)
        if item_id:
            inv.recipe_outputs[item_id] = max(inv.recipe_outputs.get(item_id, 0), count)


# ─────────────────────────────────────────────
# Lang extraction
# ─────────────────────────────────────────────


_LANG_FILE_NAMES = ("en_us.lang", "en_US.lang", "en_us.json")


def _parse_lang_dot_lang(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _parse_lang_json(text: str) -> dict[str, str]:
    text = text.lstrip("\ufeff")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()
            if isinstance(v, (str, int, float))}


# common lang key → registry key heuristics for 1.12.2 mods
#   tile.foo.name             →  modid:foo
#   item.foo.name             →  modid:foo
#   tile.modid.foo.name       →  modid:foo
#   item.modid.foo.name       →  modid:foo
#   modid.foo.name            →  modid:foo
#   tile.foo                  →  modid:foo  (no .name suffix)
_LANG_KEY_RE = re.compile(
    r"^(?:tile|item|block|fluid|enchantment|entity)\."
    r"(?:[a-z0-9_]+\.)?"         # optional modid prefix
    r"([A-Za-z0-9_]+?)"          # the actual name (lazy)
    r"(?:\.name|\.tooltip|\.desc|\.description)?$"
)
_LANG_NAME_SUFFIX_RE = re.compile(r"\.(name|displayName)$", re.IGNORECASE)


def _lang_key_to_item_id(key: str, modid: str) -> str:
    """Best-effort: turn a lang key into a `modid:item_id` candidate.

    Returns "" if the key doesn't look like an item/block name.
    """
    if not key or not modid:
        return ""
    # only keep keys that end in .name / .displayName — those are user-facing labels
    if not _LANG_NAME_SUFFIX_RE.search(key):
        return ""
    m = _LANG_KEY_RE.match(key)
    if not m:
        # fallback: take everything between the type prefix and `.name`
        body = re.sub(r"^(tile|item|block|fluid)\.", "", key)
        body = _LANG_NAME_SUFFIX_RE.sub("", body)
        # drop a leading modid.
        body = re.sub(rf"^{re.escape(modid)}\.", "", body)
        # take last segment
        body = body.split(".")[-1]
    else:
        body = m.group(1)
    if not body:
        return ""
    # CamelCase → snake_case (Forge convention for registry names)
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", body).lower()
    snake = re.sub(r"[^a-z0-9_]+", "_", snake).strip("_")
    if not snake:
        return ""
    return f"{modid}:{snake}"


def _parse_lang(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    """Pull display-name pairs from assets/<modid>/lang/."""
    if not modid:
        return
    found_paths: list[str] = []
    for name in zf.namelist():
        if not name.startswith(f"assets/{modid}/lang/"):
            continue
        base = name.rsplit("/", 1)[-1]
        if base in _LANG_FILE_NAMES:
            found_paths.append(name)
    if not found_paths:
        # try other lang locales as a fallback so we still get *something*
        for name in zf.namelist():
            if name.startswith(f"assets/{modid}/lang/") and name.endswith((".lang", ".json")):
                found_paths.append(name)
                break
    for path in found_paths:
        try:
            text = zf.read(path).decode("utf-8", errors="replace")
        except Exception:
            continue
        if path.endswith(".json"):
            entries = _parse_lang_json(text)
        else:
            entries = _parse_lang_dot_lang(text)
        inv.raw_lang.update(entries)
        for k, v in entries.items():
            if not v:
                continue
            item_id = _lang_key_to_item_id(k, modid)
            if item_id and item_id not in inv.item_display_names:
                inv.item_display_names[item_id] = v


# ─────────────────────────────────────────────
# Blockstates → block registry names
# ─────────────────────────────────────────────


def _parse_blockstates(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    if not modid:
        return
    prefix = f"assets/{modid}/blockstates/"
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        bn = name[len(prefix):-len(".json")]
        if "/" in bn:
            continue
        bn = bn.strip()
        if bn:
            inv.block_ids.add(f"{modid}:{bn}")


# ─────────────────────────────────────────────
# Patchouli book extraction
# ─────────────────────────────────────────────


def _patchouli_collect_item_refs(obj: object, out: list[str]) -> None:
    """Recursively pull item-ID looking strings out of a Patchouli page tree."""
    if isinstance(obj, str):
        # patchouli supports "modid:item" and "modid:item{nbt}#meta"
        s = obj.strip()
        m = re.match(r"^([a-z0-9_]+):([a-z0-9_/.]+)", s)
        if m and len(m.group(1)) >= 2 and len(m.group(2)) >= 2:
            out.append(f"{m.group(1)}:{m.group(2)}")
        return
    if isinstance(obj, list):
        for x in obj:
            _patchouli_collect_item_refs(x, out)
        return
    if isinstance(obj, dict):
        # explicit fields we care about
        for key in ("item", "icon", "output", "result", "stack",
                    "main_item", "main_stack"):
            v = obj.get(key)
            if v is not None:
                _patchouli_collect_item_refs(v, out)
        # input lists in crafting pages
        for key in ("ingredients", "items", "recipes"):
            v = obj.get(key)
            if v is not None:
                _patchouli_collect_item_refs(v, out)


def _parse_patchouli(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    """Find Patchouli books inside the jar and parse their structure.

    Layout (1.12.2 with Patchouli):
        assets/<modid>/patchouli_books/<book_id>/book.json
        assets/<modid>/patchouli_books/<book_id>/en_us/categories/*.json
        assets/<modid>/patchouli_books/<book_id>/en_us/entries/**/*.json
    """
    if not modid:
        return
    prefix = f"assets/{modid}/patchouli_books/"
    book_ids: set[str] = set()
    for name in zf.namelist():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if "/" not in rel:
            continue
        bid = rel.split("/", 1)[0]
        if bid:
            book_ids.add(bid)

    for book_id in sorted(book_ids):
        book = PatchouliBook(book_id=f"{modid}:{book_id}", title=book_id)

        # try to read book.json for a title
        book_meta = f"{prefix}{book_id}/book.json"
        if book_meta in zf.namelist():
            try:
                meta = json.loads(zf.read(book_meta).decode("utf-8", errors="replace"))
                if isinstance(meta, dict):
                    name = meta.get("name") or meta.get("title")
                    if isinstance(name, str) and name.strip():
                        book.title = name.strip()
            except Exception:
                pass

        # categories
        cat_prefix = f"{prefix}{book_id}/en_us/categories/"
        for name in sorted(zf.namelist()):
            if not name.startswith(cat_prefix) or not name.endswith(".json"):
                continue
            try:
                data = json.loads(zf.read(name).decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            title = data.get("name") or name.split("/")[-1][:-5]
            if isinstance(title, str) and title.strip():
                book.categories.append(title.strip())

        # entries
        ent_prefix = f"{prefix}{book_id}/en_us/entries/"
        for name in sorted(zf.namelist()):
            if not name.startswith(ent_prefix) or not name.endswith(".json"):
                continue
            try:
                data = json.loads(zf.read(name).decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            ent_name = data.get("name") or name.split("/")[-1][:-5]
            category = data.get("category", "")
            icon = data.get("icon", "")
            order = data.get("sortnum", data.get("priority", 0))
            try:
                order = int(order)
            except Exception:
                order = 0
            refs: list[str] = []
            _patchouli_collect_item_refs(data.get("pages"), refs)
            _patchouli_collect_item_refs(icon, refs)
            # dedupe preserving order
            seen: set[str] = set()
            uniq_refs: list[str] = []
            for r in refs:
                if r not in seen:
                    seen.add(r)
                    uniq_refs.append(r)
            book.entries.append(PatchouliEntry(
                category=str(category) if isinstance(category, str) else "",
                name=str(ent_name) if isinstance(ent_name, str) else "",
                icon_item=str(icon) if isinstance(icon, str) and ":" in icon else "",
                items=uniq_refs,
                order=order,
            ))

        if book.categories or book.entries:
            inv.patchouli_books.append(book)


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────


def inspect_jar(jar_path: Path, modid: str = "") -> JarInventory:
    """Open `jar_path` and extract everything we can about its mod content.

    `modid` (optional) — if you already know the mod's registry id (from
    mcmod.info / mods.toml), pass it. Otherwise this function does a
    best-effort sniff to figure out which subdir under assets/ holds the
    main mod's assets.
    """
    inv = JarInventory(modid=modid)
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            if not modid:
                modid = _sniff_modid(zf)
                inv.modid = modid
            _parse_recipes(zf, modid_filter=modid or None, inv=inv)
            _parse_lang(zf, modid, inv)
            _parse_blockstates(zf, modid, inv)
            _parse_patchouli(zf, modid, inv)
    except (zipfile.BadZipFile, OSError):
        return inv
    return inv


def _sniff_modid(zf: zipfile.ZipFile) -> str:
    """If the caller didn't pass modid, guess from the most-populated
    assets/<modid>/ subdirectory (excluding 'minecraft' and 'forge')."""
    counts: dict[str, int] = {}
    for name in zf.namelist():
        if name.startswith("assets/") and name.count("/") >= 2:
            sub = name.split("/", 2)[1]
            if sub in ("minecraft", "forge", "fml", ""):
                continue
            counts[sub] = counts.get(sub, 0) + 1
    if not counts:
        return ""
    # pick the one with the most files
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ─────────────────────────────────────────────
# Prompt-friendly summary
# ─────────────────────────────────────────────


# items we never want to surface to the AI as quest targets
_TRIVIAL_SUFFIXES_RE = re.compile(
    r"(_slab|_stairs|_wall|_fence|_carpet|_panel|_button|_pressure_plate|"
    r"_trapdoor|_door|_pane|_block_white|_block_orange|_block_magenta|"
    r"_block_light_blue|_block_yellow|_block_lime|_block_pink|_block_gray|"
    r"_block_silver|_block_cyan|_block_purple|_block_blue|_block_brown|"
    r"_block_green|_block_red|_block_black|_block_light_gray)$"
)


def _looks_trivial(item_id: str) -> bool:
    base = item_id.split(":", 1)[1] if ":" in item_id else item_id
    return bool(_TRIVIAL_SUFFIXES_RE.search(base))


def _pretty_from_id(item_id: str) -> str:
    base = item_id.split(":", 1)[1] if ":" in item_id else item_id
    base = base.replace("_", " ").strip()
    return base.title() if base else item_id


def summarize_for_prompt(inv: JarInventory, max_items: int = 120) -> dict:
    """Build a structured summary that build_prompt() can splice into the
    AI prompt.

    Returns a dict with:
      • inventory_lines : list[str]  — "modid:item   Display Name"
      • inventory_total : int
      • patchouli_outline : list[str] — "Category: Entry" outline if available
      • patchouli_used : bool
    """
    items: dict[str, str] = {}
    # 1) start with recipe outputs (definitive, craftable)
    for iid in inv.recipe_outputs:
        if _looks_trivial(iid):
            continue
        display = inv.item_display_names.get(iid) or _pretty_from_id(iid)
        items[iid] = display
    # 2) supplement with lang-derived items (those with display names)
    for iid, display in inv.item_display_names.items():
        if _looks_trivial(iid):
            continue
        if iid not in items:
            items[iid] = display
    # 3) supplement with blockstates (only if room left)
    for iid in inv.block_ids:
        if _looks_trivial(iid):
            continue
        if iid not in items:
            items[iid] = _pretty_from_id(iid)

    # If we still went over the cap, prefer items that have BOTH a recipe and
    # a display name (those are the most quest-worthy: real, craftable, with
    # a human label).
    def _rank(item_id: str) -> tuple[int, int, str]:
        has_recipe = item_id in inv.recipe_outputs
        has_lang = item_id in inv.item_display_names
        return (0 if (has_recipe and has_lang) else
                1 if has_recipe else
                2 if has_lang else 3,
                -inv.recipe_outputs.get(item_id, 0),
                item_id)

    item_ids_sorted = sorted(items.keys(), key=_rank)
    if max_items and len(item_ids_sorted) > max_items:
        item_ids_sorted = item_ids_sorted[:max_items]

    inventory_lines = [
        f"  {iid}   {items[iid]}"
        for iid in item_ids_sorted
    ]

    # Patchouli outline (the mod author's own progression spine)
    patchouli_lines: list[str] = []
    for book in inv.patchouli_books:
        if not book.entries:
            continue
        patchouli_lines.append(f"[{book.title}]")
        sorted_entries = sorted(book.entries, key=lambda e: (e.category, e.order, e.name))
        last_cat = ""
        for ent in sorted_entries:
            if ent.category != last_cat:
                patchouli_lines.append(f"  • {ent.category or '(uncategorized)'}")
                last_cat = ent.category
            icon = ent.icon_item or (ent.items[0] if ent.items else "")
            tag = f"  → {icon}" if icon else ""
            patchouli_lines.append(f"      - {ent.name}{tag}")

    return {
        "inventory_lines": inventory_lines,
        "inventory_total": len(items),
        "inventory_truncated": max_items > 0 and len(items) > max_items,
        "patchouli_outline": patchouli_lines,
        "patchouli_used": bool(patchouli_lines),
    }
