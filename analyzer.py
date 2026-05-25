"""
Mod analyzer: takes a list of mod names (or a directory of .jar files) and
scores each one by how well it suits automatic quest-chain generation.

For .jar files, we read the real mod name from `mcmod.info` (1.12.2 format)
or `META-INF/mods.toml` (1.13+ Forge format) using stdlib `zipfile`, so the
analyzer doesn't have to guess from the filename.

Scoring criteria (higher = better recommendation):
  +50  Has a hand-curated KNOWN_MOD_STAGES entry
  +30  Mod is found on Modrinth at all
  +15  Modrinth mod has > 100k downloads (proven popularity)
  +10  Modrinth mod has > 10k downloads
  +20  Mod category is "technology" or "magic"
  +10  Mod category is "adventure", "transportation", "storage"
  +15  Has 1.12.2 in its supported versions
  +10  Has a description > 100 chars
   -50 Mod name/modid clearly identifies a library (-API, -lib, -core suffix,
       common library names)
   -30 Mod looks utility-only (utility/decoration/optimization/QoL/library)
       and has NO curated stages
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import scraper


@dataclass
class ModAnalysis:
    name: str
    modid: str = ""
    file: str = ""
    score: int = 0
    downloads: int = 0
    categories: list[str] = field(default_factory=list)
    description: str = ""
    has_curated: bool = False
    has_1_12_2: bool = False
    on_modrinth: bool = False
    is_library: bool = False
    reason: list[str] = field(default_factory=list)

    @property
    def stars(self) -> str:
        if self.score >= 80:
            return "★★★"
        if self.score >= 50:
            return "★★ "
        if self.score >= 25:
            return "★  "
        return "   "


# ─────────────────────────────────────────────
# .jar metadata reading
# ─────────────────────────────────────────────

_LIB_SUFFIXES = (
    "api", "lib", "core", "loader", "config",
    "framework", "compat", "tweaks", "ext", "utils",
)
_KNOWN_LIBS = {
    "codechickenlib", "cofhcore", "mcjtylib", "autoreglib",
    "brandonscore", "hammerlib", "mtlib", "soundreloader",
    "asmodeuscore", "endercore", "renderlib", "resourceloader",
    "sonarcore", "wanionlib", "stellarcore", "cyclopscore",
    "lunatriuscore", "stevekung_lib", "tesla_core_lib",
    "ftblib", "gunpowderlib", "loliasm", "vintagefix",
    "universaltweaks", "modtweaker", "moretweaker", "crafttweaker",
    "mantle", "chameleon", "libnine", "libraryex", "atlas_lib",
    "u_team_core", "xaerolib", "creativecore", "eventhelper",
    "redstoneflux", "mcmultipart", "mixinbooter", "configanytime",
    "fermiumbooter", "improved_relauncher", "forgelin", "mmmmmmmmmmmm",
    "stellarcore", "ftbutilities", "ftbquests",
}
_UTILITY_KEYWORDS = (
    "library", "utility", "optimization", "performance",
    "decoration", "minimap", "skin", "fps", "splash",
    "loading", "shader",
)


@dataclass
class JarInfo:
    name: str = ""
    modid: str = ""


def _read_mcmod_info(zf: zipfile.ZipFile) -> Optional[JarInfo]:
    """Read 1.12.2-style mcmod.info from a Forge mod jar."""
    if "mcmod.info" not in zf.namelist():
        return None
    try:
        raw = zf.read("mcmod.info").decode("utf-8", errors="replace")
    except Exception:
        return None
    raw = raw.strip()
    if not raw:
        return None
    raw = raw.lstrip("\ufeff")  # BOM
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # mcmod.info sometimes has trailing commas / loose JSON — be permissive
        cleaned = re.sub(r",(\s*[\]}])", r"\1", raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
    entries: list[dict] = []
    if isinstance(data, list):
        entries = [d for d in data if isinstance(d, dict)]
    elif isinstance(data, dict):
        ml = data.get("modList") or data.get("modlist") or []
        if isinstance(ml, list):
            entries = [d for d in ml if isinstance(d, dict)]
    for e in entries:
        name = e.get("name") or e.get("displayName") or ""
        modid = e.get("modid") or e.get("modId") or e.get("id") or ""
        if name or modid:
            return JarInfo(name=str(name).strip(), modid=str(modid).strip())
    return None


def _read_mods_toml(zf: zipfile.ZipFile) -> Optional[JarInfo]:
    """Read META-INF/mods.toml (1.13+) without a TOML parser."""
    if "META-INF/mods.toml" not in zf.namelist():
        return None
    try:
        raw = zf.read("META-INF/mods.toml").decode("utf-8", errors="replace")
    except Exception:
        return None
    raw = raw.lstrip("\ufeff")
    name = ""
    modid = ""
    in_mods = False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("[[mods]]"):
            in_mods = True
            continue
        if not in_mods:
            continue
        if s.startswith("[") and not s.startswith("[[mods]]"):
            break
        m_name = re.match(r'^displayName\s*=\s*"([^"]*)"', s)
        if m_name:
            name = m_name.group(1).strip()
            continue
        m_id = re.match(r'^modId\s*=\s*"([^"]*)"', s)
        if m_id:
            modid = m_id.group(1).strip()
            continue
        if name and modid:
            break
    if name or modid:
        return JarInfo(name=name, modid=modid)
    return None


def read_jar_metadata(jar_path: Path) -> Optional[JarInfo]:
    """Read display name + modid from a .jar file. Returns None on failure."""
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            info = _read_mcmod_info(zf)
            if info and (info.name or info.modid):
                return info
            info = _read_mods_toml(zf)
            if info and (info.name or info.modid):
                return info
    except (zipfile.BadZipFile, OSError):
        return None
    return None


_VERSION_CUT_RE = re.compile(
    r"[-_+.\s]+"
    r"(v?\d+\.\d|mc\d|"
    r"forge|fabric|universal|deobf|release|beta|alpha|snapshot|client|server|jar)",
    re.IGNORECASE,
)


def _name_from_filename(filename: str) -> str:
    """Best-effort guess of a mod's display name from its .jar filename."""
    base = Path(filename).stem
    base = re.sub(r"[\[\(].*?[\]\)]", " ", base)        # drop "[1.12.2]"-style tags
    m = _VERSION_CUT_RE.search(base)
    if m:
        base = base[: m.start()]
    parts = [p for p in re.split(r"[-_+.\s]+", base) if p]
    if not parts:
        return Path(filename).stem
    words = []
    for w in parts:
        if w.isupper() or (len(w) <= 4 and any(c.isdigit() for c in w)):
            words.append(w)
        elif any(c.isupper() for c in w[1:]):  # CamelCase already
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


