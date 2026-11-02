import logging
import random
import time
from typing import TYPE_CHECKING, Any, Dict, List, Set

from NetUtils import ClientStatus
from worlds._bizhawk.client import BizHawkClient
from .Locations import LEVEL_TO_LOCATION, build_location_table
from .Shuffle import FIRST_AREA_ID, compute_redirect, validate_shuffle_order

if TYPE_CHECKING:
    from worlds._bizhawk.context import BizHawkClientContext

logger = logging.getLogger("Client")

# --- RAM addresses (SMBDIS.ASM, verified against the official disassembly) ---
WORLD_NUMBER = 0x075F
LEVEL_NUMBER = 0x075C
OPER_MODE = 0x0770
PLAYER_STATUS = 0x0756       # 0=small, 1=big, 2=fiery (controls fire/damage state)
PLAYER_SIZE = 0x0754         # 0=big, 1=small (controls the actual sprite/hitbox size)
NUMBER_OF_LIVES = 0x075A
COIN_TALLY = 0x075E          # coins in the current level (0-99, resets every level)
PLAYER_Y_POSITION = 0x00CE   # Mario's Y position on the current screen
PLAYER_X_SPEED = 0x0057      # signed byte, horizontal speed
PLAYER_X_POSITION = 0x0086   # on-screen X position (used for the Ice Trap freeze, more reliable than zeroing speed)
STAR_INVINCIBLE_TIMER = 0x079F
STOMP_CHAIN_COUNTER = 0x0484  # increments by 1 on every successful stomp kill, resets on landing
STAR_FLAG_TASK_CONTROL = 0x0746  # 1->5 while Mario walks into the castle after the flagpole; at 5 the game advances
WARP_ZONE_CONTROL = 0x06D6       # nonzero while standing in a warp zone room
CHANGE_AREA_TIMER = 0x06DE       # counts down after entering a pipe, before the area actually changes
DEMO_ACTION = 0x0717             # nonzero while the title screen's scripted attract-mode demo is playing
AREA_NUMBER = 0x0760             # index of the current area within the world (the REAL level selector, with WorldNumber)
AREA_POINTER = 0x0750            # area id the game loads; the warp pipe code writes it directly

# Real, cross-game Trap Link tag. Other games (OOT, Tunic, Elden Ring, etc.)
# also listen on this same tag, so a trap someone receives in this world can
# now actually reach them, and vice versa. We handle incoming data
# defensively (see on_package) since we can't control how other games'
# clients format their payload.
TRAP_LINK_TAG = "TrapLink"

TITLE_MODE_VALUE = 0
GAME_MODE_VALUE = 1
VICTORY_MODE_VALUE = 2
GAME_OVER_MODE_VALUE = 3

# World 8 (index 7) is the true end of the game (Bowser); we distinguish it
# from regular level clears using WorldNumber == 7 and LevelNumber == 3.
FINAL_WORLD = 7
FINAL_LEVEL = 3

ICE_TRAP_DURATION = 3.0       # seconds
STAR_POWER_FRAMES = 0xF0      # ~4 seconds of invincibility when the "Star Power" item is used

TRAP_ITEM_NAMES = {"Shrink Trap", "Ice Trap", "Death Trap"}

# Different games can call the same basic concept by different names (this
# is a known, expected part of how cross-game Trap Link works -- games are
# expected to recognize synonyms for what they can trigger locally, not just
# their own exact item names). This maps names we might see from OTHER
# games' Bounce data to the trap we should actually apply.
TRAP_NAME_ALIASES = {
    "Ice Trap": "Ice Trap",
    "Freeze Trap": "Ice Trap",
    "Frost Trap": "Ice Trap",
    "Frozen Trap": "Ice Trap",
    "Shrink Trap": "Shrink Trap",
    "Shrink": "Shrink Trap",
    "Small Trap": "Shrink Trap",
    "Death Trap": "Death Trap",
    "Instant Death Trap": "Death Trap",
    "OHKO Trap": "Death Trap",
}


def _signed(byte: int) -> int:
    return byte - 256 if byte >= 128 else byte


def _unsigned(value: int) -> int:
    return value & 0xFF


