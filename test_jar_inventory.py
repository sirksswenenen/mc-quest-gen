"""Quick verification that jar_inventory parses synthetic IC2-shaped jars."""

import json
import tempfile
import zipfile
from pathlib import Path

import jar_inventory


def make_synthetic_ic2_jar(out_path: Path) -> None:
    """Build a tiny jar that looks like a real 1.12.2 mod (IC2-ish)."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mcmod.info
        zf.writestr("mcmod.info", json.dumps([{
            "modid": "ic2", "name": "IndustrialCraft 2",
            "version": "2.8.221-ex112",
        }]))

        # Recipe JSONs — only some IC2 recipes are JSON; most are Java.
        # Still, we should pick up these.
        zf.writestr("assets/ic2/recipes/macerator.json", json.dumps({
            "type": "minecraft:crafting_shaped",
            "pattern": ["FCF", "C C", "RMR"],
            "key": {"F": {"item": "minecraft:flint"}, "C": {"item": "ic2:plate"},
                    "R": {"item": "ic2:rubber"}, "M": {"item": "ic2:machine_block"}},
            "result": {"item": "ic2:macerator", "count": 1},
        }))
        zf.writestr("assets/ic2/recipes/iron_furnace.json", json.dumps({
            "type": "minecraft:crafting_shaped",
            "result": {"item": "ic2:iron_furnace", "count": 1},
        }))
        zf.writestr("assets/ic2/recipes/induction_furnace.json", json.dumps({
            "type": "minecraft:crafting_shaped",
            "result": {"item": "ic2:induction_furnace", "count": 1},
        }))
        zf.writestr("assets/ic2/recipes/mass_fabricator.json", json.dumps({
            "type": "minecraft:crafting_shaped",
            "result": {"item": "ic2:mass_fabricator", "count": 1},
        }))
        # alternative output shape
        zf.writestr("assets/ic2/recipes/uu_matter.json", json.dumps({
            "type": "ic2:replicator",
            "output": "ic2:uu_matter",
        }))

        # Lang file — real IC2 uses 'lang_ic2/en_us.properties' with quirky
        # 'te.X = Y' / 'cable.X = Y' prefixes, not the Forge convention.
        zf.writestr("assets/ic2/lang_ic2/en_us.properties", "\n".join([
            "# en_US translation",
            "te.macerator = Macerator",
            "te.iron_furnace = Iron Furnace",
            "te.induction_furnace = Induction Furnace",
            "te.mass_fabricator = Mass Fabricator",
            "te.electric_furnace = Electric Furnace",
            "te.compressor = Compressor",
            "te.extractor = Extractor",
            "te.recycler = Recycler",
            "te.nuclear_reactor = Nuclear Reactor",
            "te.coke_kiln = Coke Kiln",
            "te.batbox = BatBox",
            "te.chargepad_batbox = Charge Pad (BatBox)",
            "cable.copper_cable_0 = Copper Cable",
            "cable.copper_cable_1 = Insulated Copper Cable",
            "pipe.bronze_pipe_small = Small Bronze Pipe",
            "rubber = Rubber",
            "item.plate.iron = Iron Plate",
            # Junk we should reject:
            "ie.manual.category.energy.name = Power, wires, generators",
            "item.tooltip.power = Power",
            "subtitle.macerator = Macerator sound",
            "chat.something = Something",
            "advancement.foo.name = Some advancement",
        ]))

        # Blockstates — registry names live here too
        zf.writestr("assets/ic2/blockstates/macerator.json", json.dumps({}))
        zf.writestr("assets/ic2/blockstates/iron_furnace.json", json.dumps({}))
        zf.writestr("assets/ic2/blockstates/nuclear_reactor.json", json.dumps({}))
        zf.writestr("assets/ic2/blockstates/ore_copper.json", json.dumps({}))

        # Item model JSONs — these are the registry-name truth
        for name in ["macerator", "iron_furnace", "induction_furnace",
                     "mass_fabricator", "electric_furnace", "compressor",
                     "extractor", "recycler", "nuclear_reactor",
                     "coke_kiln"]:
            zf.writestr(f"assets/ic2/models/item/{name}.json", json.dumps({
                "parent": f"ic2:block/{name}",
            }))

        # A non-IC2 asset that should NOT be counted as IC2's
        zf.writestr("assets/minecraft/lang/en_us.lang", "ignored=yes")

        # IC2-style text recipe configs (this is the main reason real IC2
        # ships zero JSON recipes — most recipes live in these INIs).
        zf.writestr("assets/ic2/config/shaped_recipes.ini", "\n".join([
            "; shaped_recipes",
            "; <inputs> = <output>",
            "",
            '"PSP|SPS|PSP" P:OreDict:plateLead S:minecraft:stone@* = ic2:resource#reactor_vessel*4',
            '"PCP|BBB|PPP" P:OreDict:plankWood C:ic2:cable#type:tin,insulation:1 = ic2:te#batbox',
            '"CPC|RBR|" B:ic2:te#batbox = ic2:te#chargepad_batbox',
            '"FCF|C C|RMR" F:minecraft:flint M:ic2:resource#machine = ic2:te#macerator',
            # Cross-mod output — should NOT pollute IC2's recipe outputs
            '"   |UUU|   " U:ic2:misc_resource#matter = minecraft:cobblestone*64',
            # NBT-style variant — base should be added, complex variant skipped
            '"   |UUU|   " U:ic2:misc_resource#matter = ic2:cable#type:tin,insulation:1',
            # Recipe attribute after the spec must not break the parser
            '"VVV|VCV|VVV" V:ic2:resource#reactor_vessel = ic2:te#reactor_fluid_port @consuming',
        ]))
        zf.writestr("assets/ic2/config/shapeless_recipes.ini", "\n".join([
            "; shapeless_recipes",
            "OreDict:plateCopper OreDict:craftingToolWireCutter = ic2:cable#type:copper,insulation:0*2",
            "OreDict:plateBronze OreDict:craftingToolForgeHammer = ic2:casing#bronze*2",
        ]))


def make_synthetic_botania_jar(out_path: Path) -> None:
    """Synthetic Botania-ish jar with a Patchouli book inside."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mcmod.info", json.dumps([{
            "modid": "botania", "name": "Botania",
        }]))
        # patchouli book.json
        zf.writestr("assets/botania/patchouli_books/lexicon/book.json", json.dumps({
            "name": "Lexica Botania", "version": 87,
        }))
        # categories
        zf.writestr("assets/botania/patchouli_books/lexicon/en_us/categories/basics.json",
                    json.dumps({"name": "Basics of Botania", "icon": "botania:petal"}))
        zf.writestr("assets/botania/patchouli_books/lexicon/en_us/categories/devices.json",
                    json.dumps({"name": "Functional Flowers", "icon": "botania:mana_pool"}))
        # entries
        zf.writestr(
            "assets/botania/patchouli_books/lexicon/en_us/entries/basics/petals.json",
            json.dumps({
                "name": "Petal Crafting",
                "category": "botania:basics",
                "icon": "botania:petal",
                "sortnum": 10,
                "pages": [
                    {"type": "text", "text": "Collect mystical flowers and break them for petals."},
                    {"type": "crafting", "recipe": "botania:petal_apothecary",
                     "result": {"item": "botania:apothecary"}},
                ],
            }),
        )
        zf.writestr(
            "assets/botania/patchouli_books/lexicon/en_us/entries/devices/mana_pool.json",
            json.dumps({
                "name": "Mana Pool",
                "category": "botania:devices",
                "icon": "botania:mana_pool",
                "sortnum": 20,
                "pages": [
                    {"type": "text", "text": "Place mana pool and offer it to flowers."},
                    {"type": "spotlight", "item": "botania:mana_pool"},
                ],
            }),
        )