# ─────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────

def _is_library_id(name: str, modid: str) -> bool:
    low_id = modid.lower().strip().replace("-", "_").replace(" ", "_")
    low_name = name.lower()
    if low_id in _KNOWN_LIBS:
        return True
    for suf in _LIB_SUFFIXES:
        if low_id.endswith(suf) or low_name.endswith(f" {suf}") or low_name.endswith(f"-{suf}"):
            return True
    if low_name.endswith("lib") or low_name.endswith("api") or low_name.endswith("core"):
        if len(low_name) > 4:
            return True
    return False


def _looks_utility(categories: list[str]) -> bool:
    cats = " ".join(categories).lower()
    return any(kw in cats for kw in _UTILITY_KEYWORDS)


def _score_mod(
    name: str,
    modid: str,
    mod_hit: Optional[dict],
    curated_stages: list[str],
) -> ModAnalysis:
    res = ModAnalysis(name=name, modid=modid)

    if curated_stages:
        res.has_curated = True
        res.score += 50
        res.reason.append("curated stages")

    if mod_hit:
        res.on_modrinth = True
        res.score += 30
        if not res.modid:
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

        cats_low = " ".join(res.categories).lower()
        if "technology" in cats_low or "magic" in cats_low:
            res.score += 20
            res.reason.append("tech/magic")
        elif any(kw in cats_low for kw in
                 ("adventure", "exploration", "storage", "transportation", "food")):
            res.score += 10

        versions = mod_hit.get("versions") or []
        gv = " ".join(mod_hit.get("game_versions", []) or [])
        if "1.12.2" in versions or "1.12.2" in gv:
            res.has_1_12_2 = True
            res.score += 15
            res.reason.append("1.12.2")

        if len(res.description) > 100:
            res.score += 10

        if _looks_utility(res.categories) and not res.has_curated:
            res.score -= 30
            res.reason.append("utility/lib category")

    if _is_library_id(name, modid):
        res.is_library = True
        if not res.has_curated:
            res.score -= 50
            res.reason.append("library/API")
        else:
            res.score -= 15
            res.reason.append("library-ish")

    return res


