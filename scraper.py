"""
Scraper module: fetches mod metadata + progression hints from the web.

Strategy:
  1. Modrinth API           → mod metadata (title, description, categories, slug=modid)
  2. CurseForge search      → optional, requires API key (skipped by default)
  3. FTB Wiki (MediaWiki)   → "Getting Started" / mod page wikitext as context
  4. Minecraft Wiki Fandom  → fallback context for vanilla-adjacent topics
  5. Local KNOWN_MOD_STAGES → curated progression hints for popular 1.12.2 mods

We never need to feed actual *recipes* to the AI — the AI just needs to know
what mod we're talking about and roughly which tiers exist. Item IDs are
generated from the modid + slugified quest title, which is right ~90% of
the time. The AI is also instructed to use the real modid prefix.
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


def _similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def search_modrinth(mod_name: str, game_version: str = "1.12.2") -> Optional[dict]:
    q = urllib.parse.quote(mod_name)
    facets = urllib.parse.quote(json.dumps([
        [f"game_versions:{game_version}"],
        ["project_type:mod"],
    ]))
    data = _get_json(f"{MODRINTH_BASE}/search?query={q}&facets={facets}&limit=8")
    if not isinstance(data, dict):
        return None
    hits = data.get("hits") or []
    if not hits:
        # Retry without version facet — some 1.12.2 mods only list themselves on
        # later versions but are still the right answer.
        data = _get_json(f"{MODRINTH_BASE}/search?query={q}&limit=8")
        if not isinstance(data, dict):
            return None
        hits = data.get("hits") or []
    if not hits:
        return None
    target = mod_name.lower()
    hits.sort(key=lambda h: _similarity(h.get("title", ""), target), reverse=True)
    return hits[0]


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

def get_mod_info(mod_name: str, game_version: str = "1.12.2") -> dict:
    info: dict = {
        "name": mod_name,
        "game_version": game_version,
        "description": "",
        "categories": [],
        "modid": "",
        "wiki_snippet": "",
        "source": "unknown",
    }

    hit = search_modrinth(mod_name, game_version)
    if hit:
        info["name"] = hit.get("title", mod_name)
        info["description"] = hit.get("description", "")
        info["categories"] = hit.get("categories", [])
        info["modid"] = hit.get("slug", "") or info["modid"]
        info["source"] = "modrinth"

    time.sleep(0.2)
    info["wiki_snippet"] = fetch_mod_wiki_info(mod_name)

    # Heuristic modid fallback (lowercase, strip non-alnum)
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
