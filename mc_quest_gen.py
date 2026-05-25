#!/usr/bin/env python3
"""
MC Quest Generator
==================

Generates FTB Quests (Minecraft 1.12.2) quest chains for a list of mods,
using an AI provider chain to figure out the per-mod progression.

Outputs a ready-to-use `config/ftbquests/quests.json` — copy it into your
Minecraft instance and run `/ftbquests editing_mode` to view/edit.

A standalone `preview.html` file is also produced — open it in any browser
to see all chapters & quests rendered in an FTB-Quests-like UI (icons,
dependency lines, click-to-inspect).

Usage:
  python mc_quest_gen.py --setup                          # configure API keys
  python mc_quest_gen.py --test                           # test all providers
  python mc_quest_gen.py -m "Thermal Expansion" "IC2"     # generate quests
  python mc_quest_gen.py --analyze --mods-file mods.txt   # scoring + interactive
  python mc_quest_gen.py --append -m "Botania"            # add to existing config
  python mc_quest_gen.py --regenerate-mod "IC2"           # redo one chapter
  python mc_quest_gen.py --list-mods -o ./output          # list existing chapters
  python mc_quest_gen.py --html ./output/config/ftbquests/quests.json  # rerender HTML

Repo: https://github.com/sirksswenenen/mc-quest-gen
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from pathlib import Path

import analyzer
import ftbquests
import html_visualizer
import providers
import scraper


# ─────────────────────────────────────────────
# AI prompt builder
# ─────────────────────────────────────────────

SYSTEM_PROMPT_EN = """You are an expert Minecraft modpack creator for version 1.12.2.
Your task: generate a quest chain (progression cheat-sheet) for a given mod.

Rules:
- Each quest = ONE crafting / progression milestone in the mod's tech tree.
- Start from the very beginning (basic resources) and walk up to end-game.
- 8–18 quests per mod, roughly sorted from easiest to hardest.
- Each quest MUST follow this EXACT format:

## Stage N: Quest Title Here
Item: modid:item_id_here
Description: One or two sentences explaining what this stage is about
and why it matters in the progression.
Depends on: Stage N-1 (or "none" for the first quest)

- For "Item:" use a real Minecraft item ID (lowercase, with the modid prefix).
- If a quest requires multiple items, comma-separate them: Item: modid:item1, modid:item2
- Keep quest titles concise (≤ 8 words).
- Keep descriptions informative but short (1-3 sentences).
- Do NOT add rewards or any extra fields — JUST the format above.
- Do NOT add intro/outro text or markdown outside the ## Stage blocks.
- Do NOT use <think>...</think>; write the final answer directly.
"""

SYSTEM_PROMPT_RU = """Ты — эксперт по созданию Minecraft-модпаков для версии 1.12.2.
Твоя задача: сгенерировать цепочку квестов (шпаргалку по прогрессии) для указанного мода.

Правила:
- Каждый квест = ОДИН этап крафта / прогрессии в тех-дереве мода.
- Начни с самого начала (базовые ресурсы) и иди до эндгейма.
- 8–18 квестов на мод, от простого к сложному.
- Каждый квест СТРОГО в таком формате:

## Этап N: Название квеста
Item: modid:item_id
Description: Одно-два предложения — что нужно сделать и зачем это важно.
Depends on: Этап N-1 (или "none" для первого)