def analyze_mod(name: str, modid: str = "", file: str = "") -> ModAnalysis:
    name = name.strip()
    if not name and not modid:
        return ModAnalysis(name=name, score=-100, file=file)
    # Try CurseForge first (best 1.12.2 coverage); fall back to Modrinth.
    # Both calls are no-ops returning None when there's no match.
    hit = scraper.search_curseforge(name or modid, game_version="1.12.2", modid=modid)
    if hit is None:
        hit = scraper.search_modrinth(name or modid, game_version="1.12.2", modid=modid)
    if hit is None and modid and modid.lower() != (name or "").lower().replace(" ", ""):
        hit = scraper.search_modrinth(modid, game_version="1.12.2", modid=modid)
    curated = scraper.get_offline_stages(name or modid)
    if not curated and modid:
        curated = scraper.get_offline_stages(modid)
    res = _score_mod(name or modid, modid, hit, curated)
    res.file = file
    return res


def analyze_mods(items: list[tuple[str, str, str]], progress: bool = True) -> list[ModAnalysis]:
    """items: list of (display_name, modid, filename)"""
    out: list[ModAnalysis] = []
    for i, (name, modid, file) in enumerate(items, 1):
        if progress:
            shown = name or modid or file
            print(f"  [{i}/{len(items)}] {shown}", flush=True)
        out.append(analyze_mod(name, modid, file))
    return out


# ─────────────────────────────────────────────
# Mod discovery from a directory of .jar files
# ─────────────────────────────────────────────

def discover_mods_from_directory(path: Path) -> list[tuple[str, str, str]]:
    """Return list of (display_name, modid, filename) for each .jar in `path`."""
    if not path.exists() or not path.is_dir():
        return []
    out: list[tuple[str, str, str]] = []
    for jar in sorted(path.glob("*.jar")):
        info = read_jar_metadata(jar)
        if info and (info.name or info.modid):
            display = info.name or info.modid
            out.append((display, info.modid, jar.name))
        else:
            guessed = _name_from_filename(jar.name)
            out.append((guessed, "", jar.name))
    # Dedupe by lowercase display name
    seen = set()
    deduped: list[tuple[str, str, str]] = []
    for name, modid, file in out:
        key = (name or modid).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append((name, modid, file))
    return deduped


# ─────────────────────────────────────────────
# Pretty printing + selection UI
# ─────────────────────────────────────────────

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


def _default_checked(sorted_res: list[ModAnalysis], default_top: int = 10) -> list[bool]:
    """Pre-check strong recommendations only.

    Strategy: curated-and-not-library wins; if none, take top N with
    score >= 50 that are confirmed on Modrinth (filters out garbage names).
    Default top N is small on purpose so the user starts mostly empty
    and adds, rather than starting full and deleting.
    """
    checked = [False] * len(sorted_res)
    n_checked = 0
    for i, r in enumerate(sorted_res):
        if r.has_curated and not r.is_library:
            checked[i] = True
            n_checked += 1
    if n_checked == 0:
        for i, r in enumerate(sorted_res):
            if n_checked >= default_top:
                break
            if r.on_modrinth and r.score >= 50 and not r.is_library:
                checked[i] = True
                n_checked += 1
    if n_checked == 0:
        for i in range(min(default_top, len(sorted_res))):
            checked[i] = True
    return checked


def _print_menu_list(
    sorted_res: list[ModAnalysis],
    checked: list[bool],
    filter_pat: str = "",
) -> int:
    name_w = max((len(r.name) for r in sorted_res), default=10)
    name_w = min(name_w, 40)
    print()
    shown = 0
    selected = sum(1 for c in checked if c)
    print(f"  Mods: {len(sorted_res)} found, {selected} currently selected"
          + (f", filter: {filter_pat!r}" if filter_pat else "")
          + ":")
    print()
    for i, r in enumerate(sorted_res, 1):
        if filter_pat:
            haystack = f"{r.name} {r.modid} {' '.join(r.categories)}".lower()
            if filter_pat.lower() not in haystack:
                continue
        mark = "x" if checked[i-1] else " "
        reasons = ", ".join(r.reason[:3]) or "—"
        dl = f"{r.downloads:>7,}dl" if r.downloads else "         "
        print(f"   [{mark}] {i:>3}. {r.stars} {r.name[:name_w]:<{name_w}}  "
              f"s={r.score:<4} {dl}  {reasons}")
        shown += 1
    if not shown:
        print(f"   (no mods match filter {filter_pat!r})")
    return shown


