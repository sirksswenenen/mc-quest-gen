"""
Scraper module: fetches mod metadata + progression hints from the web.

Strategy (in order):
  1. CurseForge API        → best 1.12.2 coverage, requires CF_API_KEY (optional)
  2. Modrinth direct slug  → if we know the real modid, fetch the project directly
  3. Modrinth strict search→ fuzzy search rejecting matches with no token overlap
  4. FTB Wiki (MediaWiki)  → "Getting Started" / mod page wikitext as context
  5. Minecraft Wiki Fandom → fallback context for vanilla-adjacent topics
  6. Local KNOWN_MOD_STAGES→ curated progression hints for popular 1.12.2 mods

We never feed actual *recipes* to the AI — the AI just needs to know what mod
we're talking about and roughly which tiers exist. Item IDs come from the
real modid (from jar metadata when available) + slugified quest title.

Design rule: it is *strictly better* to return "no match" than to return
a wrong match — a wrong slug becomes a wrong modid prefix on every quest
item and silently breaks the entire chapter.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

_HEADERS = {
    "User-Agent": (
        "mc-quest-gen/2.0 "
        "(+https://github.com/sirksswenenen/mc-quest-gen)"
    ),
    "Accept": "application/json, */*",
}


# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> Optional[str]:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    except Exception:
        return None


def _get_json(url: str, timeout: int = 15) -> Optional[dict | list]:
    raw = _get(url, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────
# Modrinth
# ─────────────────────────────────────────────

MODRINTH_BASE = "https://api.modrinth.com/v2"
CURSEFORGE_BASE = "https://api.curseforge.com/v1"
CF_MC_GAME_ID = 432            # CurseForge "Minecraft" gameId
CF_MODS_CLASS_ID = 6           # "Mods" category class

_MIN_TOKEN_LEN = 4
_STOPWORDS = {
    "the", "and", "mod", "mc", "for", "of", "a", "an",
    "forge", "fabric", "backport", "continuous", "reborn",
    "community", "edition", "classic", "plus",
}


def _tokens(s: str) -> set[str]:
    """Significant tokens for match validation — lowercase, ≥ _MIN_TOKEN_LEN chars,
    not in _STOPWORDS, alphanumeric only."""
    raw = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _is_acceptable_match(query: str, modid: str, hit_title: str, hit_slug: str) -> bool:
    """Return True only if the hit clearly identifies the same mod as the query.

    Rules (in order):
      1. Exact slug == modid             → accept
      2. ALL significant query tokens appear in the hit's title or slug → accept
      3. Otherwise                       → reject

    "Significant tokens" are alphanumeric, ≥4 chars, not in _STOPWORDS.
    The subset rule is what kills "Industrialcraft 2" → "to-the-stars" and
    "Advanced Solar Panels" → "Ultimate Solar Panels" fuzzy disasters.
    """
    slug_low = hit_slug.lower()
    modid_low = modid.lower()
    if modid_low and (slug_low == modid_low or slug_low.replace("-", "_") == modid_low
                      or slug_low.replace("-", "") == modid_low.replace("_", "")):
        return True
    name_tokens = _tokens(query)
    hit_tokens = _tokens(hit_title) | _tokens(hit_slug.replace("-", " "))
    if not name_tokens or not hit_tokens:
        return False
    return name_tokens.issubset(hit_tokens)


def get_modrinth_project(slug: str) -> Optional[dict]:
    """Direct project lookup by slug or id. Returns None on 404."""
    if not slug:
        return None
    s = urllib.parse.quote(slug.strip().lower())
    data = _get_json(f"{MODRINTH_BASE}/project/{s}")
    if isinstance(data, dict) and data.get("slug"):
        return data
    return None


def _modrinth_project_to_search_shape(p: dict) -> dict:
    """Convert /project response shape into the same fields /search uses."""
    return {
        "slug":          p.get("slug", ""),
        "title":         p.get("title", ""),
        "description":   p.get("description", ""),
        "categories":    p.get("categories", []) or [],
        "downloads":     p.get("downloads", 0),
        "icon_url":      p.get("icon_url", ""),
        "game_versions": p.get("game_versions", []) or [],
        "versions":      p.get("versions", []) or [],
    }


def search_modrinth(
    mod_name: str,
    game_version: str = "1.12.2",
    modid: str = "",
) -> Optional[dict]:
    """Strict Modrinth lookup. Tries direct slug match first, then a fuzzy search
    whose results are filtered to those sharing at least one significant token
    with the query/modid. Returns None rather than the wrong project."""
    # 1. Direct slug lookup using modid — cheapest + most reliable
    if modid:
        for candidate in (modid, modid.replace("_", "-"), modid.replace("-", "_")):
            proj = get_modrinth_project(candidate)
            if proj:
                return _modrinth_project_to_search_shape(proj)
    # 2. Direct slug lookup using a slug-form of the display name
    slug_guess = re.sub(r"[^a-z0-9]+", "-", mod_name.lower()).strip("-")
    if slug_guess and slug_guess != modid:
        proj = get_modrinth_project(slug_guess)
        if proj:
            return _modrinth_project_to_search_shape(proj)

    # 3. Fuzzy search with strict validation
    q = urllib.parse.quote(mod_name)
    facets = urllib.parse.quote(json.dumps([
        [f"game_versions:{game_version}"],
        ["project_type:mod"],
    ]))
    data = _get_json(f"{MODRINTH_BASE}/search?query={q}&facets={facets}&limit=8")
    hits: list[dict] = []
    if isinstance(data, dict):
        hits = data.get("hits") or []
    if not hits:
        data = _get_json(f"{MODRINTH_BASE}/search?query={q}&limit=8")
        if isinstance(data, dict):
            hits = data.get("hits") or []
    if not hits:
        return None
    target = mod_name.lower()
    hits.sort(key=lambda h: _similarity(h.get("title", ""), target), reverse=True)
    for h in hits:
        if _is_acceptable_match(mod_name, modid, h.get("title", ""), h.get("slug", "")):
            return h
    return None


# ─────────────────────────────────────────────
# CurseForge — best 1.12.2 mod coverage (IC2, ExtraUtils 2, Avaritia, etc.)
# Requires CF_API_KEY (free from https://console.curseforge.com)
# ─────────────────────────────────────────────

_CURSEFORGE_API_KEY: Optional[str] = None


def configure_curseforge(api_key: Optional[str]) -> None:
    """Tell scraper which CurseForge key to use, if any."""
    global _CURSEFORGE_API_KEY
    _CURSEFORGE_API_KEY = (api_key or "").strip() or None


def _cf_get(path: str, params: dict[str, str]) -> Optional[dict]:
    if not _CURSEFORGE_API_KEY:
        return None
    qs = urllib.parse.urlencode(params)
    url = f"{CURSEFORGE_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        **_HEADERS,
        "x-api-key": _CURSEFORGE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _cf_to_search_shape(p: dict) -> dict:
    """Convert CF mod object → same shape we use for Modrinth hits."""
    cats = [c.get("name", "") for c in p.get("categories", []) if isinstance(c, dict)]
    logo = (p.get("logo") or {}).get("thumbnailUrl") or (p.get("logo") or {}).get("url") or ""
    versions = []
    for f in p.get("latestFilesIndexes", []) or []:
        v = f.get("gameVersion")
        if v:
            versions.append(v)
    return {
        "slug":          p.get("slug", ""),
        "title":         p.get("name", ""),
        "description":   p.get("summary", "") or "",
        "categories":    [c.lower() for c in cats],
        "downloads":     int(p.get("downloadCount") or 0),
        "icon_url":      logo,
        "game_versions": list(set(versions)),
        "versions":      list(set(versions)),
        "source":        "curseforge",
        "cf_id":         p.get("id"),
    }


# Display-name → canonical in-game modid map, for the cases where the user
# provides a mod name without a .jar (so we cannot read mcmod.info) AND the
# online slug differs from the actual modid embedded in items.
# Only meaningful for 1.12.2 since modids are stable per major version.
_KNOWN_MODIDS_1_12_2: dict[str, str] = {
    "industrialcraft 2":           "ic2",
    "industrial craft 2":          "ic2",
    "industrial craft":            "ic2",
    "ic2":                         "ic2",
    "ic2 classic":                 "ic2",            # IC2 Classic shares modid
    "industrial craft classic":    "ic2",
    "extra utilities 2":           "extrautils2",
    "extra utilities":             "extrautils2",
    "extra utilities classic":     "extrautils2",
    "modern warfare cubed":        "mw",
    "tinkers' construct":          "tconstruct",
    "tinkers construct":           "tconstruct",
    "applied energistics 2":       "appliedenergistics2",
    "applied energistics":         "appliedenergistics2",
    "draconic evolution":          "draconicevolution",
    "thermal expansion":           "thermalexpansion",
    "thermal foundation":          "thermalfoundation",
    "thermal dynamics":            "thermaldynamics",
    "ender io":                    "enderio",
    "buildcraft":                  "buildcraft",
    "blood magic":                 "bloodmagic",
    "astral sorcery":              "astralsorcery",
    "immersive engineering":       "immersiveengineering",
    "refined storage":             "refinedstorage",
    "biomes o' plenty":            "biomesoplenty",
    "biomes o plenty":             "biomesoplenty",
    "just enough items":           "jei",
    "not enough items":            "notenoughitems",
    "world edit":                  "worldedit",
    "worldedit":                   "worldedit",
    "thaumcraft":                  "thaumcraft",
    "botania":                     "botania",
    "mekanism":                    "mekanism",
    "mekanism generators":         "mekanismgenerators",
    "mekanism tools":              "mekanismtools",
    "actually additions":          "actuallyadditions",
    "extreme reactors":            "bigreactors",
    "big reactors":                "bigreactors",
}


def canonical_modid_for(mod_name: str, game_version: str = "1.12.2") -> str:
    """Return the in-game modid for the given display name, if we know it.
    Use only as a fallback for mod-names provided without a .jar."""
    if game_version != "1.12.2":
        return ""
    return _KNOWN_MODIDS_1_12_2.get(mod_name.strip().lower(), "")


# Curated (modid_or_lowername, game_version) → CurseForge slug overrides.
# For 1.12.2-specific quirks where the obvious search fails to surface the
# real mod (CF sorts by all-time popularity and old 1.7 forks dominate).
_CF_SLUG_OVERRIDES: dict[tuple[str, str], str] = {
    ("ic2", "1.12.2"):                  "industrial-craft",
    ("industrialcraft", "1.12.2"):      "industrial-craft",
    ("industrial craft 2", "1.12.2"):   "industrial-craft",
    ("galacticraft", "1.12.2"):         "galacticraft-legacy",
    ("galacticraftcore", "1.12.2"):     "galacticraft-legacy",
    ("avaritia", "1.12.2"):             "avaritia-1-10",
    ("extrautils2", "1.12.2"):          "extra-utilities",
    ("extra utilities 2", "1.12.2"):    "extra-utilities",
    ("mw", "1.12.2"):                   "modern-warfare-cubed",
    ("crossbows", "1.12.2"):            "crossbows-backport",
    ("xaerominimap", "1.12.2"):         "xaeros-minimap",
    ("xaeroworldmap", "1.12.2"):        "xaeros-world-map",
    ("worldedit", "1.12.2"):            "worldedit",
    ("ftblib", "1.12.2"):               "ftb-library-legacy-forge",
    ("ftbquests", "1.12.2"):            "ftb-quests-legacy-forge",
    ("ftbutilities", "1.12.2"):         "ftb-utilities",
}


def _cf_get_by_slug(slug: str, game_version: str = "1.12.2") -> Optional[dict]:
    """Direct slug lookup via /mods/search?slug=… (the only CF endpoint that
    accepts a slug filter). Returns the first match or None."""
    if not slug:
        return None
    data = _cf_get("/mods/search", {
        "gameId":      str(CF_MC_GAME_ID),
        "classId":     str(CF_MODS_CLASS_ID),
        "slug":        slug.lower(),
        "gameVersion": game_version,
    })
    hits = (data or {}).get("data", []) or []
    return hits[0] if hits else None


def _cf_match_quality(query: str, modid: str, hit: dict) -> int:
    """Higher = better. Used to pick the best CurseForge match among fuzzy hits."""
    title = hit.get("name", "") or ""
    slug = (hit.get("slug", "") or "").lower()
    q_name = query.lower().strip()
    q_id = modid.lower().strip()
    q_slug = re.sub(r"[^a-z0-9]+", "-", q_name).strip("-")
    score = 0
    if slug == q_id or slug == q_slug:
        score += 1000
    if slug.replace("-", "") == q_id.replace("_", "") or slug.replace("-", "") == q_name.replace(" ", ""):
        score += 500
    if title.lower() == q_name:
        score += 200
    q_tokens = _tokens(q_name) | _tokens(q_id)
    h_tokens = _tokens(title) | _tokens(slug.replace("-", " "))
    extra = h_tokens - q_tokens
    score -= 10 * len(extra)
    # Mild downloads tiebreaker
    dl = int(hit.get("downloadCount", 0) or 0)
    if dl > 1_000_000:
        score += 3
    elif dl > 100_000:
        score += 2
    elif dl > 10_000:
        score += 1
    return score


def search_curseforge(
    mod_name: str,
    game_version: str = "1.12.2",
    modid: str = "",
) -> Optional[dict]:
    if not _CURSEFORGE_API_KEY:
        return None

    # 1. Direct slug lookup — try the display-name-as-slug first (catches
    #    "IC2 Classic" before the "ic2" override snags it), then the raw
    #    modid, then the modid with "_"→"-" rewriting.
    slug_from_name = re.sub(r"[^a-z0-9]+", "-", mod_name.lower()).strip("-")
    candidates: list[str] = []
    if slug_from_name:
        candidates.append(slug_from_name)
    if modid:
        candidates.append(modid.lower())
        candidates.append(modid.lower().replace("_", "-"))
    seen: set[str] = set()
    for slug in candidates:
        if slug in seen or not slug:
            continue
        seen.add(slug)
        hit = _cf_get_by_slug(slug, game_version)
        if hit:
            return _cf_to_search_shape(hit)

    # 2. Curated override — for cases where CF stores the mod under a slug
    #    we can't guess (e.g. ic2 → "industrial-craft", galacticraft →
    #    "galacticraft-legacy" for 1.12.2)
    for key in (modid.lower(), mod_name.lower()):
        if (key, game_version) in _CF_SLUG_OVERRIDES:
            override_slug = _CF_SLUG_OVERRIDES[(key, game_version)]
            hit = _cf_get_by_slug(override_slug, game_version)
            if hit:
                return _cf_to_search_shape(hit)

    # 3. Fuzzy search — but rank hits by match quality, not by CF's popularity
    data = _cf_get("/mods/search", {
        "gameId":       str(CF_MC_GAME_ID),
        "classId":      str(CF_MODS_CLASS_ID),
        "searchFilter": mod_name,
        "gameVersion":  game_version,
        "sortField":    "2",   # popularity
        "sortOrder":    "desc",
        "pageSize":     "20",
    })
    hits = (data or {}).get("data", []) or []
    candidates_q = [
        h for h in hits
        if _is_acceptable_match(mod_name, modid, h.get("name", ""), h.get("slug", ""))
    ]
    if not candidates_q:
        return None
    candidates_q.sort(key=lambda h: _cf_match_quality(mod_name, modid, h), reverse=True)
    return _cf_to_search_shape(candidates_q[0])


# ─────────────────────────────────────────────
# FTB Wiki
# ─────────────────────────────────────────────

FTB_WIKI_API = "https://ftbwiki.org/api.php"
MC_WIKI_API = "https://minecraft.wiki/api.php"


def _wiki_search(api: str, query: str, limit: int = 5) -> list[str]:
    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
    })
    data = _get_json(f"{api}?{params}")
    if not isinstance(data, dict):
        return []
    return [r["title"] for r in (data.get("query") or {}).get("search", [])]


def _wiki_page_text(api: str, title: str) -> str:
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
    })
    data = _get_json(f"{api}?{params}")
    if not isinstance(data, dict):
        return ""
    for page in (data.get("query") or {}).get("pages", {}).values():
        slots = (page.get("revisions") or [{}])[0].get("slots", {}).get("main", {})
        return slots.get("*", "")
    return ""


_WIKI_FILE_RE = re.compile(r"\[\[File:[^\]]+\]\]")
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]")
_WIKI_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_WIKI_HEADER_RE = re.compile(r"==+\s*([^=]+?)\s*==+")
_WIKI_REFS_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL)
_WIKI_HTML_RE = re.compile(r"<[^>]+>")


def _clean_wikitext(text: str) -> str:
    text = _WIKI_FILE_RE.sub("", text)
    text = _WIKI_REFS_RE.sub("", text)
    # Repeatedly remove templates to handle nesting one level deep
    for _ in range(3):
        text = _WIKI_TEMPLATE_RE.sub("", text)
    text = _WIKI_LINK_RE.sub(r"\1", text)
    text = _WIKI_HEADER_RE.sub(r"\n## \1\n", text)
    text = _WIKI_HTML_RE.sub("", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_mod_wiki_info(mod_name: str) -> str:
    """Try FTB Wiki first, then Minecraft Wiki. Returns a plain-text snippet."""
    for api in (FTB_WIKI_API, MC_WIKI_API):
        titles = _wiki_search(api, mod_name, limit=4)
        prefer = [
            t for t in titles
            if any(k in t.lower() for k in (
                "getting started", "guide", "progression",
                "tutorial", "tier", "progress",
            ))
        ]
        for title in (prefer or titles)[:2]:
            text = _wiki_page_text(api, title)
            if text:
                cleaned = _clean_wikitext(text)
                if len(cleaned) > 200:
                    return cleaned[:3000]
        time.sleep(0.2)
    return ""


# ─────────────────────────────────────────────
# Mod info aggregator
# ─────────────────────────────────────────────

def get_mod_info(
    mod_name: str,
    game_version: str = "1.12.2",
    modid: str = "",
) -> dict:
    """Aggregate mod metadata.

    `modid` may come from a jar's mcmod.info — it is treated as the authoritative
    identifier. Online sources only confirm/decorate it; they never override it.
    If no modid is supplied, we fall back to a small curated display-name → modid
    map (see _KNOWN_MODIDS_1_12_2) so item IDs come out right for common mods.
    """
    if not modid:
        modid = canonical_modid_for(mod_name, game_version)

    info: dict = {
        "name": mod_name,
        "game_version": game_version,
        "description": "",
        "categories": [],
        "modid": modid or "",
        "wiki_snippet": "",
        "icon_url": "",
        "source": "unknown",
    }

    # 1. CurseForge first (only attempted if user has CF_API_KEY configured)
    cf_hit = search_curseforge(mod_name, game_version, modid=modid)
    if cf_hit:
        info["name"] = cf_hit.get("title") or mod_name
        info["description"] = cf_hit.get("description") or ""
        info["categories"] = cf_hit.get("categories") or []
        info["icon_url"] = cf_hit.get("icon_url") or ""
        # Trust jar modid over CF slug for in-game item IDs
        if not modid:
            info["modid"] = cf_hit.get("slug", "") or ""
        info["source"] = "curseforge"

    # 2. Modrinth — only with strict validation; fills gaps when CF empty
    if info["source"] != "curseforge":
        m_hit = search_modrinth(mod_name, game_version, modid=modid)
        if m_hit:
            info["name"] = m_hit.get("title") or mod_name
            info["description"] = m_hit.get("description") or ""
            info["categories"] = m_hit.get("categories") or []
            info["icon_url"] = m_hit.get("icon_url") or ""
            if not modid:
                info["modid"] = m_hit.get("slug", "") or info["modid"]
            info["source"] = "modrinth"
    else:
        # Even when CF succeeded, try Modrinth for icon if CF didn't have one
        if not info["icon_url"]:
            m_hit = search_modrinth(mod_name, game_version, modid=modid)
            if m_hit and m_hit.get("icon_url"):
                info["icon_url"] = m_hit["icon_url"]

    time.sleep(0.2)
    info["wiki_snippet"] = fetch_mod_wiki_info(mod_name)

    # 3. Heuristic modid fallback (lowercase, strip non-alnum) — only if we
    # still have nothing; preserves the jar's modid when present.
    if not info["modid"]:
        info["modid"] = re.sub(r"[^a-z0-9_]", "", mod_name.lower().replace(" ", "_"))

    return info


# ─────────────────────────────────────────────
# Curated progression data for popular 1.12.2 mods
# ─────────────────────────────────────────────

KNOWN_MOD_STAGES: dict[str, list[str]] = {
    "industrialcraft": [
        "Copper/Tin/Iron age: basic machines (Macerator, Furnace, Generator)",
        "Bronze/Steel age: Extractor, Compressor, Electrolyzer",
        "Advanced machines: Industrial Macerator, Centrifuge",
        "Nuclear: Reactor components, EU storage",
        "Quantum age: Quantum Suit, Matter Fabricator",
    ],
    "ic2": [
        "Basic EU generation (Generator, Solar Panel, Wind Mill)",
        "Ore processing (Macerator → Ore Dust → Furnace → 2x ingots)",
        "Cables & Storage (BatBox → MFE → MFSU)",
        "Advanced machines (Industrial Grinder, Industrial Centrifuge)",
        "Nano/Quantum armor",
        "Nuclear Reactor",
    ],
    "thermal expansion": [
        "Machines: Pulverizer, Smeltery, Redstone Furnace",
        "Power: Dynamos (Steam, Compression, Magmatic, Numismatic)",
        "Storage: Energy Cell tiers (Basic → Hardened → Reinforced → Signalum → Resonant)",
        "Augments & upgrades",
        "Satchels, Strongboxes, Cache",
        "Flux Networks integration",
    ],
    "applied energistics": [
        "Certus Quartz & Nether Quartz grind",
        "ME Network basics: Controller, ME Drive, ME Terminal",
        "Autocrafting: Molecular Assembler, Crafting CPU",
        "P2P Tunnels, Quantum Network Bridge",
        "ME Interfaces & Patterns",
    ],
    "thaumcraft": [
        "Research basics: Thaumonomicon, Arcane Workbench",
        "Vis & Aspects research, Thaumometer",
        "Golem automation: Clay/Wood/Stone/Iron golems",
        "Infusion altar & Runic matrix",
        "Eldritch secrets, Outer Lands",
    ],
    "draconic evolution": [
        "Draconium Ore & Dust",
        "Awakened Draconium (End dragon kill)",
        "Energy Core tiers (1–8)",
        "Draconic armor & weapons",
        "Chaos Guardian fight & Chaos Shards",
        "Chaos Island & infinite power",
    ],
    "ender io": [
        "Alloy Smelter: basic alloys (Electrical Steel, Energetic Alloy, Redstone Alloy)",
        "SAG Mill & Slice'N'Splice",
        "Conduits: Item, Fluid, Energy, Redstone",
        "Farming Station, Capacitor Bank",
        "Enderium & Dark Steel processing",
        "Soul Binder & Powders",
    ],
    "botania": [
        "Mana generation: Daybloom, Endoflame, Gourmaryllis",
        "Mana Pool & Spreader",
        "Runic Altar crafting",
        "Terrasteel & Terra Blade",
        "Alfheim portal & Elven trade",
        "Gaia Guardian I & II",
    ],
    "tinkers construct": [
        "Smeltery setup: smelting ores → liquid metal",
        "Tool parts: Tool Forge, Part Builder",
        "Material modifiers & Reinforcements",
        "Slime Island materials",
        "Rapier, Cleaver, Crossbow",
    ],
    "mekanism": [
        "Basic Machines: Crusher, Enrichment Chamber, Metallurgic Infuser",
        "Gas processing: Electrolytic Separator, Chemical Injector",
        "Ore quintupling (Chemical Dissolution → Chemical Washer → Chemical Crystallizer → Chemical Injector → Energized Smelter)",
        "Fission/Fusion Reactor",
        "Mekasuit & modules",
    ],
    "create": [
        "Mechanical components: Shafts, Gearboxes, Cogs",
        "Kinetic generation: Windmill, Water Wheel, Hand Crank",
        "Contraptions: Mechanical Piston, Bearing, Rope Pulley",
        "Processing: Millstone, Fan, Depot",
        "Train system: Bogey, Train Track, Signal",
    ],
    "blood magic": [
        "Sacrificial Knife & Blood Altar Tier 1",
        "Blank Slates → Reinforced Slates",
        "Altar tiers 2-6 (Runes, Speed, Sacrifice)",
        "Sigils: Air, Water, Lava, Fast Miner",
        "Demon Will & Tartaric Gems",
        "Living Armor & Sentient Sword",
    ],
    "extra utilities": [
        "Generators: Furnace, Heated, Survivalist",
        "Pipes & Transfer Nodes",
        "Mid-tier generators: Slime, Death, Ender",
        "Quantum Quarry, Mining Well",
        "Cursed/Blessed Earth",
    ],
    "astral sorcery": [
        "Resonating Wand, Sooty Marble",
        "Attunement Altar tiers 1-2",
        "Discidia / Vicio / Aevitas / Armara constellations",
        "Lightwell, Spectral Relay",
        "Iridescent Altar & Starlight Crafting",
    ],
    "embers": [
        "Copper picks & Hammer + Bore",
        "Mixer, Stamper, Melter, Mixer Centrifuge",
        "Ember Activator pipelines",
        "Caminite & Aspectus",
        "Wildfire Core",
    ],
    "actually additions": [
        "Crusher, Compost, Empowerer basics",
        "Coal Generator → Solar/Heat generators",
        "Atomic Reconstructor & Lenses",
        "Phantom storage network",
        "Battery Box, Worm Farm",
    ],
    "immersive engineering": [
        "Hammer + Casting basics: Treated Wood, Steel",
        "Power: Kinetic Dynamo (Watermill, Windmill), Thermoelectric Generator",
        "Multiblocks: Coke Oven, Blast Furnace, Metal Press, Crusher",
        "Refining: Distillation Tower, Squeezer, Fermenter",
        "Revolver upgrades, Railgun, Chemical Thrower",
    ],
    "rftools": [
        "Machine Frame, Crafter Tier 1-3",
        "Endergenic Generator setup",
        "Storage Modules & Modular Storage",
        "Dimension Builder & Dimlets",
        "Builder, Shield Projector, Spawner",
    ],
}


def get_offline_stages(mod_name: str) -> list[str]:
    """Return curated progression stages for a mod, or empty list."""
    key = mod_name.lower().strip()
    # Exact substring match
    for k, stages in KNOWN_MOD_STAGES.items():
        if k in key or key in k:
            return stages
    # Word-level fuzzy match
    target_words = set(re.findall(r"[a-z]+", key))
    best: tuple[float, list[str]] = (0.0, [])
    for k, stages in KNOWN_MOD_STAGES.items():
        kw = set(re.findall(r"[a-z]+", k))
        if not kw or not target_words:
            continue
        score = len(kw & target_words) / max(len(kw), len(target_words))
        if score > best[0]:
            best = (score, stages)
    return best[1] if best[0] >= 0.5 else []
