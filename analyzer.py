"""
Mod analyzer: takes a list of mod names (or a directory of .jar files) and
scores each one by how well it suits automatic quest-chain generation.

Scoring criteria (higher = better recommendation):
  +50  Has a hand-curated KNOWN_MOD_STAGES entry
  +30  Mod is found on Modrinth at all
  +15  Modrinth mod has > 100k downloads (proven popularity)
  +10  Modrinth mod has > 10k downloads
  +20  Mod category is "technology" or "magic" (well-structured tech-trees)
  +10  Mod category is "adventure", "transportation", "storage"
  +15  Has 1.12.2 in its supported versions
  +10  Has a description > 100 chars
   -5  Mod name looks like a library (-API, -lib, -core suffix)

Stars (★ ★ ★) shown next to recommended mods based on bucketed score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import scraper


@dataclass
class ModAnalysis:
    name: str
    modid: str = ""
    score: int = 0
    downloads: int = 0
    categories: list[str] = None
    description: str = ""
    has_curated: bool = False
    has_1_12_2: bool = False
    on_modrinth: bool = False
    reason: list[str] = None

    def __post_init__(self):
        if self.categories is None:
            self.categories = []
        if self.reason is None:
            self.reason = []

    @property
    def stars(self) -> str:
        if self.score >= 80:
            return "★★★"
        if self.score >= 50:
            return "★★ "
        if self.score >= 25:
            return "★  "
        return "   "


_LIB_HINTS = ("api", "lib", "core", "loader", "config", "framework", "compat", "tweaks")
_TECH_KEYWORDS = ("technology", "tech", "magic", "automation", "energy", "industrial")
_GENERAL_KEYWORDS = ("adventure", "exploration", "storage", "transportation", "food")


def _looks_like_library(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(f" {h}") or low.endswith(f"-{h}") or low.endswith(h)
               for h in _LIB_HINTS)


def _score_mod(name: str, mod_hit: Optional[dict], curated_stages: list[str]) -> ModAnalysis:
    res = ModAnalysis(name=name)

    if curated_stages:
        res.has_curated = True
        res.score += 50
        res.reason.append("curated progression hints")

    if mod_hit:
        res.on_modrinth = True
        res.score += 30
        res.modid = mod_hit.get("slug", "") or ""
        res.downloads = int(mod_hit.get("downloads") or 0)
        res.categories = mod_hit.get("categories", []) or []
        res.description = mod_hit.get("description", "") or ""

        if res.downloads > 100_000:
            res.score += 15
            res.reason.append(f"{res.downloads // 1000}k downloads")
        elif res.downloads > 10_000:
            res.score += 10
            res.reason.append(f"{res.downloads // 1000}k downloads")
        elif res.downloads > 1_000:
            res.score += 5

        cats = " ".join(res.categories).lower()
        if any(kw in cats for kw in _TECH_KEYWORDS):
            res.score += 20
            res.reason.append("tech/magic mod")
        elif any(kw in cats for kw in _GENERAL_KEYWORDS):
            res.score += 10

        versions = mod_hit.get("versions") or []
        if "1.12.2" in versions or "1.12.2" in " ".join(
            mod_hit.get("game_versions", []) or []
        ):
            res.has_1_12_2 = True
            res.score += 15
            res.reason.append("supports 1.12.2")

        if len(res.description) > 100:
            res.score += 10

    if _looks_like_library(name):
        res.score -= 25
        res.reason.append("looks like a library/API")

    return res


def analyze_mod(name: str) -> ModAnalysis:
    name = name.strip()
    if not name:
        return ModAnalysis(name=name, score=-100)
    hit = scraper.search_modrinth(name, game_version="1.12.2")
    if hit is None:
        hit = scraper.search_modrinth(name)
    curated = scraper.get_offline_stages(name)
    return _score_mod(name, hit, curated)


def analyze_mods(names: list[str], progress: bool = True) -> list[ModAnalysis]:
    out: list[ModAnalysis] = []
    for i, name in enumerate(names, 1):
        if progress:
            print(f"  [{i}/{len(names)}] {name}", flush=True)
        out.append(analyze_mod(name))
    return out


_JAR_RE = re.compile(r"^(?P<name>.+?)[-_]?(?:mc)?[-_]?\d.*\.jar$", re.IGNORECASE)


def discover_mods_from_directory(path: Path) -> list[str]:
    """Find mod names from a directory of `.jar` files."""
    if not path.exists() or not path.is_dir():
        return []
    names: list[str] = []
    for jar in sorted(path.glob("*.jar")):
        stem = jar.stem
        m = _JAR_RE.match(jar.name)
        if m:
            stem = m.group("name")
        stem = stem.replace("_", " ").replace("-", " ").strip()
        # Title case
        words = [w.capitalize() if not w.isupper() else w for w in stem.split()]
        guess = " ".join(words)
        if guess:
            names.append(guess)
    return names


def print_analysis_table(results: list[ModAnalysis], top: Optional[int] = None) -> None:
    sorted_res = sorted(results, key=lambda r: (-r.score, r.name.lower()))
    if top:
        sorted_res = sorted_res[:top]
    name_w = max((len(r.name) for r in sorted_res), default=10)
    name_w = min(name_w, 40)
    print()
    print(f"  {'':3} {'Mod':<{name_w}} {'Score':>6} {'Downloads':>10}  Why")
    print(f"  {'':3} {'-'*name_w} {'-'*6} {'-'*10}  {'-'*40}")
    for r in sorted_res:
        reasons = ", ".join(r.reason[:4]) or "—"
        dl = f"{r.downloads:>10,}" if r.downloads else f"{'':>10}"
        print(f"  {r.stars} {r.name[:name_w]:<{name_w}} {r.score:>6} {dl}  {reasons}")
    print()


def interactive_select(results: list[ModAnalysis], default_top: int = 999) -> list[str]:
    """Show a checkbox-style selection prompt; returns the chosen mod names."""
    sorted_res = sorted(results, key=lambda r: (-r.score, r.name.lower()))
    checked = [r.score >= 25 for r in sorted_res]
    while True:
        print()
        print("  Select mods to generate quests for (sorted by score, ★ = recommended):")
        print()
        name_w = max((len(r.name) for r in sorted_res), default=10)
        name_w = min(name_w, 40)
        for i, r in enumerate(sorted_res, 1):
            mark = "x" if checked[i-1] else " "
            reasons = ", ".join(r.reason[:3]) or "—"
            print(f"   [{mark}] {i:>3}. {r.stars} {r.name[:name_w]:<{name_w}}  score={r.score:<4}  {reasons}")
        print()
        print("  Commands: <numbers> toggle | a=all | n=none | r=recommended | t=top10 | g=GO | q=quit")
        try:
            cmd = input("  > ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return []
        if not cmd:
            continue
        if cmd in ("q", "quit", "exit"):
            return []
        if cmd in ("g", "go", "ok", "done", ""):
            break
        if cmd in ("a", "all"):
            checked = [True] * len(sorted_res)
            continue
        if cmd in ("n", "none", "clear"):
            checked = [False] * len(sorted_res)
            continue
        if cmd in ("r", "rec", "recommended"):
            checked = [r.score >= 25 for r in sorted_res]
            continue
        if cmd in ("t", "top10"):
            checked = [i < 10 for i in range(len(sorted_res))]
            continue
        if re.match(r"^top\s*\d+$", cmd):
            n = int(re.search(r"\d+", cmd).group())
            checked = [i < n for i in range(len(sorted_res))]
            continue
        for token in re.split(r"[,\s]+", cmd):
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(sorted_res):
                    checked[idx] = not checked[idx]
            elif re.match(r"^\d+-\d+$", token):
                a, b = map(int, token.split("-"))
                for j in range(a-1, b):
                    if 0 <= j < len(sorted_res):
                        checked[j] = not checked[j]

    return [r.name for i, r in enumerate(sorted_res) if checked[i]]
