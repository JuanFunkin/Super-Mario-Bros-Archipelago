"""Pure logic for Level Shuffle / Warp Shuffle (no Archipelago imports, so it
can be unit-tested on its own).

How the game really picks the next level (verified byte-by-byte against the
USA ROM):

* The area that gets loaded is chosen by ``WorldNumber`` + ``AreaNumber``
  (``FindAreaPointer``), NOT by ``LevelNumber``. LevelNumber is only the
  "X-Y" shown on screen. AreaNumber counts *areas*, and some levels have an
  extra area (worlds 1, 2, 4 and 7 each have a short intro area before the
  real 2nd level), so AreaNumber and LevelNumber do not move in lockstep.
* Finishing a flagpole level (``RdyNextA``/``NextArea``) does:
      LevelNumber += 1 ; AreaNumber += 1 ; LoadAreaPointer
  WorldNumber is not touched.
* Finishing a castle (``PlayerEndWorld``) does:
      AreaNumber = 0 ; LevelNumber = 0 ; WorldNumber += 1 ; LoadAreaPointer
  but only if WorldNumber < 7 (``cpy #7 / bcs``, an UNSIGNED compare).

So to land on a chosen level we pre-write the values *one step behind* and
let the game's own increment do the rest.
"""
from typing import List, Optional, Tuple

FINAL_SLOT = 7 * 4 + 3  # 8-4, the real ending

# AreaNumber of the FIRST area of each level, per world (index 0-7).
# Worlds 1, 2, 4 and 7 have 5 areas (an intro area before level 2's real
# area), the others have 4. Read straight from the ROM's AreaAddrOffsets.
AREA_START = (
    (0, 1, 3, 4),  # World 1
    (0, 1, 3, 4),  # World 2
    (0, 1, 2, 3),  # World 3
    (0, 1, 3, 4),  # World 4
    (0, 1, 2, 3),  # World 5
    (0, 1, 2, 3),  # World 6
    (0, 1, 3, 4),  # World 7
    (0, 1, 2, 3),  # World 8
)

# AreaPointer value of area 0 of each world (first level of the world).
FIRST_AREA_ID = (0x25, 0x28, 0x24, 0x22, 0x2A, 0x2E, 0x33, 0x30)


def build_shuffle_order(rng) -> List[int]:
    """Random permutation of the 32 level slots (world*4+level).

    * It always STARTS at 1-1 (every new game starts there) and ENDS at 8-4
      (the real ending), so following it plays all 32 levels exactly once.
    * Every world-ending castle (slot w*4+3, except 8-4) must be followed by a
      world-start (w*4+0), because the game forces "level 1 of the next
      world" after a castle. World 1's start can never be that follower
      (reaching it would need WorldNumber = 255 before the game's unsigned
      ``>= 7`` check, which makes the game skip the advance) -- it is the
      first slot, so that holds automatically.
    """
    enders = [w * 4 + 3 for w in range(7)]
    other_starters = [w * 4 for w in range(1, 8)]
    middles = [w * 4 + l for w in range(8) for l in (1, 2)]

    rng.shuffle(other_starters)
    blocks = [[enders[i], other_starters[i]] for i in range(len(enders))]

    units = blocks + [[m] for m in middles]
    rng.shuffle(units)

    return [0] + [s for unit in units for s in unit] + [FINAL_SLOT]


def validate_shuffle_order(order) -> bool:
    if not isinstance(order, (list, tuple)) or len(order) != 32:
        return False
    if sorted(order) != list(range(32)) or order[0] != 0 or order[-1] != FINAL_SLOT:
        return False
    for i in range(31):
        w, l = divmod(order[i], 4)
        if l == 3:
            nw, nl = divmod(order[i + 1], 4)
            if nl != 0 or nw < 1:
                return False
    return True


def compute_redirect(world: int, level: int, order) -> Optional[Tuple[int, Optional[int], Optional[int]]]:
    """What to write so that clearing (world, level) leads to the next slot of
    the shuffle order.

    Returns ``(WorldNumber, LevelNumber, AreaNumber)`` byte values to write,
    where LevelNumber/AreaNumber are ``None`` when they must be left alone
    (castle: the game forces them to 0 itself). Returns ``None`` when no
    redirect should be done (invalid reading, the final castle, a malformed
    order, ...) -- in that case the game just advances normally.
    """
    if not (0 <= world <= 7 and 0 <= level <= 3):
        return None
    src = world * 4 + level
    if src == FINAL_SLOT or not isinstance(order, (list, tuple)):
        return None
    try:
        pos = order.index(src)
    except ValueError:
        return None
    if pos + 1 >= len(order):
        return None
    tw, tl = divmod(order[pos + 1], 4)
    if not (0 <= tw <= 7 and 0 <= tl <= 3):
        return None

    if level == 3:
        # castle: the game sets Area=0, Level=0 and does WorldNumber += 1
        if tl != 0 or tw < 1:
            return None
        return (tw - 1, None, None)

    # flagpole level: the game does LevelNumber += 1 and AreaNumber += 1
    return (tw, (tl - 1) & 0xFF, (AREA_START[tw][tl] - 1) & 0xFF)
