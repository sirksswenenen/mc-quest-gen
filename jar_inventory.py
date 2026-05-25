"""
jar_inventory.py
================

Extracts the *real* item inventory of a Minecraft 1.12.2 mod from its .jar:

Registry-name sources (definitive — these are items the mod actually adds):
  • assets/<modid>/models/item/**/*.json       (filename = registry name)
  • assets/<modid>/blockstates/*.json          (filename = block registry name)
  • assets/<modid>/recipes/**/*.json           (result.item field)
  • data/<modid>/recipes/**/*.json             (newer mods)

Display-name sources (used to *label* registry items, not to invent them):
  • assets/<modid>/lang/en_us.lang | en_US.lang | en_us.json
  • assets/<modid>/lang_<modid>/en_us.properties     (IC2-style quirky path)

Mod-author progression backbone:
  • assets/<modid>/patchouli_books/<book>/en_us/{categories,entries}/*.json

The key design rule: lang files NEVER add new registry IDs to the inventory.
They only *decorate* IDs we already know are real (because they appear in
models/blockstates/recipes). That is what kills the IE-style problem of
sucking in Patchouli category names, advancements, subtitles, manuals, etc.

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
    name: str
    icon_item: str
    items: list[str]
    order: int = 0


@dataclass
class PatchouliBook:
    book_id: str
    title: str
    categories: list[str] = field(default_factory=list)
    entries: list[PatchouliEntry] = field(default_factory=list)


@dataclass
class JarInventory:
    modid: str = ""
    # Registry sources (truthy = "this item exists in the mod")
    recipe_outputs: dict[str, int] = field(default_factory=dict)    # item_id -> max craft count
    item_model_ids: set[str] = field(default_factory=set)           # from models/item/
    block_ids: set[str] = field(default_factory=set)                # from blockstates/
    # Display names (resolved against registry sources only)
    item_display_names: dict[str, str] = field(default_factory=dict)
    raw_lang: dict[str, str] = field(default_factory=dict)
    # Patchouli (mod author's own progression book)
    patchouli_books: list[PatchouliBook] = field(default_factory=list)

    def has_signal(self) -> bool:
        return bool(self.recipe_outputs or self.item_model_ids or
                    self.block_ids or self.patchouli_books)

    def all_item_ids(self) -> set[str]:
        out: set[str] = set(self.recipe_outputs)
        out |= self.item_model_ids
        out |= self.block_ids
        # also names referenced inside patchouli books (mod author's own list)
        for book in self.patchouli_books:
            for ent in book.entries:
                out.update(ent.items)
                if ent.icon_item:
                    out.add(ent.icon_item)
        return out


# ─────────────────────────────────────────────
# Recipe extraction
# ─────────────────────────────────────────────


def _extract_result_item(recipe: dict) -> tuple[str, int]:
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


def _parse_recipes(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    """Pick up recipe outputs from assets/<modid>/recipes/ and data/<modid>/recipes/."""
    for name in zf.namelist():
        if not name.endswith(".json"):
            continue
        parts = name.split("/")
        if len(parts) < 4:
            continue
        if parts[0] not in ("assets", "data"):
            continue
        if parts[2] != "recipes":
            continue
        if modid and parts[1] != modid:
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
# Item-model and blockstate extraction (the REAL registry-name truth)
# ─────────────────────────────────────────────


def _parse_item_models(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    """assets/<modid>/models/item/<...>/<name>.json
    Every registered item gets a model JSON (unless the mod ships custom
    model loaders — those are rare). The filename IS the registry name."""
    if not modid:
        return
    prefix = f"assets/{modid}/models/item/"
    skip_subdirs = {"builtin", "block"}     # not user-facing registry names
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        rel = name[len(prefix):-len(".json")]
        if not rel or rel.endswith("/"):
            continue
        # skip nested helper models like 'metal/inner_xyz' if they look like
        # internal variants -- the outermost folder is the registry name for
        # metadata-style items in many old mods. We keep them all, the
        # quality filter at the end will downrank duplicates.
        if any(rel.startswith(sd + "/") for sd in skip_subdirs):
            continue
        # take the deepest path component as the variant name; full path
        # joined with underscore captures sub-folder variants like
        # 'armor/bronze_helmet' -> 'armor_bronze_helmet' which matches
        # how Forge usually exposes such variants in lang files.
        flat = rel.replace("/", "_")
        # Drop pure-digit names like '36.json' (those are minecraft:36 leftovers)
        if flat.isdigit():
            continue
        inv.item_model_ids.add(f"{modid}:{flat}")


def _parse_blockstates(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
    if not modid:
        return
    prefix = f"assets/{modid}/blockstates/"
    for name in zf.namelist():
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        rel = name[len(prefix):-len(".json")]
        if "/" in rel or not rel:
            continue
        inv.block_ids.add(f"{modid}:{rel}")


# ─────────────────────────────────────────────
# Lang display-name extraction
# ─────────────────────────────────────────────


# Files we look at, in priority order
_LANG_CANDIDATE_NAMES = (
    "en_us.lang", "en_US.lang", "en_us.json", "en_US.json",
)
_PROPERTIES_CANDIDATE_NAMES = ("en_us.properties", "en_US.properties")

# Keys we trust as "this names an item/block". Anything else (chat,
# subtitle, gui, advancement, manual, …) is rejected.
_ITEMLIKE_PREFIX_RE = re.compile(
    r"^(item|tile|block|fluid|entity)\."
)
_NAME_SUFFIX_RE = re.compile(r"\.(name|displayname)$", re.IGNORECASE)
# Things that LOOK item-like but are NOT actual items.
# Match against the FULL key (not just the leading word), so e.g.
# 'item.immersiveengineering.tools.name' is rejected because it contains
# '.manual.' or '.category.' anywhere. Plain 'item.foo.tools.name' is fine.
_BAD_SEGMENT_RE = re.compile(
    r"(^|\.)(manual|tooltip|subtitle|desc|description|info|category|"
    r"shortname|tagline|landing|page|chat|subtitle|gui|key|modifier|"
    r"death|advancement|achievement|stat|book|container|inventory|menu|"
    r"options|commands|itemgroup|selectworld|createworld|connect|"
    r"disconnect|multiplayer|language|generator|gamemode|texturepack|"
    r"enchantment|narrator|filled_map|effect|biome|update|update_news|"
    r"news|credits|patreon|dev|page_|page0|page1|page2)(\.|$)",
    re.IGNORECASE,
)
# Lang-key top-level prefixes that are NEVER item/block names.
_BAD_TOP_PREFIX_RE = re.compile(
    r"^(desc|chat|subtitle|gui|key|modifier|death|potion|"
    r"advancement|achievement|stat|tooltip|info|book|"
    r"container|inventory|menu|options|commands|itemgroup|"
    r"selectworld|createworld|connect|disconnect|multiplayer|"
    r"language|generator|gamemode|texturepack|enchantment|"
    r"narrator|filled_map|effect|biome|the_|ie|update|news|"
    r"manual|credits)\."
)
# IC2-style prefixes that DO denote real items in some old mods.
_IC2_STYLE_PREFIX_RE = re.compile(
    r"^(te|cable|pipe|coke|wire)\.([a-z0-9_]+)$"
)


def _parse_lang_dot_lang(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff").strip()
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


def _read_all_lang(zf: zipfile.ZipFile, modid: str) -> dict[str, str]:
    """Read every English-locale lang/properties file in the mod and merge."""
    merged: dict[str, str] = {}
    if not modid:
        return merged
    candidates: list[str] = []
    # 1) Standard layout: assets/<modid>/lang/en_us.{lang,json}
    for n in zf.namelist():
        if not n.startswith(f"assets/{modid}/lang/"):
            continue
        base = n.rsplit("/", 1)[-1]
        if base in _LANG_CANDIDATE_NAMES:
            candidates.append(n)
    # 2) Quirky layout: assets/<modid>/lang_<modid>/en_us.properties
    for n in zf.namelist():
        if "/lang_" not in n:
            continue
        base = n.rsplit("/", 1)[-1]
        if base in _PROPERTIES_CANDIDATE_NAMES:
            candidates.append(n)
    # 3) Last resort: ANY en_us* in this modid's assets dir
    if not candidates:
        for n in zf.namelist():
            if not n.startswith(f"assets/{modid}/"):
                continue
            base = n.rsplit("/", 1)[-1].lower()
            if base.startswith("en_us") and (n.endswith(".lang") or n.endswith(".json")
                                             or n.endswith(".properties")):
                candidates.append(n)
                break
    for path in candidates:
        try:
            text = zf.read(path).decode("utf-8", errors="replace")
        except Exception:
            continue
        if path.endswith(".json"):
            merged.update(_parse_lang_json(text))
        else:
            merged.update(_parse_lang_dot_lang(text))
    return merged


def _candidate_lang_keys(modid: str, base: str) -> list[str]:
    """All lang keys that might carry a display name for `modid:base`.

    Order matters — earlier keys are preferred.
    """
    keys = [
        f"item.{modid}.{base}.name",
        f"tile.{modid}.{base}.name",
        f"block.{modid}.{base}.name",
        f"fluid.{modid}.{base}.name",
        f"item.{modid}.{base}",
        f"tile.{modid}.{base}",
        # IC2-style: bare names + 'te.<x>', 'cable.<x>', 'pipe.<x>'
        f"te.{base}",
        f"cable.{base}",
        f"pipe.{base}",
        f"item.{base}.name",
        f"tile.{base}.name",
        f"block.{base}.name",
        base,
    ]
    # also try CamelCase variants of 'base'
    camel = "".join(p.title() for p in base.split("_"))
    keys += [
        f"item.{camel}.name",
        f"tile.{camel}.name",
        f"item.{modid}.{camel}.name",
        f"tile.{modid}.{camel}.name",
    ]
    seen: set[str] = set()
    uniq: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _assign_display_names(modid: str, lang: dict[str, str], inv: JarInventory) -> None:
    """For every registry item we know is real, try to find its display name
    in the lang map. Lang never adds new items here — it only labels them."""
    if not modid or not lang:
        return
    known: set[str] = set(inv.recipe_outputs)
    known |= inv.item_model_ids
    known |= inv.block_ids
    for iid in known:
        if ":" not in iid:
            continue
        ns, base = iid.split(":", 1)
        if ns != modid:
            continue
        if iid in inv.item_display_names:
            continue
        for k in _candidate_lang_keys(modid, base):
            v = lang.get(k)
            if v is None:
                continue
            # tolerate Forge §-color codes
            v_clean = re.sub(r"§[0-9a-fk-or]", "", v).strip()
            if v_clean:
                inv.item_display_names[iid] = v_clean
                break

    # Store the raw lang map (capped to avoid blowing up memory)
    if len(lang) <= 5000:
        inv.raw_lang.update(lang)


def _strip_color_codes(s: str) -> str:
    s = re.sub(r"§[0-9a-fk-or]", "", s)
    return s.strip()


def _harvest_items_from_lang(modid: str, lang: dict[str, str], inv: JarInventory) -> None:
    """Walk the lang map and harvest item IDs that the file structure missed.

    This is the safety net for old / obfuscated mods that don't ship
    standard JSON item models:
      • IC2-style: `te.macerator = Macerator`,  `cable.copper_cable_0 = ...`,
        `pipe.steel_pipe_small = ...`. The portion after the prefix IS the
        registry-name suffix in IC2's actual code.
      • Forge-conventional but metadata-based: `item.<modid>.<sub>.<variant>.name`,
        where <sub> is the registry name and <variant> is a metadata label
        (e.g. `item.immersiveengineering.metal.ingot_copper.name = Copper Ingot`).
        For quest prompts we expose `<modid>:<sub>_<variant>` so the AI can
        refer to the specific variant. FTB Quests can match these via NBT.
      • Simple `item.<X>.name` / `tile.<X>.name` keys, snake_cased.

    Patchouli book entries (`ie.manual.entry.X.name`, `*.category.*`, etc.),
    subtitles, tooltips, advancements, GUIs and the like are filtered out
    via the BAD_TOP_PREFIX / BAD_SEGMENT rules.
    """
    if not modid or not lang:
        return
    for k, v in lang.items():
        if not isinstance(v, str):
            continue
        v_clean = _strip_color_codes(v)
        if not v_clean:
            continue
        kl = k.lower()

        # 1) IC2-style: 'te.macerator', 'cable.copper_cable_0', etc.
        m = _IC2_STYLE_PREFIX_RE.match(kl)
        if m:
            base = m.group(2)
            if base.isdigit():
                continue
            iid = f"{modid}:{base}"
            inv.item_model_ids.add(iid)
            inv.item_display_names.setdefault(iid, v_clean)
            continue

        # 2) Forge-conventional: item./tile./block./fluid./entity. + .name
        if not _ITEMLIKE_PREFIX_RE.match(kl):
            continue
        if not _NAME_SUFFIX_RE.search(kl):
            continue
        if _BAD_TOP_PREFIX_RE.match(kl):
            continue
        if _BAD_SEGMENT_RE.search(kl):
            continue

        # Strip prefix + .name suffix to get the body
        body = re.sub(r"^(item|tile|block|fluid|entity)\.", "", k, flags=re.IGNORECASE)
        body = _NAME_SUFFIX_RE.sub("", body)
        body_parts = body.split(".")
        # Strip optional modid prefix from the body
        if body_parts and body_parts[0].lower() == modid.lower():
            body_parts = body_parts[1:]
        if not body_parts:
            continue
        flat = "_".join(body_parts)
        # CamelCase -> snake_case for keys like item.ItemPlateIron.name
        flat = re.sub(r"(?<!^)(?=[A-Z])", "_", flat).lower()
        flat = re.sub(r"[^a-z0-9_]+", "_", flat).strip("_")
        if not flat or flat.isdigit() or len(flat) < 2:
            continue
        iid = f"{modid}:{flat}"
        inv.item_model_ids.add(iid)
        inv.item_display_names.setdefault(iid, v_clean)


# ─────────────────────────────────────────────
# Patchouli book extraction
# ─────────────────────────────────────────────


def _patchouli_collect_item_refs(obj: object, out: list[str]) -> None:
    if isinstance(obj, str):
        s = obj.strip()
        m = re.match(r"^([a-z0-9_]+):([a-z0-9_/.-]+)", s)
        if m and len(m.group(1)) >= 2 and len(m.group(2)) >= 2:
            out.append(f"{m.group(1)}:{m.group(2)}")
        return
    if isinstance(obj, list):
        for x in obj:
            _patchouli_collect_item_refs(x, out)
        return
    if isinstance(obj, dict):
        for key in ("item", "icon", "output", "result", "stack",
                    "main_item", "main_stack"):
            v = obj.get(key)
            if v is not None:
                _patchouli_collect_item_refs(v, out)
        for key in ("ingredients", "items", "recipes"):
            v = obj.get(key)
            if v is not None:
                _patchouli_collect_item_refs(v, out)


def _parse_patchouli(zf: zipfile.ZipFile, modid: str, inv: JarInventory) -> None:
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
    inv = JarInventory(modid=modid)
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            if not modid:
                modid = _sniff_modid(zf)
                inv.modid = modid
            _parse_recipes(zf, modid, inv)
            _parse_item_models(zf, modid, inv)
            _parse_blockstates(zf, modid, inv)
            lang = _read_all_lang(zf, modid)
            _assign_display_names(modid, lang, inv)
            _harvest_items_from_lang(modid, lang, inv)
            _parse_patchouli(zf, modid, inv)
    except (zipfile.BadZipFile, OSError):
        return inv
    return inv


def _sniff_modid(zf: zipfile.ZipFile) -> str:
    counts: dict[str, int] = {}
    for name in zf.namelist():
        if name.startswith("assets/") and name.count("/") >= 2:
            sub = name.split("/", 2)[1]
            if sub in ("minecraft", "forge", "fml", ""):
                continue
            counts[sub] = counts.get(sub, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ─────────────────────────────────────────────
# Prompt-friendly summary
# ─────────────────────────────────────────────


# items we never want to surface to the AI as quest targets
# Matches `_stairs`, `_slab`, etc. either at end of string, or followed by
# digits (variant suffix) or another underscore segment (e.g. concrete_stairs_xyz).
_TRIVIAL_SUFFIXES_RE = re.compile(
    r"(_slab|_stairs|_wall|_fence|_carpet|_panel|_button|_pressure_plate|"
    r"_trapdoor|_door|_pane)(_[a-z0-9]+)*\d*$"
)
# Variant suffixes like _0/_1/_2 from metadata items — when there are multiple
# variants we keep just the most informative one (lowest index).
_VARIANT_TAIL_RE = re.compile(r"_(\d+)$")


def _looks_trivial(item_id: str) -> bool:
    base = item_id.split(":", 1)[1] if ":" in item_id else item_id
    if _TRIVIAL_SUFFIXES_RE.search(base):
        return True
    # decorative variants
    if base.endswith(("_block_white", "_block_black", "_block_red", "_block_green",
                      "_block_blue", "_block_yellow", "_block_orange",
                      "_block_purple", "_block_pink", "_block_cyan",
                      "_block_brown", "_block_gray", "_block_light_gray",
                      "_block_lime", "_block_magenta", "_block_silver",
                      "_block_light_blue")):
        return True
    return False


def _pretty_from_id(item_id: str) -> str:
    base = item_id.split(":", 1)[1] if ":" in item_id else item_id
    base = base.replace("_", " ").strip()
    return base.title() if base else item_id


def summarize_for_prompt(inv: JarInventory, max_items: int = 200) -> dict:
    """Build a structured summary that build_prompt() splices into the prompt.

    Returns:
      inventory_lines: list[str] — "modid:item   Display Name" (capped to max_items)
      inventory_total: int       — full unique-item count BEFORE the cap
      inventory_truncated: bool
      patchouli_outline: list[str] — "[Book]\n  • Category\n      - Entry" lines
      patchouli_used: bool
    """
    # 1) Gather every real registry id from the jar
    real_ids: set[str] = set()
    real_ids |= set(inv.recipe_outputs)
    real_ids |= inv.item_model_ids
    real_ids |= inv.block_ids

    # 2) Drop trivial cosmetic variants (slabs/stairs/colored versions)
    candidates = [iid for iid in real_ids if not _looks_trivial(iid)]

    # 3) Collapse metadata _0/_1/... variants when the base item is present
    base_to_variants: dict[str, list[str]] = {}
    for iid in candidates:
        m = _VARIANT_TAIL_RE.search(iid)
        if m:
            base_to_variants.setdefault(iid[: m.start()], []).append(iid)
    # If a "base" item exists in candidates AND it has _N variants, keep only
    # the base item plus the LOWEST-index variant (representative of the family)
    kept: set[str] = set(candidates)
    for base, variants in base_to_variants.items():
        if base in kept and len(variants) > 1:
            variants_sorted = sorted(variants, key=lambda v: int(_VARIANT_TAIL_RE.search(v).group(1)))
            for v in variants_sorted[1:]:
                kept.discard(v)

    # 4) Build display map
    display: dict[str, str] = {}
    for iid in kept:
        display[iid] = inv.item_display_names.get(iid) or _pretty_from_id(iid)

    # 5) Rank: items with BOTH recipe + display name first; then display name;
    # then recipes; then bare model/blockstate ids without a label.
    def _rank(item_id: str) -> tuple[int, int, str]:
        has_recipe = item_id in inv.recipe_outputs
        has_named = item_id in inv.item_display_names
        if has_recipe and has_named:
            tier = 0
        elif has_named:
            tier = 1
        elif has_recipe:
            tier = 2
        else:
            tier = 3
        return (tier, -inv.recipe_outputs.get(item_id, 0), item_id)

    sorted_ids = sorted(display.keys(), key=_rank)
    total = len(sorted_ids)
    truncated = max_items > 0 and total > max_items
    if truncated:
        sorted_ids = sorted_ids[:max_items]

    inventory_lines = [f"  {iid}   {display[iid]}" for iid in sorted_ids]

    # 6) Patchouli outline (mod author's own progression spine)
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
        "inventory_total": total,
        "inventory_truncated": truncated,
        "patchouli_outline": patchouli_lines,
        "patchouli_used": bool(patchouli_lines),
    }
