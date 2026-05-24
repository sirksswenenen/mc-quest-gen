#!/usr/bin/env python3
"""
MC Quest Generator
==================
Generates FTB Quests (1.12.2) quest chains for any list of Minecraft mods.
AI figures out what to craft and in what order; outputs a ready-to-use
config/ftbquests/quests.json.

Usage:
  python mc_quest_gen.py --setup                        # configure API keys
  python mc_quest_gen.py --test                         # test all providers
  python mc_quest_gen.py -m "Thermal Expansion" "IC2"  # generate quests
  python mc_quest_gen.py -m "Draconic Evolution" -o ./output --lang ru
  python mc_quest_gen.py --mods-file mods.txt           # read mods from file
"""

import argparse
import json
import sys
import time
import textwrap
from pathlib import Path

# Local modules
import providers
import scraper
import ftbquests


# ─────────────────────────────────────────────
# AI prompt builder
# ─────────────────────────────────────────────

SYSTEM_PROMPT_EN = """You are an expert Minecraft modpack creator for version 1.12.2.
Your task: generate a quest chain (progression guide) for a given mod.

Rules:
- Each quest = one crafting/progression milestone in the mod's tech tree.
- Start from the very beginning (basic materials) and go to end-game.
- 8–20 quests per mod, roughly sorted from easiest to hardest.
- Each quest MUST follow this EXACT format:

## Stage N: Quest Title Here
Item: modid:item_id_here
Description: One or two sentences explaining what this quest is about and why it matters in progression.
Depends on: Stage N-1 (or "none" for the first quest)

- For "Item:" use the real Minecraft item ID (lowercase, with modid prefix).
- If a quest requires multiple items, list them: Item: modid:item1, modid:item2
- Keep quest titles concise (max 8 words).
- Keep descriptions informative but short (1-3 sentences).
- Do NOT add rewards or any extra fields — just the format above.
- Do NOT add any intro/outro text outside the ## Stage blocks.
"""

SYSTEM_PROMPT_RU = """Ты — эксперт по созданию Minecraft-модпаков для версии 1.12.2.
Твоя задача: сгенерировать цепочку квестов (гайд по прогрессии) для указанного мода.

Правила:
- Каждый квест = один этап крафта/прогрессии в тех-дереве мода.
- Начни с самого начала (базовые материалы) и иди до эндгейма.
- 8–20 квестов на мод, от простого к сложному.
- Каждый квест ОБЯЗАТЕЛЬНО в таком формате:

## Этап N: Название квеста здесь
Item: modid:item_id_здесь
Description: Одно-два предложения — что нужно сделать и зачем это важно.
Depends on: Этап N-1 (или "none" для первого)

- В поле "Item:" используй реальный ID предмета Minecraft (строчные буквы, с префиксом мода).
- Если квест требует несколько предметов: Item: modid:item1, modid:item2
- Название квеста — не длиннее 8 слов.
- Описание — информативное, но короткое (1-3 предложения).
- НЕ добавляй награды и лишние поля.
- НЕ пиши ничего вне блоков ## Этап.
"""


