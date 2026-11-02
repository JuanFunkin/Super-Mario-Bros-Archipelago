from typing import Dict
from BaseClasses import Location

BASE_ID = 0xADD000 + 0x1000

WORLDS = range(8)   # 0-7 = World 1-8
LEVELS = range(4)   # 0-3 = level 1-4 of each world

MAX_COIN_CHECKS = 64     # matches CoinCheckCount's upper range bound
MAX_ONEUP_CHECKS = 15    # cap on "1-Up obtained in a level" checks
MAX_KILL_CHECKS = 64     # matches KillCheckCount's upper range bound


def level_location_name(world: int, level: int) -> str:
    return f"{world + 1}-{level + 1} Complete"


def no_hit_location_name(world: int, level: int) -> str:
    return f"{world + 1}-{level + 1} No-Hit Clear"


def build_location_table(coin_checks: bool, oneup_checks: bool,
                          kill_checks: bool = False, no_hit_checks: bool = False,
                          coin_check_count: int = MAX_COIN_CHECKS,
                          kill_check_count: int = MAX_KILL_CHECKS) -> Dict[str, int]:
    table: Dict[str, int] = {}
    code = BASE_ID

    for w in WORLDS:
        for l in LEVELS:
            table[level_location_name(w, l)] = code
            code += 1

    if coin_checks:
        for i in range(coin_check_count):
            table[f"Coin Milestone #{i + 1}"] = code
            code += 1

    if oneup_checks:
        for i in range(MAX_ONEUP_CHECKS):
            table[f"1-Up Get #{i + 1}"] = code
            code += 1

    if kill_checks:
        for i in range(kill_check_count):
            table[f"Stomp Kill Milestone #{i + 1}"] = code
            code += 1

    if no_hit_checks:
        for w in WORLDS:
            for l in LEVELS:
                table[no_hit_location_name(w, l)] = code
                code += 1

    return table


# "Full" table (every possible location) so Archipelago can display location
# names even for a seed that doesn't use all of them. Uses the max possible
# coin/kill check counts so every id that could ever be used is represented.
location_table: Dict[str, int] = build_location_table(
    True, True, True, True, MAX_COIN_CHECKS, MAX_KILL_CHECKS
)

LEVEL_TO_LOCATION = {
    (w, l): level_location_name(w, l) for w in WORLDS for l in LEVELS
}


class SMB1Location(Location):
    game = "Super Mario Bros."