def _parse_number_spec(spec: str, total: int) -> list[int]:
    """Parse '1-14,26,29 41 3-5' → list of 0-based indices."""
    out: set[int] = set()
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        if "-" in token and re.match(r"^\d+-\d+$", token):
            a, b = map(int, token.split("-"))
            for j in range(a-1, b):
                if 0 <= j < total:
                    out.add(j)
        elif token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < total:
                out.add(idx)
    return sorted(out)


_HELP = """\
  Commands:
    only 1-14,26,29       select EXACTLY these, deselect all others
    + 26 29 36            ADD these to selection
    - 5 6 7               REMOVE these from selection
    1-14,26,29            toggle these (existing behaviour)
    a / all               select all
    n / none              deselect all
    r / recommended       select only score >= 50
    t / top10             select only top 10 by score
    s>=80                 (re)select only mods with score >= N
    f tech                filter view to lines containing 'tech'
    f                     clear filter
    show / list           reprint the full list
    g / go                generate quests for the selected mods
    q / quit              cancel and exit
    h / help              show this help"""


def interactive_select(results: list[ModAnalysis], default_top: int = 10) -> list[str]:
    sorted_res = sorted(results, key=lambda r: (-r.score, r.name.lower()))
    checked = _default_checked(sorted_res, default_top)
    filter_pat = ""
    _print_menu_list(sorted_res, checked, filter_pat)
    print(_HELP)

    while True:
        try:
            cmd = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return []
        if not cmd:
            continue

        low = cmd.lower()

        if low in ("q", "quit", "exit"):
            return []
        if low in ("g", "go", "ok", "done"):
            break
        if low in ("h", "help", "?"):
            print(_HELP)
            continue
        if low in ("show", "list", "ls"):
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if low in ("a", "all"):
            checked = [True] * len(sorted_res)
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if low in ("n", "none", "clear"):
            checked = [False] * len(sorted_res)
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if low in ("r", "rec", "recommended"):
            checked = [r.score >= 50 for r in sorted_res]
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if low in ("t", "top10", "top"):
            checked = [i < 10 for i in range(len(sorted_res))]
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_top = re.match(r"^top\s*(\d+)$", low)
        if m_top:
            n = int(m_top.group(1))
            checked = [i < n for i in range(len(sorted_res))]
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_score = re.match(r"^s\s*([<>]=?)\s*(\d+)$", low)
        if m_score:
            op = m_score.group(1)
            n = int(m_score.group(2))
            for i, r in enumerate(sorted_res):
                val = r.score
                if op == ">=":
                    checked[i] = val >= n
                elif op == ">":
                    checked[i] = val > n
                elif op == "<=":
                    checked[i] = val <= n
                elif op == "<":
                    checked[i] = val < n
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if low == "f" or low == "filter":
            filter_pat = ""
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_f = re.match(r"^f(?:ilter)?\s+(.+)$", low)
        if m_f:
            filter_pat = m_f.group(1).strip()
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_only = re.match(r"^only\s+(.+)$", low)
        if m_only:
            idxs = _parse_number_spec(m_only.group(1), len(sorted_res))
            checked = [False] * len(sorted_res)
            for i in idxs:
                checked[i] = True
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_add = re.match(r"^\+\s*(.+)$|^add\s+(.+)$", low)
        if m_add:
            spec = m_add.group(1) or m_add.group(2)
            for i in _parse_number_spec(spec, len(sorted_res)):
                checked[i] = True
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        m_del = re.match(r"^-\s*(.+)$|^del\s+(.+)$|^remove\s+(.+)$|^rm\s+(.+)$", low)
        if m_del:
            spec = next(g for g in m_del.groups() if g is not None)
            for i in _parse_number_spec(spec, len(sorted_res)):
                checked[i] = False
            _print_menu_list(sorted_res, checked, filter_pat)
            continue
        if re.match(r"^[\d,\-\s]+$", cmd):
            for i in _parse_number_spec(cmd, len(sorted_res)):
                checked[i] = not checked[i]
            _print_menu_list(sorted_res, checked, filter_pat)
            continue

        print(f"  Unknown command: {cmd!r}. Type 'h' for help.")

    return [r.name for i, r in enumerate(sorted_res) if checked[i]]