def build_prompt(mod_info: dict, offline_stages: list[str], lang: str = "en") -> list[dict]:
    system = SYSTEM_PROMPT_RU if lang == "ru" else SYSTEM_PROMPT_EN
    mod_name = mod_info["name"]
    modid = mod_info.get("modid", mod_name.lower().replace(" ", ""))
    description = mod_info.get("description", "")
    wiki = mod_info.get("wiki_snippet", "")
    categories = ", ".join(mod_info.get("categories", [])) or "technology"

    # Build user message
    user_parts = [f"Mod name: {mod_name}"]
    if modid:
        user_parts.append(f"Mod ID (used for item IDs): {modid}")
    if categories:
        user_parts.append(f"Categories: {categories}")
    if description:
        user_parts.append(f"Description: {description[:400]}")
    if offline_stages:
        stages_text = "\n".join(f"  - {s}" for s in offline_stages)
        user_parts.append(f"Known progression stages:\n{stages_text}")
    if wiki:
        user_parts.append(f"Wiki info (excerpt):\n{wiki[:1200]}")

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
) -> ftbquests.make_chapter.__class__:
    """Fetch info, call AI, parse quests, return a chapter dict."""
    print(f"  📦 {mod_name}")

    # 1. Gather mod info
    print(f"     → Fetching mod info…", end="", flush=True)
    mod_info = scraper.get_mod_info(mod_name, game_version)
    offline_stages = scraper.get_offline_stages(mod_name)
    print(f" done ({mod_info['source']})")

    # 2. Build AI prompt & call
    print(f"     → Asking AI for quest chain…", end="", flush=True)
    messages = build_prompt(mod_info, offline_stages, lang)
    ai_response = providers.ai_call(messages, ai_cfg)
    print(f" done ({len(ai_response)} chars)")

    if verbose:
        print("\n--- AI raw response ---")
        print(ai_response[:1000], "…" if len(ai_response) > 1000 else "")
        print("--- end ---\n")

    # 3. Parse into quest objects
    modid = mod_info.get("modid", mod_name.lower().replace(" ", ""))
    quests = ftbquests.parse_ai_quests(ai_response, mod_name, modid)
    print(f"     → {len(quests)} quests generated")

    if not quests:
        print(f"     ⚠ No quests parsed — skipping {mod_name}")
        return None

    # 4. Pick chapter icon from first quest's first task
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
    p.add_argument("-m", "--mods", nargs="+", metavar="MOD", help="Mod names to generate quests for")
    p.add_argument("--mods-file", metavar="FILE", help="Text file with one mod name per line")
    p.add_argument("-o", "--output", default="./mc_quests_output", metavar="DIR",
                   help="Output directory (default: ./mc_quests_output)")
    p.add_argument("--game-version", default="1.12.2", metavar="VER",
                   help="Minecraft version (default: 1.12.2)")
    p.add_argument("--lang", default="en", choices=["en", "ru"],
                   help="Language for quest text (default: en)")
    p.add_argument("--verbose", "-v", action="store_true", help="Print AI raw responses")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ── Setup mode ──────────────────────────
    if args.setup:
        providers.interactive_setup()
        sys.exit(0)

    # ── Test mode ───────────────────────────
    if args.test:
        print("Testing AI providers…\n")
        cfg = providers.load_config()
        results = providers.test_providers(cfg)
        for name, status in results.items():
            icon = "✓" if status.startswith("ok") else ("—" if status == "no_key" else "✗")
            print(f"  {icon} {name:20s} {status}")
        sys.exit(0)

    # ── Collect mod list ────────────────────
    mods: list[str] = []
    if args.mods:
        mods.extend(args.mods)
    if args.mods_file:
        p = Path(args.mods_file)
        if not p.exists():
            print(f"Error: mods file not found: {p}", file=sys.stderr)
            sys.exit(1)
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                mods.append(line)

    if not mods:
        print("No mods specified. Use -m 'Mod Name' or --mods-file mods.txt")
        print("Run with --help for usage.")
        sys.exit(1)

    # ── Load provider config ─────────────────
    ai_cfg = providers.load_config()
    has_any_key = any(
        ai_cfg.get(p, {}).get("api_key") or ai_cfg.get(p, {}).get("api_token")
        for p in ["openrouter", "cloudflare", "google_gemini", "g4f_groq"]
    )
    if not has_any_key:
        print("⚠  No API keys configured. Run: python mc_quest_gen.py --setup")
        sys.exit(1)

    # ── Generate ─────────────────────────────
    output_dir = Path(args.output)
    print(f"\n🎮 MC Quest Generator")
    print(f"   Mods:    {len(mods)}")
    print(f"   Output:  {output_dir.resolve()}")
    print(f"   Version: {args.game_version}")
    print(f"   Lang:    {args.lang}\n")

    chapters: list[dict] = []
    failed: list[str] = []

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
        except RuntimeError as e:
            print(f"  ✗ {mod_name}: {e}")
            failed.append(mod_name)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            print(f"  ✗ {mod_name}: unexpected error: {e}")
            failed.append(mod_name)
        time.sleep(0.5)  # polite pause between mods

    if not chapters:
        print("\n✗ No chapters generated.")
        sys.exit(1)

    # ── Write output ─────────────────────────
    root = ftbquests.make_root_json(chapters)
    out_file = ftbquests.write_ftbquests_output(root, output_dir)

    # Summary
    total_quests = sum(len(c["quests"]) for c in chapters)
    print(f"\n{'='*50}")
    print(f"✓ Done! Generated {total_quests} quests across {len(chapters)} chapters.")
    print(f"  Output: {out_file}")
    print(f"\n  Install:")
    print(f"    Copy  {output_dir}/config/ftbquests/")
    print(f"    →  <minecraft_instance>/config/ftbquests/")
    print(f"\n  In-game: /ftbquests editing_mode  (to view/edit)")
    if failed:
        print(f"\n  ⚠ Failed mods: {', '.join(failed)}")


if __name__ == "__main__":
    main()
