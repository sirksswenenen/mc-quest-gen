"""
Scraper module: fetches mod info and crafting progression data from the web.

Strategy:
  1. Modrinth API  – mod metadata (title, description, categories, modid)
  2. CurseForge search (no key) – fallback mod name resolution
  3. MediaWiki API (ftbwiki.org) – item/recipe pages as wikitext
  4. Fallback: return what we know so AI can fill the rest
"""

import json
import time
import re
import urllib.request
import urllib.parse
from typing import Optional

_HEADERS = {"User-Agent": "mc-quest-gen/1.0 (github.com/mc-quest-gen; contact@example.com)"}

# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> Optional[dict | list | str]:
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except Exception:
                return raw
    except Exception:
        return None


def _get_json(url: str, timeout: int = 15) -> Optional[dict | list]:
    result = _get(url, timeout)
    if isinstance(result, (dict, list)):
        return result
    return None


# ─────────────────────────────────────────────
# Modrinth API
# ─────────────────────────────────────────────

MODRINTH_BASE = "https://api.modrinth.com/v2"


def search_modrinth(mod_name: str, game_version: str = "1.12.2") -> Optional[dict]:
    """Search Modrinth for a mod and return the best match."""
    q = urllib.parse.quote(mod_name)
    facets = urllib.parse.quote(json.dumps([
        [f"game_versions:{game_version}"],
        ["project_type:mod"],
    ]))
    url = f"{MODRINTH_BASE}/search?query={q}&facets={facets}&limit=5"
    data = _get_json(url)
    if not data or not isinstance(data, dict):
        return None
    hits = data.get("hits", [])
    if not hits:
        return None
    # Pick closest match by name similarity
    target = mod_name.lower()
    hits_sorted = sorted(
        hits,
        key=lambda h: _similarity(h.get("title", "").lower(), target),
        reverse=True,
    )
    return hits_sorted[0]


def get_modrinth_project(project_id: str) -> Optional[dict]:
    url = f"{MODRINTH_BASE}/project/{project_id}"
    return _get_json(url)


def _similarity(a: str, b: str) -> float:
    """Cheap word-overlap similarity."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


# ─────────────────────────────────────────────
# FTB Wiki / MediaWiki scraper
# ─────────────────────────────────────────────

WIKI_API = "https://ftbwiki.org/api.php"


def _wiki_search(query: str, limit: int = 5) -> list[str]:
    """Search FTB wiki and return a list of page titles."""
    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "format": "json",
    })
    data = _get_json(f"{WIKI_API}?{params}")
    if not data or not isinstance(data, dict):
        return []
    return [r["title"] for r in data.get("query", {}).get("search", [])]


def _wiki_page_text(title: str) -> str:
    """Fetch wikitext of a page."""
    params = urllib.parse.urlencode({
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
    })
    data = _get_json(f"{WIKI_API}?{params}")
    if not data or not isinstance(data, dict):
        return ""
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        slots = page.get("revisions", [{}])[0].get("slots", {}).get("main", {})
        return slots.get("*", "")
    return ""


def _clean_wikitext(text: str) -> str:
    """Very rough wikitext → plain text strip."""
    # Remove templates and wiki markup to get readable text
    text = re.sub(r"\[\[File:[^\]]+\]\]", "", text)
    text = re.sub(r"\[\[([^|\]]+\|)?([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\{\{[^}]+\}\}", "", text)
    text = re.sub(r"'''?", "", text)
    text = re.sub(r"==+([^=]+)==+", r"\n## \1\n", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_mod_wiki_info(mod_name: str) -> str:
    """
    Try to fetch useful info about a mod from FTB wiki.
    Returns plain text snippet (may be empty).
    """
    titles = _wiki_search(mod_name, limit=3)
    for title in titles:
        if any(k in title.lower() for k in ["getting started", "guide", "tutorial", mod_name.lower().split()[0]]):
            text = _wiki_page_text(title)
            if text:
                cleaned = _clean_wikitext(text)
                # Return first 3000 chars — enough context for AI
                return cleaned[:3000]
    # Fallback: use first result
    if titles:
        text = _wiki_page_text(titles[0])
        if text:
            return _clean_wikitext(text)[:3000]
    return ""


# ─────────────────────────────────────────────
# Mod info aggregator
# ─────────────────────────────────────────────

def get_mod_info(mod_name: str, game_version: str = "1.12.2") -> dict:
    """
    Collect all available info about a mod.
    Returns a dict suitable for passing to the AI prompt.
    """
    info: dict = {
        "name": mod_name,
        "game_version": game_version,
        "description": "",
        "categories": [],
        "modid": "",
        "wiki_snippet": "",
        "source": "unknown",
    }

    # 1. Try Modrinth
    hit = search_modrinth(mod_name, game_version)
    if hit:
        info["name"] = hit.get("title", mod_name)
        info["description"] = hit.get("description", "")
        info["categories"] = hit.get("categories", [])
        info["modid"] = hit.get("slug", "")
        info["source"] = "modrinth"

    # 2. Try FTB wiki
    time.sleep(0.3)  # polite rate limit
    info["wiki_snippet"] = fetch_mod_wiki_info(mod_name)

    return info


# ─────────────────────────────────────────────
# Known mod progression data (offline fallback)
# ─────────────────────────────────────────────

# These are rough tech-tree stages for popular 1.12.2 mods.
# Used when web scraping fails or to supplement AI context.
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
        "Power: Dynamos (Steam, Compression, Magmatic…)",
        "Storage: Energy Cell tiers (Basic → Hardened → Reinforced → Signalum → Resonant)",
        "Augments & upgrades",
        "Satchels, Strongboxes, Cache",
        "Flux Networks",
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
        "Golem automation: Clay/Wood/Stone/Iron golems",
        "Infusion altar",
        "Vis & Aspects research",
        "Eldritch secrets",
    ],
    "draconic evolution": [
        "Draconium Ore & Dust",
        "Awakened Draconium (End dragon kill)",
        "Energy Core tiers (1–8)",
        "Draconic armor & weapons",
        "Chaos Guardian fight & Chaos Shards",
        "Chaos Island",
    ],
    "ender io": [
        "Alloy Smelter: basic alloys (Electrical Steel, Energetic Alloy…)",
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
        "Terra Blade & equipment",
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
        "Ore quintupling (Chemical Dissolution Chamber → Chemical Washer → Chemical Crystallizer)",
        "Fusion Reactor",
        "Mekasuit & modules",
    ],
    "create": [
        "Mechanical components: Shafts, Gearboxes, Cogs",
        "Kinetic generation: Windmill, Water Wheel, Hand Crank",
        "Contraptions: Mechanical Piston, Bearing, Rope Pulley",
        "Processing: Millstone, Fan, Depot",
        "Train system: Bogey, Train Track, Signal",
    ],
}


def get_offline_stages(mod_name: str) -> list[str]:
    """Return known progression stages for a mod, or empty list."""
    key = mod_name.lower()
    for k, stages in KNOWN_MOD_STAGES.items():
        if k in key or key in k:
            return stages
    return []