- В поле "Item:" — реальный ID предмета Minecraft (строчные буквы, с modid).
- Если квест требует несколько предметов: Item: modid:item1, modid:item2
- Название квеста — не длиннее 8 слов.
- Описание — короткое и информативное (1-3 предложения).
- НЕ добавляй награды, поля или комментарии вне блоков.
- НЕ добавляй текста до/после блоков ## Этап.
- НЕ используй <think>...</think> — пиши финальный ответ сразу.
"""


def build_prompt(mod_info: dict, offline_stages: list[str], lang: str = "en") -> list[dict]:
    system = SYSTEM_PROMPT_RU if lang == "ru" else SYSTEM_PROMPT_EN
    mod_name = mod_info["name"]
    modid = mod_info.get("modid") or mod_name.lower().replace(" ", "")
    description = mod_info.get("description", "")
    wiki = mod_info.get("wiki_snippet", "")
    categories = ", ".join(mod_info.get("categories", [])) or "technology"

    user_parts = [f"Mod name: {mod_name}", f"Mod ID (use this prefix in item IDs): {modid}"]
    if categories:
        user_parts.append(f"Categories: {categories}")
    if description:
        user_parts.append(f"Short description: {description[:400]}")
    if offline_stages:
        bullets = "\n".join(f"  - {s}" for s in offline_stages)
        user_parts.append(f"Known progression tiers (use these as a backbone):\n{bullets}")
    if wiki:
        user_parts.append(f"Wiki excerpt:\n{wiki[:1200]}")

    if lang == "ru":
        user_parts.append("\nСгенерируй полную цепочку квестов для этого мода.")
    else:
        user_parts.append("\nGenerate the complete quest chain for this mod.")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


# ─────────────────────────────────────────────
# Per-mod processing
# ─────────────────────────────────────────────

def process_mod(
    mod_name: str,
    ai_cfg: dict,
    game_version: str = "1.12.2",
    lang: str = "en",
    verbose: bool = False,
    modid_hint: str = "",
) -> dict | None:
    print(f"  - {mod_name}")

    print("     · fetching mod info…", end="", flush=True)
    mod_info = scraper.get_mod_info(mod_name, game_version, modid=modid_hint)
    offline_stages = scraper.get_offline_stages(mod_name)
    src = mod_info.get("source", "unknown")
    final_modid = mod_info.get("modid") or "?"
    if src == "unknown":
        print(f" not found online (modid={final_modid})")
    else:
        print(f" done (source={src}, modid={final_modid})")

    print("     · asking AI for quest chain…", end="", flush=True)
    messages = build_prompt(mod_info, offline_stages, lang)
    ai_response = providers.ai_call(messages, ai_cfg, verbose=verbose)
    print(f" done ({len(ai_response)} chars)")

    if verbose:
        print("\n--- AI raw response ---")
        print(ai_response[:1500] + ("…" if len(ai_response) > 1500 else ""))
        print("--- end ---\n")

    modid = mod_info.get("modid") or modid_hint or mod_name.lower().replace(" ", "")
    quests = ftbquests.parse_ai_quests(ai_response, mod_name, modid)
    print(f"     · {len(quests)} quests parsed")

    if not quests:
        print(f"     ! no quests parsed — skipping {mod_name}")
        return None

    icon = "minecraft:book"
    if quests and quests[0].get("tasks"):
        icon = quests[0]["tasks"][0].get("item", icon)

    chapter = ftbquests.make_chapter(
        title=mod_info["name"],
        quests=quests,
        icon_item=icon,
    )
    chapter["_modid"] = modid
    chapter["_mod_source_name"] = mod_name
    return chapter


# ─────────────────────────────────────────────
# Quest file IO + diff
# ─────────────────────────────────────────────

def quests_json_path(output_dir: Path) -> Path:
    return output_dir / "config" / "ftbquests" / "quests.json"


def preview_html_path(output_dir: Path) -> Path:
    return output_dir / "preview.html"


def load_existing(output_dir: Path) -> dict | None:
    p = quests_json_path(output_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def existing_mods(root: dict) -> list[str]:
    """Return the list of (display-name) mods already in this config."""
    names: list[str] = []
    for ch in root.get("chapters", []):
        name = ch.get("_mod_source_name") or ch.get("title") or ""
        if name:
            names.append(name)
    return names


def find_chapter_for_mod(root: dict, mod_name: str) -> int:
    target = mod_name.strip().lower()
    for i, ch in enumerate(root.get("chapters", [])):
        src = (ch.get("_mod_source_name") or "").lower()
        title = (ch.get("title") or "").lower()
        if src == target or title == target:
            return i
    return -1


def write_outputs(root: dict, output_dir: Path) -> tuple[Path, Path]:
    out_file = ftbquests.write_ftbquests_output(root, output_dir)
    html_out = preview_html_path(output_dir)
    html_visualizer.render_html(root, html_out, title="MC Quest Preview")
    return out_file, html_out


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate FTB Quests (1.12.2) quest chains using AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python mc_quest_gen.py --setup
          python mc_quest_gen.py --test
          python mc_quest_gen.py -m "Thermal Expansion" "IC2" "Applied Energistics 2"
          python mc_quest_gen.py --mods-file my_mods.txt -o ./output --lang ru
          python mc_quest_gen.py --analyze --mods-file my_mods.txt
          python mc_quest_gen.py --append -m "Botania" -o ./output
          python mc_quest_gen.py --regenerate-mod "IC2" -o ./output
          python mc_quest_gen.py --html ./output/config/ftbquests/quests.json
          python mc_quest_gen.py --list-mods -o ./output
        """),
    )
    p.add_argument("--setup", action="store_true", help="Interactive API key setup wizard")
    p.add_argument("--test", action="store_true", help="Test all configured AI providers")
    p.add_argument("--analyze", action="store_true",
                   help="Score mods and let you pick interactively (combine with -m or --mods-file)")
    p.add_argument("--scan-dir", metavar="DIR",
                   help="Scan a folder of .jar files and analyze them")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="With --analyze: pick top-N automatically instead of asking")
    p.add_argument("--list-mods", action="store_true",
                   help="List mods already present in the output's quests.json")
    p.add_argument("--append", action="store_true",
                   help="Add new mods to existing quests.json without touching old chapters")
    p.add_argument("--regenerate-mod", metavar="MOD",
                   help="Regenerate the chapter for ONE specific mod, replacing the old one")
    p.add_argument("--html", metavar="QUESTS_JSON",
                   help="Render a preview.html for an existing quests.json (no AI calls)")
    p.add_argument("--html-out", metavar="FILE",
                   help="With --html: explicit output path (default: preview.html next to input)")
    p.add_argument("-m", "--mods", nargs="+", metavar="MOD", help="Mod names")
    p.add_argument("--mods-file", metavar="FILE", help="Text file, one mod per line, # comments")
    p.add_argument("-o", "--output", default="./mc_quests_output", metavar="DIR",
                   help="Output directory (default: ./mc_quests_output)")
    p.add_argument("--game-version", default="1.12.2", metavar="VER",
                   help="Minecraft version (default: 1.12.2)")
    p.add_argument("--lang", default="en", choices=["en", "ru"],
                   help="Quest language (default: en)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print AI raw responses + provider chain decisions")
    return p.parse_args()


