#!/usr/bin/env python3
"""
MC Quest Generator
==================

Generates FTB Quests (Minecraft 1.12.2) quest chains for a list of mods,
using an AI provider chain to figure out the per-mod progression.

Outputs a ready-to-use `config/ftbquests/quests.json` — copy it into your
Minecraft instance and run `/ftbquests editing_mode` to view/edit.

Usage:
  python mc_quest_gen.py --setup                          # configure API keys
  python mc_quest_gen.py --test                           # test all providers
  python mc_quest_gen.py -m "Thermal Expansion" "IC2"     # generate quests
  python mc_quest_gen.py -m "Draconic Evolution" --lang ru
  python mc_quest_gen.py --mods-file mods.txt -o ./output

Repo: https://github.com/sirksswenenen/mc-quest-gen
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path

import ftbquests
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
) -> dict | None:
    print(f"  - {mod_name}")

    print("     · fetching mod info…", end="", flush=True)
    mod_info = scraper.get_mod_info(mod_name, game_version)
    offline_stages = scraper.get_offline_stages(mod_name)
    src = mod_info.get("source", "unknown")
    print(f" done (source={src}, modid={mod_info.get('modid') or '?'})")

    print("     · asking AI for quest chain…", end="", flush=True)
    messages = build_prompt(mod_info, offline_stages, lang)
    ai_response = providers.ai_call(messages, ai_cfg, verbose=verbose)
    print(f" done ({len(ai_response)} chars)")

    if verbose:
        print("\n--- AI raw response ---")
        print(ai_response[:1500] + ("…" if len(ai_response) > 1500 else ""))
        print("--- end ---\n")

    modid = mod_info.get("modid") or mod_name.lower().replace(" ", "")
    quests = ftbquests.parse_ai_quests(ai_response, mod_name, modid)
    print(f"     · {len(quests)} quests parsed")

    if not quests:
        print(f"     ! no quests parsed — skipping {mod_name}")
        return None

    icon = "minecraft:book"
    if quests and quests[0].get("tasks"):
        icon = quests[0]["tasks"][0].get("item", icon)

    return ftbquests.make_chapter(
        title=mod_info["name"],
        quests=quests,
        icon_item=icon,
    )


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
        """),
    )
    p.add_argument("--setup", action="store_true", help="Interactive API key setup wizard")
    p.add_argument("--test", action="store_true", help="Test all configured AI providers")
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

    mods: list[str] = []
    if args.mods:
        mods.extend(args.mods)
    if args.mods_file:
        path = Path(args.mods_file)
        if not path.exists():
            print(f"Error: mods file not found: {path}", file=sys.stderr)
            sys.exit(1)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                mods.append(line)

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

    output_dir = Path(args.output)
    print(f"\nMC Quest Generator")
    print(f"  mods    : {len(mods)}")
    print(f"  output  : {output_dir.resolve()}")
    print(f"  version : {args.game_version}")
    print(f"  lang    : {args.lang}\n")

    chapters: list[dict] = []
    failed: list[tuple[str, str]] = []

    for mod_name in mods:
        try:
            chapter = process_mod(
                mod_name,
                ai_cfg,
                game_version=args.game_version,
                lang=args.lang,
                verbose=args.verbose,
            )
            if chapter:
                chapters.append(chapter)
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

    if not chapters:
        print("\nx No chapters generated.")
        if failed:
            for name, err in failed:
                print(f"  - {name}: {err}")
        sys.exit(1)

    root = ftbquests.make_root_json(chapters)
    out_file = ftbquests.write_ftbquests_output(root, output_dir)

    total_quests = sum(len(c["quests"]) for c in chapters)
    print(f"\n{'=' * 60}")
    print(f"Done. {total_quests} quests across {len(chapters)} chapter(s).")
    print(f"Output: {out_file}")
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