class SMB1Client(BizHawkClient):
    game = "Super Mario Bros."
    system = "NES"
    patch_suffix = ".apsmb1"

    items_received_count: int = 0

    # unlocked-ability state (sticky: once True, never reverts)
    mushroom_unlocked: bool = False
    fire_flower_unlocked: bool = False
    star_unlocked: bool = False

    # tracking for coin/1-up/kill checks
    total_coins_seen: int = 0
    last_coin_tally: int = 0
    last_lives: int = 0
    coin_checks_sent: int = 0
    oneup_checks_sent: int = 0
    granting_life_via_ap: bool = False  # avoids counting a life we injected ourselves as a "player 1-up"
    total_kills_seen: int = 0
    last_stomp_chain: int = 0
    kill_checks_sent: int = 0

    # no-hit tracking (per level attempt)
    took_damage_this_level: bool = False
    last_player_size: int = 1

    # --- end-of-level handling (level credit + Level Shuffle) ---
    # A level counts as cleared the moment its END sequence starts (flagpole
    # level: StarFlagTaskControl reaches 1-4 as Mario walks into the castle;
    # castle: OperMode switches to Victory when the axe is touched). We read
    # the level number at that instant and never compare "before/after"
    # level numbers any more -- our own Level Shuffle writes change those
    # numbers, and treating that change as a clear is what used to hand out
    # checks for levels nobody played.
    eol_handled: bool = True            # True = already handled (or not safe to handle); re-armed after 1s without the signal
    eol_clear_since: float = 0.0
    warp_handled: bool = True
    warp_clear_since: float = 0.0
    warned_no_skips_goal_failed: bool = False
    shuffle_order_cache = None          # validated copy of slot_data["level_shuffle_order"]
    shuffle_order_checked: bool = False

    # --- Death Link ---
    death_link: bool = False
    pending_death_link: bool = False    # a DeathLink came in from another player, we need to die
    sending_death_link: bool = False    # we already reported our own death, avoids duplicates

    # --- Trap Link ---
    trap_link: bool = False
    pending_traps: List[str] = None     # traps queued up to apply locally (from Trap Link or our own items)
    ice_trap_until: float = 0.0
    ice_trap_x: int = 0
    pending_death_trap: bool = False

    # Detects a BizHawk connection gap mid-session so we never trust a read that follows one.
    last_tick_time: float = 0.0
    # True = the next tick only re-baselines the delta trackers (lives, coins, ...) and does nothing else.
    need_resync: bool = True

    # Safety net: caps redirects in a short window in case something unforeseen makes them loop.
    recent_redirect_times: List[float] = None

    # Coin/lives/size RAM can read transient garbage while the game reloads memory for a new area.
    counters_safe_after: float = 0.0

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        from worlds._bizhawk import read, RequestFailedError
        try:
            # The "PRG ROM" domain is addressed from 0 (raw file offset), not
            # from 0x8000 (that's the CPU address, which only applies to the
            # "System Bus" domain).
            rom_check = await read(ctx.bizhawk_ctx, [(0x0, 4, "PRG ROM")])
        except RequestFailedError:
            return False

        # SEI ; CLD ; LDA #%00010000  ->  78 D8 A9 10  (original SMB1 USA)
        if bytes(rom_check[0]) != bytes.fromhex("78d8a910"):
            return False

        ctx.game = self.game
        ctx.items_handling = 0b001
        ctx.want_slot_data = True
        if self.pending_traps is None:
            self.pending_traps = []

        # Fresh connection: re-baseline all delta trackers, and treat any
        # end-of-level/warp sequence that might already be running as
        # handled (it re-arms by itself after 1 s without the signal).
        self.need_resync = True
        self.eol_handled = True
        self.warp_handled = True
        self.eol_clear_since = 0.0
        self.warp_clear_since = 0.0
        self.shuffle_order_checked = False
        self.counters_safe_after = time.time() + 2.0
        return True

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        # Don't override ctx.auth: at this point (before connecting) there
        # is no slot_data yet, so forcing a made-up name here just sends the
        # wrong slot name to the server ("InvalidSlot"). Let the normal
        # client flow (the name you type/configure) go through untouched.
        pass

    def on_package(self, ctx: "BizHawkClientContext", cmd: str, args: Dict[str, Any]) -> None:
        if cmd == "Connected":
            slot_data = args.get("slot_data", {}) or {}
            self.death_link = bool(slot_data.get("death_link", False))
            self.trap_link = bool(slot_data.get("trap_link", False))
        elif cmd == "Bounced":
            try:
                tags = args.get("tags", [])
                data = args.get("data", {}) or {}
                our_name = ctx.slot_info[ctx.slot].name if ctx.slot is not None else None

                if "DeathLink" in tags:
                    # ignore the echo of our own death
                    if data.get("source") != our_name:
                        self.on_deathlink(ctx)

                if TRAP_LINK_TAG in tags and self.trap_link:
                    # ignore the echo of our own trap
                    if data.get("source") != our_name:
                        # Different games format this differently -- accept
                        # a few common shapes defensively instead of
                        # assuming one exact schema (this is also why
                        # everything here is wrapped in try/except: a
                        # malformed payload from someone else's client
                        # should never be able to crash ours).
                        incoming_names = []
                        traps_field = data.get("traps")
                        if isinstance(traps_field, dict):
                            incoming_names.extend(traps_field.keys())
                        elif isinstance(traps_field, list):
                            for entry in traps_field:
                                if isinstance(entry, str):
                                    incoming_names.append(entry)
                                elif isinstance(entry, dict):
                                    for key in ("name", "trap", "item"):
                                        if isinstance(entry.get(key), str):
                                            incoming_names.append(entry[key])
                                            break
                        single_trap = data.get("trap")
                        if isinstance(single_trap, str):
                            incoming_names.append(single_trap)

                        for trap_name in incoming_names:
                            mapped_trap = TRAP_NAME_ALIASES.get(trap_name)
                            if mapped_trap:
                                if self.pending_traps is None:
                                    self.pending_traps = []
                                self.pending_traps.append(mapped_trap)
            except Exception:
                logger.debug("Ignored a malformed Bounced package", exc_info=True)

    def on_deathlink(self, ctx: "BizHawkClientContext") -> None:
        ctx.last_death_link = time.time()
        self.pending_death_link = True

    async def send_deathlink(self, ctx: "BizHawkClientContext") -> None:
        self.sending_death_link = True
        ctx.last_death_link = time.time()
        await ctx.send_death("Mario ran out of lives.")

    async def send_traplink(self, ctx: "BizHawkClientContext", trap_name: str) -> None:
        if not self.trap_link:
            return
        our_name = ctx.slot_info[ctx.slot].name if ctx.slot is not None else None
        await ctx.send_msgs([{
            "cmd": "Bounce",
            "tags": [TRAP_LINK_TAG],
            "data": {"time": time.time(), "source": our_name, "traps": {trap_name: 1}},
        }])

    def _refresh_unlocks(self, ctx: "BizHawkClientContext") -> None:
        """Recomputes which abilities are unlocked based on items received so
        far. Sticky on purpose: once True, never goes back to False, even if
        a later read of ctx.items_received were to somehow miss it -- once
        you've truly received a progression item, you should never lose the
        ability tied to it."""
        for item in ctx.items_received:
            name = ctx.item_names.lookup_in_game(item.item)
            if name == "Progressive Mushroom":
                self.mushroom_unlocked = True
            elif name == "Progressive Fire Flower":
                self.fire_flower_unlocked = True
            elif name == "Progressive Star":
                self.star_unlocked = True

    def _redirect_allowed(self) -> bool:
        """Safety net against a runaway redirect loop: allows at most 4
        Level/Warp Shuffle redirects in any 10-second window. Something
        looping this fast is never legitimate gameplay (even speedrunning
        every level back to back takes longer than that), so refusing to
        act past the cap just stops a bad loop from continuing to corrupt
        things further -- it doesn't cause one by itself."""
        if self.recent_redirect_times is None:
            self.recent_redirect_times = []
        now = time.time()
        self.recent_redirect_times = [t for t in self.recent_redirect_times if now - t < 10.0]
        if len(self.recent_redirect_times) >= 4:
            return False
        self.recent_redirect_times.append(now)
        return True

    def _apply_trap_writes(self, trap_name: str, player_status: int, x_position: int, writes: list) -> None:
        """Queues the RAM writes needed to apply a trap's effect right now."""
        if trap_name == "Shrink Trap":
            if player_status != 0:
                writes.append((PLAYER_STATUS, bytes([0]), "RAM"))
                writes.append((PLAYER_SIZE, bytes([1]), "RAM"))
        elif trap_name == "Ice Trap":
            self.ice_trap_until = time.time() + ICE_TRAP_DURATION
            self.ice_trap_x = x_position
            logger.debug("Ice Trap! Mario is frozen for a few seconds.")
        elif trap_name == "Death Trap":
            self.pending_death_trap = True
            logger.debug("Death Trap!")

    async def _credit_level(self, ctx: "BizHawkClientContext", pair, no_hit_enabled: bool) -> None:
        """Sends the 'Complete' (and, if earned, 'No-Hit Clear') check of a
        level that just ended. Called exactly once per end-of-level event."""
        from .Locations import location_table, no_hit_location_name

        ids = []
        name = LEVEL_TO_LOCATION.get(pair)
        if name:
            ids.append(location_table.get(name))
            if no_hit_enabled and not self.took_damage_this_level:
                ids.append(location_table.get(no_hit_location_name(*pair)))
        ids = [i for i in ids if i and i not in ctx.checked_locations]
        if ids:
            logger.debug(f"Level cleared {pair}: sending {ids}")
            await ctx.send_msgs([{"cmd": "LocationChecks", "locations": ids}])
        self.took_damage_this_level = False

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        from worlds._bizhawk import read, write
        from .Locations import location_table

        if self.pending_traps is None:
            self.pending_traps = []

        # A gap of more than 2 s between ticks means BizHawk dropped and came
        # back (or a big lag spike). The first read after that can't be
        # trusted as a baseline, so skip this tick and re-baseline the next.
        # (eol_handled / warp_handled are deliberately NOT reset here: if the
        # drop happened mid-sequence we must not handle it a second time with
        # values our own redirect already overwrote.)
        now = time.time()
        gap_detected = self.last_tick_time != 0.0 and (now - self.last_tick_time) > 2.0
        self.last_tick_time = now
        if gap_detected:
            self.need_resync = True
            self.counters_safe_after = now + 2.0
            self.recent_redirect_times = []
            return

        self._refresh_unlocks(ctx)

        slot_data = ctx.slot_data or {}
        goal = slot_data.get("goal", 0)
        mushroom_gating = slot_data.get("mushroom_gating", True)
        fire_flower_gating = slot_data.get("fire_flower_gating", True)
        star_gating = slot_data.get("star_gating", True)
        coin_checks_enabled = slot_data.get("coin_checks", False)
        coin_interval = max(1, slot_data.get("coin_check_interval", 50))
        coin_check_count = slot_data.get("coin_check_count", 16)
        oneup_checks_enabled = slot_data.get("oneup_checks", True)
        kill_checks_enabled = slot_data.get("kill_checks", False)
        kill_interval = max(1, slot_data.get("kill_check_interval", 5))
        kill_check_count = slot_data.get("kill_check_count", 16)
        no_hit_checks_enabled = slot_data.get("no_hit_checks", False)
        level_shuffle_enabled = slot_data.get("level_shuffle", False)
        warp_shuffle_enabled = slot_data.get("warp_shuffle", False)

        if not self.shuffle_order_checked and slot_data:
            order = slot_data.get("level_shuffle_order")
            self.shuffle_order_cache = list(order) if order and validate_shuffle_order(order) else None
            self.shuffle_order_checked = True

        reads = await read(ctx.bizhawk_ctx, [
            (WORLD_NUMBER, 1, "RAM"),
            (LEVEL_NUMBER, 1, "RAM"),
            (OPER_MODE, 1, "RAM"),
            (PLAYER_STATUS, 1, "RAM"),
            (PLAYER_SIZE, 1, "RAM"),
            (COIN_TALLY, 1, "RAM"),
            (NUMBER_OF_LIVES, 1, "RAM"),
            (STAR_INVINCIBLE_TIMER, 1, "RAM"),
            (PLAYER_X_POSITION, 1, "RAM"),
            (STOMP_CHAIN_COUNTER, 1, "RAM"),
            (STAR_FLAG_TASK_CONTROL, 1, "RAM"),
            (WARP_ZONE_CONTROL, 1, "RAM"),
            (CHANGE_AREA_TIMER, 1, "RAM"),
            (DEMO_ACTION, 1, "RAM"),
        ])
        (world_number, level_number, oper_mode, player_status, player_size, coin_tally,
         lives, star_timer, x_position, stomp_chain, star_flag_task,
         warp_zone_control, change_area_timer, demo_action) = (r[0] for r in reads)

        # The title screen's scripted demo runs on the real game engine
        # (OperMode reads as "game"), so DemoAction must be checked too.
        real_gameplay = oper_mode == GAME_MODE_VALUE and demo_action == 0
        valid_reading = 0 <= world_number <= 7 and 0 <= level_number <= 3
        counters_safe = now >= self.counters_safe_after
        writes = []

        # keep the client's "DeathLink" tag in sync with the YAML option
        await ctx.update_death_link(self.death_link)

        # Title screen, demo, or game over: nothing of ours applies. Drop
        # anything pending and re-baseline when real gameplay comes back, so
        # values that reset (lives, coins...) are never read as "changes".
        if oper_mode in (TITLE_MODE_VALUE, GAME_OVER_MODE_VALUE) or demo_action != 0:
            self.need_resync = True
            self.pending_death_link = False
            self.pending_death_trap = False
            if oper_mode == GAME_OVER_MODE_VALUE:
                self.counters_safe_after = now + 2.0
            return

        if self.need_resync:
            self.need_resync = False
            self.last_lives = lives
            self.last_coin_tally = coin_tally
            self.last_stomp_chain = stomp_chain
            self.last_player_size = player_size
            return

        # ---------- 0) Death Link ----------
        if "DeathLink" in ctx.tags:
            # received one: push Mario off the bottom of the screen, the game
            # then runs its normal fall-death
            if self.pending_death_link and real_gameplay:
                writes.append((PLAYER_Y_POSITION, bytes([0xF8]), "RAM"))
                self.pending_death_link = False
                self.sending_death_link = True  # don't echo our own forced death back out

            # we lost a life -> tell everyone
            if real_gameplay and lives < self.last_lives and not self.sending_death_link \
                    and ctx.last_death_link + 1 < now:
                await self.send_deathlink(ctx)
            elif lives >= self.last_lives:
                self.sending_death_link = False

        # any life lost during a level attempt breaks a "no-hit" clear
        if real_gameplay and lives < self.last_lives:
            self.took_damage_this_level = True

        # ---------- 1) End of level: credit it + Level Shuffle ----------
        flag_sequence = 1 <= star_flag_task <= 4     # walking into the castle after the flagpole
        castle_sequence = oper_mode == VICTORY_MODE_VALUE  # axe touched
        eol_signal = flag_sequence or castle_sequence
        warp_signal = warp_zone_control != 0 and change_area_timer > 0

        if eol_signal or warp_signal:
            # keep counters quiet for the whole sequence plus a cushion after
            self.counters_safe_after = now + 1.0
            counters_safe = False

        # re-arm each handler once its signal has been gone for a full second
        if eol_signal:
            self.eol_clear_since = 0.0
        elif self.eol_clear_since == 0.0:
            self.eol_clear_since = now
        elif now - self.eol_clear_since >= 1.0:
            self.eol_handled = False

        if warp_signal:
            self.warp_clear_since = 0.0
        elif self.warp_clear_since == 0.0:
            self.warp_clear_since = now
        elif now - self.warp_clear_since >= 1.0:
            self.warp_handled = False

        if eol_signal and not self.eol_handled and valid_reading:
            self.eol_handled = True
            await self._credit_level(ctx, (world_number, level_number), no_hit_checks_enabled)

            if level_shuffle_enabled and self.shuffle_order_cache and self._redirect_allowed():
                redirect = compute_redirect(world_number, level_number, self.shuffle_order_cache)
                if redirect is not None:
                    new_world, new_level, new_area = redirect
                    writes.append((WORLD_NUMBER, bytes([new_world]), "RAM"))
                    if new_level is not None:
                        writes.append((LEVEL_NUMBER, bytes([new_level]), "RAM"))
                    if new_area is not None:
                        writes.append((AREA_NUMBER, bytes([new_area]), "RAM"))
                    logger.debug(f"Level Shuffle: {world_number + 1}-{level_number + 1} -> {redirect}")

        # ---------- 1b) Warp Shuffle ----------
        # Pressing Down on a warp pipe makes the game set WorldNumber,
        # AreaPointer, AreaNumber and LevelNumber at once, then plays the
        # pipe animation (ChangeAreaTimer counts down ~0.8 s) before loading
        # the area from AreaPointer. We overwrite those during the animation.
        if warp_signal and not self.warp_handled:
            self.warp_handled = True
            if warp_shuffle_enabled and self._redirect_allowed():
                random_world = random.randrange(8)
                writes.append((WORLD_NUMBER, bytes([random_world]), "RAM"))
                writes.append((AREA_POINTER, bytes([FIRST_AREA_ID[random_world]]), "RAM"))
                writes.append((AREA_NUMBER, bytes([0]), "RAM"))
                writes.append((LEVEL_NUMBER, bytes([0]), "RAM"))
                logger.debug(f"Warp Shuffle: -> world {random_world + 1}")

        # ---------- 2) Ability gating ----------
        gating_forced_shrink_this_tick = False
        if real_gameplay:
            mushroom_ok = (not mushroom_gating) or self.mushroom_unlocked
            fire_flower_ok = (not fire_flower_gating) or self.fire_flower_unlocked

            if player_status == 1 and not mushroom_ok:
                writes.append((PLAYER_STATUS, bytes([0]), "RAM"))
                writes.append((PLAYER_SIZE, bytes([1]), "RAM"))  # 1 = small (this one changes the sprite/hitbox)
                gating_forced_shrink_this_tick = True
            elif player_status == 2 and not fire_flower_ok:
                # without the flower, at most stay big (small if mushrooms are locked too)
                if mushroom_ok:
                    writes.append((PLAYER_STATUS, bytes([1]), "RAM"))
                    writes.append((PLAYER_SIZE, bytes([0]), "RAM"))  # 0 = big
                else:
                    writes.append((PLAYER_STATUS, bytes([0]), "RAM"))
                    writes.append((PLAYER_SIZE, bytes([1]), "RAM"))
                    gating_forced_shrink_this_tick = True

            if star_gating and not self.star_unlocked and star_timer != 0:
                writes.append((STAR_INVINCIBLE_TIMER, bytes([0]), "RAM"))

        # ---------- 2.5) Damage tracking for No-Hit ----------
        # PLAYER_SIZE: 0 = big, 1 = small. Big -> small that we didn't force ourselves = a real hit.
        if real_gameplay and counters_safe:
            if player_size == 1 and self.last_player_size == 0 and not gating_forced_shrink_this_tick:
                self.took_damage_this_level = True
            self.last_player_size = player_size

        # ---------- 3) Coin checks ----------
        if coin_checks_enabled and real_gameplay and counters_safe:
            if coin_tally != self.last_coin_tally:
                if coin_tally > self.last_coin_tally:
                    delta = coin_tally - self.last_coin_tally
                elif self.last_coin_tally >= 90 and coin_tally <= 9:
                    # real rollover: the 100th coin gives a 1-up and wraps 99 -> 00
                    delta = coin_tally + 100 - self.last_coin_tally
                else:
                    # went DOWN without a rollover (new game, 01 -> 00): a reset, not coins
                    delta = 0
                self.total_coins_seen += delta
                self.last_coin_tally = coin_tally

            milestones_reached = min(self.total_coins_seen // coin_interval, coin_check_count)
            while self.coin_checks_sent < milestones_reached:
                self.coin_checks_sent += 1
                location_id = location_table.get(f"Coin Milestone #{self.coin_checks_sent}")
                if location_id and location_id not in ctx.checked_locations:
                    await ctx.send_msgs([{"cmd": "LocationChecks", "locations": [location_id]}])
        elif coin_checks_enabled and real_gameplay:
            self.last_coin_tally = coin_tally  # re-baseline quietly while the area settles

        # ---------- 3.5) Stomp-kill checks ----------
        if kill_checks_enabled and real_gameplay and counters_safe:
            if stomp_chain > self.last_stomp_chain:
                self.total_kills_seen += stomp_chain - self.last_stomp_chain
            self.last_stomp_chain = stomp_chain

            kill_milestones_reached = min(self.total_kills_seen // kill_interval, kill_check_count)
            while self.kill_checks_sent < kill_milestones_reached:
                self.kill_checks_sent += 1
                location_id = location_table.get(f"Stomp Kill Milestone #{self.kill_checks_sent}")
                if location_id and location_id not in ctx.checked_locations:
                    await ctx.send_msgs([{"cmd": "LocationChecks", "locations": [location_id]}])
        elif kill_checks_enabled and real_gameplay:
            self.last_stomp_chain = stomp_chain

        # ---------- 4) 1-Up checks ----------
        if oneup_checks_enabled and real_gameplay and counters_safe:
            if lives - self.last_lives == 1 and not self.granting_life_via_ap:
                from .Locations import MAX_ONEUP_CHECKS
                if self.oneup_checks_sent < MAX_ONEUP_CHECKS:
                    self.oneup_checks_sent += 1
                    location_id = location_table.get(f"1-Up Get #{self.oneup_checks_sent}")
                    if location_id and location_id not in ctx.checked_locations:
                        await ctx.send_msgs([{"cmd": "LocationChecks", "locations": [location_id]}])
            self.granting_life_via_ap = False

        if real_gameplay:
            self.last_lives = lives

        # ---------- 5) Inject received items (one per tick, never blocks the queue) ----------
        if self.items_received_count < len(ctx.items_received):
            item = ctx.items_received[self.items_received_count]
            item_name = ctx.item_names.lookup_in_game(item.item)

            if item_name == "1-Up Mushroom":
                self.granting_life_via_ap = True
                writes.append((NUMBER_OF_LIVES, bytes([min(lives + 1, 99)]), "RAM"))
                self.items_received_count += 1
            elif item_name in ("Progressive Mushroom", "Progressive Fire Flower", "Progressive Star"):
                self.items_received_count += 1  # only changes the gating state
            elif item_name == "Star Power":
                if real_gameplay and ((not star_gating) or self.star_unlocked):
                    writes.append((STAR_INVINCIBLE_TIMER, bytes([STAR_POWER_FRAMES]), "RAM"))
                self.items_received_count += 1  # consumed either way, so the queue never gets stuck
            elif item_name in TRAP_ITEM_NAMES:
                self._apply_trap_writes(item_name, player_status, x_position, writes)
                self.items_received_count += 1
                await self.send_traplink(ctx, item_name)
            else:
                self.items_received_count += 1  # e.g. "10 Coins": no effect implemented

        # ---------- 5.5) Apply pending trap effects (own items + Trap Link) ----------
        if real_gameplay and self.pending_traps:
            trap_name = self.pending_traps.pop(0)
            self._apply_trap_writes(trap_name, player_status, x_position, writes)

        if self.pending_death_trap and real_gameplay:
            writes.append((PLAYER_Y_POSITION, bytes([0xF8]), "RAM"))
            self.pending_death_trap = False

        if now < self.ice_trap_until:
            # Pin X position + zero speed every tick: the game recalculates speed
            # from the buttons faster than we poll, so snapping position is what holds him.
            writes.append((PLAYER_X_POSITION, bytes([self.ice_trap_x]), "RAM"))
            writes.append((PLAYER_X_SPEED, bytes([0]), "RAM"))

        if writes:
            await write(ctx.bizhawk_ctx, writes)

        # ---------- 6) Goal ----------
        if castle_sequence and world_number == FINAL_WORLD and level_number == FINAL_LEVEL \
                and not ctx.finished_game:
            if goal == 1:  # no_skips: every level's own "Complete" check must exist
                cleared = sum(
                    1 for name in LEVEL_TO_LOCATION.values()
                    if location_table.get(name) in ctx.checked_locations
                )
                if cleared < 32:
                    if not self.warned_no_skips_goal_failed:
                        self.warned_no_skips_goal_failed = True
                        logger.info(
                            f"Reached the end, but your goal is 'No Skips' and only {cleared} of 32 "
                            f"levels were cleared -- goal not completed."
                        )
                    return
            await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
            ctx.finished_game = True