def test_ic2_inventory():
    with tempfile.TemporaryDirectory() as tmpdir:
        jar = Path(tmpdir) / "ic2.jar"
        make_synthetic_ic2_jar(jar)
        inv = jar_inventory.inspect_jar(jar, modid="ic2")
        assert inv.modid == "ic2", inv.modid
        # JSON recipes
        assert "ic2:macerator" in inv.recipe_outputs
        assert "ic2:iron_furnace" in inv.recipe_outputs
        assert "ic2:induction_furnace" in inv.recipe_outputs
        assert "ic2:mass_fabricator" in inv.recipe_outputs
        assert "ic2:uu_matter" in inv.recipe_outputs
        # IC2 INI recipes — base ids
        assert "ic2:te" in inv.recipe_outputs, sorted(inv.recipe_outputs)
        assert "ic2:resource" in inv.recipe_outputs
        assert "ic2:cable" in inv.recipe_outputs
        assert "ic2:casing" in inv.recipe_outputs
        # IC2 INI recipes — variant ids (only added when known via lang/models)
        assert "ic2:batbox" in inv.recipe_outputs
        assert "ic2:chargepad_batbox" in inv.recipe_outputs
        assert "ic2:reactor_vessel" not in inv.recipe_outputs, \
            "variant must be gated by known ids (lang doesn't define it)"
        # Cross-mod output (minecraft:cobblestone) must NOT pollute IC2
        assert "minecraft:cobblestone" not in inv.recipe_outputs
        # Counts are preserved from the INI '*N' suffix
        assert inv.recipe_outputs["ic2:resource"] >= 4
        # lang-derived items (IC2-style 'te.X = Y' prefixes feed back into the inventory)
        assert "ic2:macerator" in inv.item_display_names, sorted(inv.item_display_names)
        assert inv.item_display_names["ic2:macerator"] == "Macerator"
        assert inv.item_display_names.get("ic2:coke_kiln") == "Coke Kiln"
        assert inv.item_display_names.get("ic2:copper_cable_0") == "Copper Cable"
        # blockstates
        assert "ic2:macerator" in inv.block_ids
        assert "ic2:nuclear_reactor" in inv.block_ids
        # item models
        assert "ic2:macerator" in inv.item_model_ids
        assert "ic2:nuclear_reactor" in inv.item_model_ids
        # summary
        summary = jar_inventory.summarize_for_prompt(inv, max_items=200)
        joined = "\n".join(summary["inventory_lines"])
        assert "ic2:macerator" in joined
        assert "ic2:induction_furnace" in joined
        assert "ic2:mass_fabricator" in joined
        # critically, NO hallucinated 'ic2:copper_furnace'
        assert "copper_furnace" not in joined
        # JUNK we explicitly fed in must be REJECTED
        assert "ic2:energy" not in joined, "Patchouli/manual category leaked!"
        assert "ic2:power" not in joined, "tooltip key leaked!"
        assert "ic2:something" not in joined, "chat key leaked!"
        assert "ic2:foo" not in joined, "advancement key leaked!"
        # verify minecraft assets did NOT leak in
        assert "minecraft:" not in joined or "ignored" not in joined
        print(f"IC2: {summary['inventory_total']} items, sample:")
        for line in summary["inventory_lines"][:10]:
            print(line)
        print()


