import os
from typing import Dict, Any, List, ClassVar
from BaseClasses import Item, Tutorial, ItemClassification
from worlds.AutoWorld import World, WebWorld
from .Items import item_table, create_item
from .Locations import location_table, build_location_table, WORLDS, LEVELS
from .Regions import create_regions
from .Options import SMB1Options
from .Shuffle import build_shuffle_order
from .Rom import SMB1Settings, SMB1ProcedurePatch
from . import client  # noqa: F401  needed so patch_suffix (.apsmb1) gets registered

world_version = (0, 2, 0)


class SMB1Web(WebWorld):
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up Super Mario Bros. for Archipelago (BizHawk).",
            "English",
            "setup_en.md",
            "setup/en",
            ["YourName"],
        )
    ]


class SMB1World(World):
    """
    Super Mario Bros. (NES). Each completed level (reaching the flag/castle)
    is a check. Optionally, collecting coins and 1-Ups also grants checks.
    Abilities (mushroom/fire flower) are unlocked by receiving items from
    the multiworld; if "Ability Gating" is enabled, picking up the power-up
    in a level without having unlocked the matching item shrinks you back
    down instantly.
    """
    game = "Super Mario Bros."
    web = SMB1Web()
    options_dataclass = SMB1Options
    options: SMB1Options
    settings: ClassVar[SMB1Settings]
    settings_key = "smb1_settings"

    item_name_to_id = {name: data.code for name, data in item_table.items()}
    # rebuilt in generate_early based on the player's options
    location_name_to_id: Dict[str, int] = dict(location_table)

    def generate_early(self) -> None:
        # NOTE: location_name_to_id intentionally stays as the fixed, full
        # `location_table` (every possible location, ids always in the same
        # order) regardless of this player's options. Regions.py and
        # client.py also use that same fixed table when creating/reporting
        # locations, so ids always agree everywhere. Previously this method
        # rebuilt a per-option, differently-numbered table here, which made
        # the ids the client sent not match what the server expected for
        # this slot -- checks looked like they went through (logged in the
        # client console) but never actually granted anything.

        self.level_shuffle_order = None
        if self.options.level_shuffle.value:
            self.level_shuffle_order = self._build_level_shuffle()

    def _build_level_shuffle(self) -> List[int]:
        # all the logic (and the reasons for its constraints) lives in
        # Shuffle.py, which is unit-tested against the real ROM's tables
        return build_shuffle_order(self.random)

    def create_regions(self) -> None:
        create_regions(self)

    def create_item(self, name: str) -> Item:
        return create_item(name, self.player)

    def create_items(self) -> None:
        pool: List[Item] = []

        # Ability items: only forced into the pool if their gating option is
        # on. If a given power-up's gating is off, Mario can use it freely
        # and that item doesn't need to exist.
        ability_names = []
        if self.options.mushroom_gating.value:
            ability_names.append("Progressive Mushroom")
        if self.options.fire_flower_gating.value:
            ability_names.append("Progressive Fire Flower")
        if self.options.star_gating.value:
            ability_names.append("Progressive Star")

        for name in ability_names:
            pool.append(self.create_item(name))

        # Auto-hint the ability items: they gate core gameplay, so the
        # player should know where they are from the start.
        self.options.start_hints.value.update(ability_names)

        # Number of locations actually created for this player (depends on
        # their options) -- NOT len(self.location_name_to_id), which is
        # intentionally the fixed full table (see generate_early).
        actual_location_count = len(build_location_table(
            self.options.coin_checks.value,
            self.options.oneup_checks.value,
            self.options.kill_checks.value,
            self.options.no_hit_checks.value,
            self.options.coin_check_count.value,
            self.options.kill_check_count.value,
        ))

        # Fill the rest with filler until it matches the number of locations
        filler_names = ["1-Up Mushroom", "Star Power", "10 Coins", "Shrink Trap", "Ice Trap", "Death Trap"]
        remaining = actual_location_count - len(pool)
        for i in range(remaining):
            pool.append(self.create_item(filler_names[i % len(filler_names)]))

        self.multiworld.itempool.extend(pool)

    def set_rules(self) -> None:
        goal_location = "8-4 Complete"
        self.multiworld.completion_condition[self.player] = lambda state: state.can_reach(
            goal_location, "Location", self.player
        )

    def generate_output(self, output_directory: str) -> None:
        patch = SMB1ProcedurePatch(player=self.player, player_name=self.multiworld.player_name[self.player])
        out_name = self.multiworld.get_out_file_name_base(self.player)
        patch.write(os.path.join(output_directory, f"{out_name}{patch.patch_file_ending}"))

    def fill_slot_data(self) -> Dict[str, Any]:
        return {
            "goal": self.options.goal.value,
            "coin_checks": self.options.coin_checks.value,
            "coin_check_interval": self.options.coin_check_interval.value,
            "coin_check_count": self.options.coin_check_count.value,
            "oneup_checks": self.options.oneup_checks.value,
            "kill_checks": self.options.kill_checks.value,
            "kill_check_interval": self.options.kill_check_interval.value,
            "kill_check_count": self.options.kill_check_count.value,
            "no_hit_checks": self.options.no_hit_checks.value,
            "mushroom_gating": self.options.mushroom_gating.value,
            "fire_flower_gating": self.options.fire_flower_gating.value,
            "star_gating": self.options.star_gating.value,
            "trap_link": self.options.trap_link.value,
            "level_shuffle": self.options.level_shuffle.value,
            "level_shuffle_order": self.level_shuffle_order,
            "warp_shuffle": self.options.warp_shuffle.value,
            "death_link": self.options.death_link.value,
        }