def _print_test_results(results: dict) -> None:
    width = max(len(k) for k in results) if results else 12
    for name, status in results.items():
        if status.startswith("ok"):
            icon = "ok "
        elif status == "no_key":
            icon = "·  "
        elif status.startswith("rate_limited"):
            icon = "!  "
        else:
            icon = "x  "
        print(f"  {icon} {name:<{width}}  {status}")


def collect_mods(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """Returns (display_name, modid, filename) tuples.
    For -m / --mods-file entries, modid and filename are empty."""
    items: list[tuple[str, str, str]] = []
    if args.mods:
        for m in args.mods:
            items.append((m, "", ""))
    if args.mods_file:
        path = Path(args.mods_file)
        if not path.exists():
            print(f"Error: mods file not found: {path}", file=sys.stderr)
            sys.exit(1)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append((line, "", ""))
    if args.scan_dir:
        d = Path(args.scan_dir)
        discovered = analyzer.discover_mods_from_directory(d)
        if not discovered:
            print(f"Warning: no .jar files found in {d}", file=sys.stderr)
        items.extend(discovered)
    # Dedupe by lowercase display name preserving order
    seen = set()
    out: list[tuple[str, str, str]] = []
    for name, modid, file in items:
        key = (name or modid).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((name, modid, file))
    return out


def cmd_html_only(args: argparse.Namespace) -> int:
    src = Path(args.html)
    if not src.exists():
        print(f"Error: file not found: {src}", file=sys.stderr)
        return 1
    # Configure CF so mod icons resolve cleanly
    try:
        cfg = providers.load_config()
        scraper.configure_curseforge((cfg.get("curseforge") or {}).get("api_key", ""))
    except Exception:
        pass
    out = Path(args.html_out) if args.html_out else src.parent / "preview.html"
    result = html_visualizer.render_from_file(src, out)
    print(f"Wrote: {result}")
    return 0


def cmd_list_mods(args: argparse.Namespace) -> int:
    root = load_existing(Path(args.output))
    if not root:
        print(f"No quests.json found at {quests_json_path(Path(args.output))}")
        return 1
    names = existing_mods(root)
    if not names:
        print("No chapters found.")
        return 0
    print(f"\nMods in {quests_json_path(Path(args.output))}:")
    for n in names:
        ch_idx = find_chapter_for_mod(root, n)
        ch = root["chapters"][ch_idx]
        print(f"  - {n}  ({len(ch.get('quests', []))} quests)")
    return 0


def cmd_analyze(args: argparse.Namespace, items: list[tuple[str, str, str]]) -> list[str]:
    if not items:
        print("Error: --analyze needs a mod list (use -m, --mods-file, or --scan-dir).",
              file=sys.stderr)
        sys.exit(1)
    # Configure CurseForge here too — analysis happens BEFORE the main
    # configure step, and CF lookups dramatically improve match accuracy
    cfg = providers.load_config()
    cf_key = (cfg.get("curseforge") or {}).get("api_key", "")
    scraper.configure_curseforge(cf_key)
    if cf_key:
        print("  (using CurseForge for mod metadata)")
    else:
        print("  (CurseForge key not configured — using Modrinth only; "
              "many 1.12.2 mods won't be found. See README for CF setup.)")
    print(f"\nAnalyzing {len(items)} mod(s)…")
    results = analyzer.analyze_mods(items)
    if args.top is not None:
        sorted_res = sorted(results, key=lambda r: (-r.score, r.name.lower()))
        chosen = [r.name for r in sorted_res[: args.top]]
        print(f"Auto-picked top {len(chosen)}: {', '.join(chosen)}")
        return chosen
    return analyzer.interactive_select(results)


def main() -> None:
    args = parse_args()

    if args.setup:
        providers.interactive_setup()
        sys.exit(0)

    if args.test:
        print("Testing AI providers…\n")
        cfg = providers.load_config()
        results = providers.test_providers(cfg)
        _print_test_results(results)
        ok = [n for n, s in results.items() if s.startswith("ok")]
        if ok:
            print(f"\n{len(ok)} provider(s) working: {', '.join(ok)}")
            sys.exit(0)
        else:
            print("\nNo provider works. Run --setup to fix keys.")
            sys.exit(2)

    if args.html:
        sys.exit(cmd_html_only(args))

    if args.list_mods:
        sys.exit(cmd_list_mods(args))

    output_dir = Path(args.output)
    existing_root = load_existing(output_dir)

    # Regenerate a single mod
    if args.regenerate_mod:
        if not existing_root:
            print(f"Error: no existing quests.json at {quests_json_path(output_dir)}",
                  file=sys.stderr)
            sys.exit(1)
        ai_cfg = providers.load_config()
        scraper.configure_curseforge((ai_cfg.get("curseforge") or {}).get("api_key", ""))
        mod_name = args.regenerate_mod
        idx = find_chapter_for_mod(existing_root, mod_name)
        # If we had the modid stored when we generated the original chapter,
        # reuse it to keep item IDs consistent.
        modid_hint = ""
        if idx >= 0:
            modid_hint = existing_root["chapters"][idx].get("_modid", "") or ""
        chapter = process_mod(mod_name, ai_cfg,
                              game_version=args.game_version,
                              lang=args.lang, verbose=args.verbose,
                              modid_hint=modid_hint)
        if not chapter:
            print(f"Failed to regenerate {mod_name}.", file=sys.stderr)
            sys.exit(1)
        if idx >= 0:
            existing_root["chapters"][idx] = chapter
            print(f"Replaced chapter for: {mod_name}")
        else:
            existing_root["chapters"].append(chapter)
            print(f"Appended new chapter for: {mod_name}")
        out_file, html_out = write_outputs(existing_root, output_dir)
        print(f"\nUpdated: {out_file}\nPreview: {html_out}")
        sys.exit(0)

    items = collect_mods(args)

    # Map display-name → modid (from jar metadata) so we can pass to process_mod
    modid_map: dict[str, str] = {name: modid for name, modid, _file in items if modid}

    if args.analyze:
        mods = cmd_analyze(args, items)
        if not mods:
            print("Nothing selected. Exiting.")
            sys.exit(0)
    else:
        mods = [name for name, _modid, _file in items]

    if not mods:
        print("No mods specified. Use -m 'Mod Name' or --mods-file mods.txt")
        print("Run with --help for usage.")
        sys.exit(1)

    ai_cfg = providers.load_config()
    has_any_key = any(
        ai_cfg.get(p, {}).get("api_key") or ai_cfg.get(p, {}).get("api_token")
        for p in providers.PROVIDERS
    )
    if not has_any_key:
        print("⚠  No API keys configured. Run: python mc_quest_gen.py --setup")
        sys.exit(1)

    # Tell scraper which CurseForge key to use (if any)
    cf_key = (ai_cfg.get("curseforge") or {}).get("api_key", "")
    scraper.configure_curseforge(cf_key)

    is_append = args.append or (existing_root is not None and not args.regenerate_mod)
    existing_names: list[str] = []
    if existing_root and is_append:
        existing_names = [n.lower() for n in existing_mods(existing_root)]
        before = len(mods)
        mods = [m for m in mods if m.lower() not in existing_names]
        if before != len(mods):
            print(f"  (skipping {before - len(mods)} mods already in {quests_json_path(output_dir)})")

    if not mods:
        if existing_root:
            print("\nAll requested mods are already in the config — nothing to do.")
            print("Use --regenerate-mod 'Mod Name' to redo one, or pass new mods.")
            sys.exit(0)
        print("No new mods to process.")
        sys.exit(0)

    print(f"\nMC Quest Generator")
    print(f"  mods    : {len(mods)} {'(appending)' if existing_root else ''}")
    print(f"  output  : {output_dir.resolve()}")
    print(f"  version : {args.game_version}")
    print(f"  lang    : {args.lang}\n")

    new_chapters: list[dict] = []
    failed: list[tuple[str, str]] = []

    for mod_name in mods:
        try:
            chapter = process_mod(
                mod_name,
                ai_cfg,
                game_version=args.game_version,
                lang=args.lang,
                verbose=args.verbose,
                modid_hint=modid_map.get(mod_name, ""),
            )
            if chapter:
                new_chapters.append(chapter)
            else:
                failed.append((mod_name, "no quests parsed"))
        except RuntimeError as e:
            short = str(e).splitlines()[0]
            print(f"  x {mod_name}: {short}")
            failed.append((mod_name, short))
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            print(f"  x {mod_name}: unexpected error: {type(e).__name__}: {e}")
            failed.append((mod_name, f"{type(e).__name__}: {e}"))
        time.sleep(0.4)

    if not new_chapters and not existing_root:
        print("\nx No chapters generated.")
        if failed:
            for name, err in failed:
                print(f"  - {name}: {err}")
        sys.exit(1)

    if existing_root:
        root = existing_root
        root["chapters"].extend(new_chapters)
    else:
        root = ftbquests.make_root_json(new_chapters)

    out_file, html_out = write_outputs(root, output_dir)

    total_quests = sum(len(c["quests"]) for c in root["chapters"])
    print(f"\n{'=' * 60}")
    if existing_root:
        print(f"Done. +{sum(len(c['quests']) for c in new_chapters)} new quests appended.")
        print(f"      Total: {total_quests} quests across {len(root['chapters'])} chapter(s).")
    else:
        print(f"Done. {total_quests} quests across {len(root['chapters'])} chapter(s).")
    print(f"Output : {out_file}")
    print(f"Preview: {html_out}  (open in any browser)")
    print()
    print("Install:")
    print(f"  copy  {output_dir}/config/ftbquests/")
    print("  to    <minecraft_instance>/config/ftbquests/")
    print()
    print("In-game: /ftbquests editing_mode")
    if failed:
        print()
        print("Failed mods:")
        for name, err in failed:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