def test_botania_patchouli():
    with tempfile.TemporaryDirectory() as tmpdir:
        jar = Path(tmpdir) / "botania.jar"
        make_synthetic_botania_jar(jar)
        inv = jar_inventory.inspect_jar(jar, modid="botania")
        assert inv.modid == "botania"
        assert len(inv.patchouli_books) == 1
        book = inv.patchouli_books[0]
        assert book.title == "Lexica Botania"
        assert "Basics of Botania" in book.categories
        assert "Functional Flowers" in book.categories
        entry_names = [e.name for e in book.entries]
        assert "Petal Crafting" in entry_names
        assert "Mana Pool" in entry_names
        # item refs were collected from pages
        all_items = {it for e in book.entries for it in e.items}
        assert "botania:mana_pool" in all_items
        assert "botania:petal" in all_items or "botania:apothecary" in all_items
        summary = jar_inventory.summarize_for_prompt(inv, max_items=200)
        assert summary["patchouli_used"], summary
        outline = "\n".join(summary["patchouli_outline"])
        assert "Petal Crafting" in outline
        assert "Mana Pool" in outline
        print("Botania Patchouli outline:")
        print(outline)
        print()


def test_sniff_modid():
    """When modid is not given, _sniff_modid should still find it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jar = Path(tmpdir) / "ic2.jar"
        make_synthetic_ic2_jar(jar)
        inv = jar_inventory.inspect_jar(jar, modid="")
        assert inv.modid == "ic2", inv.modid


if __name__ == "__main__":
    test_ic2_inventory()
    test_botania_patchouli()
    test_sniff_modid()
    print("All tests passed.")
