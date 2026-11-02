from BaseClasses import Region
from .Locations import (
    SMB1Location, location_table, WORLDS, LEVELS, level_location_name,
    no_hit_location_name,
    MAX_COIN_CHECKS, MAX_ONEUP_CHECKS, MAX_KILL_CHECKS,
)


def create_regions(world) -> None:
    player = world.player
    multiworld = world.multiworld
    coin_checks = world.options.coin_checks.value
    coin_check_count = world.options.coin_check_count.value
    oneup_checks = world.options.oneup_checks.value
    kill_checks = world.options.kill_checks.value
    kill_check_count = world.options.kill_check_count.value
    no_hit_checks = world.options.no_hit_checks.value

    menu = Region("Menu", player, multiworld)
    multiworld.regions.append(menu)

    previous_region = menu
    level_regions = {}

    # SMB1 is linear within each world, and worlds are played in order
    # (warp zones are ignored for now). We chain the 32 locations one after
    # another: 1-1 -> 1-2 -> 1-3 -> 1-4 -> 2-1 -> ... -> 8-4
    for w in WORLDS:
        for l in LEVELS:
            name = level_location_name(w, l)
            region = Region(name, player, multiworld)
            loc = SMB1Location(player, name, location_table[name], region)
            region.locations.append(loc)
            multiworld.regions.append(region)
            level_regions[(w, l)] = region

            previous_region.connect(region, f"To {name}")
            previous_region = region

    # the last location generated is the goal: 8-4
    ordered_level_regions = list(level_regions.values())  # 32, in 1-1..8-4 order

    # Coin/1-Up/Kill locations: spread out one per level region (cycling
    # through all 32 in order) instead of dumping every single one on Menu.
    # Piling dozens (or hundreds) of always-accessible milestone checks on
    # Menu meant they were ALL reachable from the very first second of the
    # game -- one giant sphere 1 -- instead of unlocking gradually as you
    # actually progress.
    if coin_checks:
        for i in range(coin_check_count):
            name = f"Coin Milestone #{i + 1}"
            region = ordered_level_regions[i % len(ordered_level_regions)]
            loc = SMB1Location(player, name, location_table[name], region)
            region.locations.append(loc)

    if oneup_checks:
        for i in range(MAX_ONEUP_CHECKS):
            name = f"1-Up Get #{i + 1}"
            region = ordered_level_regions[i % len(ordered_level_regions)]
            loc = SMB1Location(player, name, location_table[name], region)
            region.locations.append(loc)

    if kill_checks:
        for i in range(kill_check_count):
            name = f"Stomp Kill Milestone #{i + 1}"
            region = ordered_level_regions[i % len(ordered_level_regions)]
            loc = SMB1Location(player, name, location_table[name], region)
            region.locations.append(loc)

    # No-Hit Clear locations: tied to the level itself (same access as the
    # level's own "Complete" location), since you can only clear it without
    # damage after actually reaching/finishing that level.
    if no_hit_checks:
        for w in WORLDS:
            for l in LEVELS:
                name = no_hit_location_name(w, l)
                region = level_regions[(w, l)]
                loc = SMB1Location(player, name, location_table[name], region)
                region.locations.append(loc)
