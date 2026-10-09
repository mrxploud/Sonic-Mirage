"""The two games as the fusion host sees them: how to boot each one into gameplay,
how one set of logical actions maps onto each pad, and where the player lives in RAM.

Every address below was found by running the game under scripted input and checked
against the screen; nothing is taken from documentation alone.
"""
import os

import numpy as np

import retro

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIONS = ('left', 'right', 'up', 'down', 'jump', 'shoot', 'start')


class Game:
    core_path = disc = name = None
    options = {}
    pad = {}

    def __init__(self, log=None):
        self.core = retro.Core(os.path.join(ROOT, self.core_path), os.path.join(ROOT, 'bios'),
                               os.path.join(ROOT, 'saves', self.name), self.options, log)
        self.core.load(os.path.join(ROOT, self.disc))
        self.map_memory()

    def boot(self):
        """Reach gameplay: reuse the saved state, or replay the menus once and save it."""
        path = os.path.join(ROOT, 'states', self.name + '.state')
        if os.path.exists(path):
            with open(path, 'rb') as f:
                self.core.load_state(f.read())
            return
        for frame in range(1, self.boot_frames + 1):
            self.core.run(1, self.boot_input(frame))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(self.core.save_state())

    def step(self, actions):
        self.core.run(1, {self.pad[a] for a in actions if a in self.pad})


class SilhouetteMirage(Game):
    """Saturn, T-32901G V1.003. Boots into the tutorial stage (STG00.BIN), in the practice
    round of lesson 9, "grab the enemy": the one whose enemy can be grabbed."""
    name = 'silhouette_mirage'
    core_path = 'cores/mednafen_saturn/mednafen_saturn_libretro.dll'
    disc = 'discs/silhouette_mirage/silhouette_mirage.cue'
    # Saturn C jumps; Saturn B fires the equipped parasite; Saturn A puts up the reflector,
    # or grabs an enemy at arm's length and then hits it; Saturn X swaps which side is
    # Silhouette and which is Mirage. The remaining moves are combinations: direction
    # twice dashes, down twice lies prone, diagonal-down + C slides, A+B+C sets off the
    # parasite bomb. (L and R step through the three equipped parasites; not used here,
    # the inventory is written directly.)
    pad = dict(left=retro.LEFT, right=retro.RIGHT, up=retro.UP, down=retro.DOWN,
               jump=retro.R, shoot=retro.A, grab=retro.B, swap=retro.Y, start=retro.START)

    # Parasites, her weapons: seven kinds in six levels ("ages"). The game keeps three
    # equipped, one byte each, kind * 6 + level with 0 for an empty slot; the first slot
    # is the one she fires, and it is read live.
    INVENTORY = 0x06002050
    PARASITES = ('Surosa', 'Grattoni', 'Cavitas', 'Angara', 'Envia', 'Rusty', 'Priday')
    LEVELS = 6
    VITAL = 0xF0                  # words: most vital, vital, most spirit, spirit
    LESSON = 9
    boot_frames = 4880 + (LESSON - 1) * 378 + 274

    # Player object. This address belongs to the tutorial overlay's own data area;
    # other stage overlays are a different size and are expected to place it elsewhere.
    PLAYER = 0x060B6500
    X, Y = 0x18, 0x1C             # 16.16 fixed point, world pixels
    VEL_Y = 0x28                  # 16.16 fixed point
    ACTION = 0x5C                 # high byte is the move, see the constants below
    DASH, JUMP, FALL, SLIDE, BOMB = 0x02, 0x04, 0x05, 0x17, 0x18
    PRONE_DOWN, PRONE, PRONE_DASH, REFLECT, SWAP = 0x19, 0x1A, 0x1B, 0x1C, 0x1E
    THROW_SIDE, THROW_UP, THROW_DOWN, AROUND, GRAB = 0x12, 0x13, 0x14, 0x16, 0x1D
    ANIM_FRAME = 0x6C
    SCREEN_X, SCREEN_Y = 0x70, 0x72

    @classmethod
    def boot_input(cls, frame):
        if frame <= 3000:                               # logos, title, "GAME START"
            return {retro.START} if frame in (1500, 1501, 1800, 1801, 2100, 2101) else set()
        t = frame - 3000
        if t <= 1560:                                   # tutorial dialogue: tap Saturn A
            return {retro.B} if t <= 1263 and t % 120 in (60, 61, 62, 63) else set()
        t -= 1560 + 320                                 # lesson 1's menu, text finished
        if t <= 0:
            return set()
        if t <= (cls.LESSON - 1) * 378:                 # "next" once per lesson
            k = (t - 1) % 378
            return {retro.RIGHT} if k < 4 else {retro.B} if 24 <= k < 28 else set()
        return {retro.B} if t - (cls.LESSON - 1) * 378 <= 4 else set()     # "execute"

    def map_memory(self):
        # SYSTEM_RAM is low work RAM followed by high work RAM, stored as host-endian words
        ptr, size = self.core.memory(retro.MEMORY_SYSTEM_RAM)
        self.low = retro.Region('WRAM-L', 0x00200000, ptr, 0x100000, swap16=True)
        self.high = retro.Region('WRAM-H', 0x06000000, ptr + 0x100000, 0x100000, swap16=True)

    def player(self):
        m, p = self.high, self.PLAYER
        return dict(x=m.s32(p + self.X) / 65536, y=m.s32(p + self.Y) / 65536,
                    vel_y=m.s32(p + self.VEL_Y) / 65536, action=m.u16(p + self.ACTION),
                    frame=m.u16(p + self.ANIM_FRAME),
                    screen_x=m.s16(p + self.SCREEN_X), screen_y=m.s16(p + self.SCREEN_Y))

    # --- treadmill ---------------------------------------------------------------------
    # The game is kept in the tutorial room as a character simulator: Shyna is put back
    # on the same spot before every frame, and only how far she moved during the frame
    # is used. The room's floor is flat; she counts as airborne whenever she is above it,
    # falls at 0.25 px/frame^2 and never faster than 8 px/frame.
    HOME_X, FLOOR_Y = 512, 304
    AIR_Y = 280                   # held here while airborne: one frame of fall cannot reach the floor
    FRAME_DX, FRAME_DY = 5, 8     # screen coordinates -> pixels of the core's 330x240 frame
    VIEW = (112, 232)             # rows of the frame that show the playfield
    PHASE = 0x060C6CBD            # tutorial progress: 2 while the practice round runs

    # The lesson's enemy, same object layout as the player. It is kept invisible and
    # used as a stand-in for whichever of the host's enemies is within her reach, so
    # that her grab, hits and throws have something to act on.
    ENEMY = 0x060B7000
    ENEMY_TIMER, ENEMY_HIDE = 0x58, 0x3C
    IDLE, BREAK_FREE, THROWN, DOWN, GETTING_UP, HELD = 0x00, 0x05, 0x0A, 0x0B, 0x0C, 0x0F
    GRAB_REACH = 24               # pressing A this close to it grabs instead of firing
    MONEY = 0x0601DB6E            # +10 for every hit on a held enemy

    def hold(self, airborne, enemy=None, parasite=(0, 1)):
        """Call before each frame; returns the position she starts the frame from.
        `enemy` is where the stand-in belongs relative to her, or None to keep it away;
        it is left alone while she is holding it or it is flying from a throw.
        `parasite` is the (kind, level) she has equipped."""
        m, p, e = self.high, self.PLAYER, self.ENEMY
        y = self.AIR_Y if airborne else m.s32(p + self.Y) / 65536
        m.w32(p + self.X, self.HOME_X << 16)
        if airborne:
            m.w32(p + self.Y, self.AIR_Y << 16)
        m.w8(self.PHASE, 2)                           # the lesson never counts as completed
        m.w8(e + self.ENEMY_HIDE, 0)                  # not drawn
        m.write(self.INVENTORY, bytes([parasite[0] * 6 + parasite[1], 0, 0]))
        for obj in (p, e):                            # neither of them wears down in here
            m.w16(obj + self.VITAL + 2, m.u16(obj + self.VITAL))
            m.w16(obj + self.VITAL + 6, m.u16(obj + self.VITAL + 4))
        if self.enemy_state() not in (self.HELD, self.THROWN):
            if enemy is None:
                m.w32(e + self.X, 3000 << 16)
            else:
                m.w32(e + self.X, int((self.HOME_X + enemy[0]) * 65536))
                m.w32(e + self.Y, int((y + enemy[1]) * 65536))
        return self.HOME_X, y

    def enemy_state(self):
        return self.high.u8(self.ENEMY + self.ACTION)

    def enemy_offset(self):
        """Where the stand-in is relative to her, in pixels."""
        m, p, e = self.high, self.PLAYER, self.ENEMY
        return ((m.s32(e + self.X) - m.s32(p + self.X)) / 65536, (m.s32(e + self.Y) - m.s32(p + self.Y)) / 65536)

    def enemy_reset(self):
        """Back on its feet at once, ready to stand in for another enemy."""
        self.high.w16(self.ENEMY + self.ACTION, 0)
        self.high.w16(self.ENEMY + self.ENEMY_TIMER, 0)

    def money(self):
        return self.high.u16(self.MONEY)

    def land(self):
        self.high.w32(self.PLAYER + self.Y, (self.FLOOR_Y - 1) << 16)

    def set_vel_y(self, v):
        self.high.w32(self.PLAYER + self.VEL_Y, int(v * 65536))

    def build_plate(self):
        """The room with nobody in it: per-pixel median of frames with her in different places."""
        path = os.path.join(ROOT, 'states', self.name + '.plate.npy')
        if os.path.exists(path):
            self.plate = np.load(path)
            return
        state = self.core.save_state()
        frames = []
        for x in (392, 420, 448, 476, 548, 576, 604, 632, 660):
            for _ in range(20):
                self.hold(False)
                self.high.w32(self.PLAYER + self.X, x << 16)
                self.step(())
            frames.append(self.core.image())
        self.plate = np.median(np.stack(frames), axis=0).astype(np.int16)
        np.save(path, self.plate)
        self.core.load_state(state)

    def sprite(self):
        """Everything the game drew over the empty room this frame, as RGBA, plus the
        position of Shyna's feet inside that image."""
        frame = self.core.image()
        top, bottom = self.VIEW
        view = frame[top:bottom]
        alpha = (np.abs(view.astype(np.int16) - self.plate[top:bottom]).max(axis=2) > 12)
        rgba = np.dstack([view, alpha.astype(np.uint8) * 255])
        m, p = self.high, self.PLAYER
        feet = (m.s16(p + self.SCREEN_X) + self.FRAME_DX, m.s16(p + self.SCREEN_Y) + self.FRAME_DY - top)
        return rgba, feet


class SonicCD(Game):
    """Sega CD, MK-4407 (US). Boots into Palmtree Panic Zone 1."""
    name = 'sonic_cd'
    core_path = 'cores/genesis_plus_gx/genesis_plus_gx_libretro.dll'
    disc = 'discs/sonic_cd/sonic_cd.cue'
    # without a backup RAM cart the game uses the console's internal save memory
    options = {'genesis_plus_gx_cart_size': 'disabled'}
    pad = dict(left=retro.LEFT, right=retro.RIGHT, up=retro.UP, down=retro.DOWN,
               jump=retro.B, start=retro.START)
    boot_frames = 2760

    # Same object layout as Sonic 1 on the Mega Drive
    PLAYER = 0xFFD000
    X, Y = 0x08, 0x0C             # pixels (word) followed by a sub-pixel word
    VEL_X, VEL_Y, INERTIA = 0x10, 0x12, 0x14
    ANIM, STATUS = 0x1C, 0x22
    CAMERA_X, CAMERA_Y = 0xFFF700, 0xFFF704

    @staticmethod
    def boot_input(frame):
        return {retro.START} if frame in (1300, 1301, 1700, 1701, 2100, 2101) else set()

    def map_memory(self):
        d = {m['name']: m for m in self.core.memory_map}
        self.ram = retro.Region('68KRAM', 0xFF0000, d['68KRAM']['ptr'], d['68KRAM']['len'], swap16=True)
        self.word_ram = retro.Region('WORDRAM', 0x200000, d['WORDRAM']['ptr'], d['WORDRAM']['len'], swap16=True)

    def player(self):
        m, p = self.ram, self.PLAYER
        return dict(x=m.u16(p + self.X), y=m.u16(p + self.Y), vel_x=m.s16(p + self.VEL_X),
                    vel_y=m.s16(p + self.VEL_Y), inertia=m.s16(p + self.INERTIA),
                    anim=m.u8(p + self.ANIM), status=m.u8(p + self.STATUS),
                    camera_x=m.u16(self.CAMERA_X), camera_y=m.u16(self.CAMERA_Y))

    # --- body --------------------------------------------------------------------------
    # Sonic stays in the level as an invisible body. The game still collides him with
    # the terrain and with every object, so slopes, platforms and rings work unchanged;
    # only his speed comes from outside. Speeds are 8.8 fixed point, pixels per frame.
    MAP = 0x04                    # pointer to his sprite mappings
    HEIGHT = 0x16                 # half-height in pixels: 19 standing
    ROUTINE = 0x24                # 2 while he is under normal control, 4 hurt, 6 dying
    ANGLE = 0x26                  # of the ground under him: 00 floor, C0 right wall, 80 ceiling
    FLASH = 0x30                  # post-hit timer; nothing can hurt him while it runs
    AIR = 0x02                    # status bit
    GRAVITY, FRICTION = 0x38, 0x0C
    WALL_SPEED = 0x300            # the game drops him off walls and ceilings below 0x280

    # Level objects: 0x40 bytes each. A badnik is an object whose collision byte is
    # 01..3F; when the game destroys one it turns the slot into object 18, the explosion,
    # which goes on to release the flower seed by itself.
    SLOTS = np.arange(0xD800, 0xF000, 0x40)
    COLLISION, EXPLOSION = 0x20, 0x18
    BLANK = 0x204                 # zero bytes needed for mappings with no pieces in any frame

    def controllable(self):
        return self.ram.u8(self.PLAYER + self.ROUTINE) == 2

    def airborne(self):
        return bool(self.ram.u8(self.PLAYER + self.STATUS) & self.AIR)

    def hide(self):
        """Point his mappings at a run of zero bytes, so that every animation frame has
        no sprite pieces. He stays in the level and can still be hurt."""
        raw = self.word_ram.raw
        o = getattr(self, '_blank', None)
        if o is None or raw[o:o + self.BLANK].any():
            z = np.diff(np.concatenate(([0], (raw == 0).view(np.int8), [0])))
            starts, ends = np.flatnonzero(z == 1), np.flatnonzero(z == -1)
            k = int(np.argmax(ends - starts))
            if ends[k] - starts[k] < self.BLANK + 2:
                self.ram.w16(self.PLAYER + self.FLASH, 3)      # no room: fall back to the timer
                return
            o = self._blank = ((int(starts[k]) + int(ends[k]) - self.BLANK) // 2) & ~1
        self.ram.w32(self.PLAYER + self.MAP, self.word_ram.start + o)

    def flash(self):
        return self.ram.u16(self.PLAYER + self.FLASH)

    def shield(self):
        """Nothing can hurt him during the coming frame. The game takes one off the
        timer before it checks for hits, so it has to start the frame at two."""
        if self.flash() < 2:
            self.ram.w16(self.PLAYER + self.FLASH, 2)

    def drive(self, dx, dy, jump):
        """Move the body by (dx, dy) pixels this frame. On the ground only dx counts and
        the game bends it along the slope; `jump` lifts him off the ground first.
        Returns what was written, for comparing against what the game made of it:
        ('air', x speed, y speed) or ('ground', ground speed)."""
        m, p = self.ram, self.PLAYER
        if jump and not self.airborne():
            m.w8(p + self.STATUS, m.u8(p + self.STATUS) | self.AIR)
        if self.airborne():
            m.w16(p + self.VEL_X, round(dx * 256))
            m.w16(p + self.VEL_Y, round(dy * 256))
            return 'air', round(dx * 256), round(dy * 256)
        speed = round(abs(dx) * 256)
        if speed and (m.u8(p + self.ANGLE) + 0x20) & 0xC0:
            speed = max(speed, self.WALL_SPEED)    # keep her on loops and walls while she moves
        # with no pad input the game takes friction off the ground speed before moving
        inertia = (speed + self.FRICTION) * (1 if dx > 0 else -1) if speed else 0
        m.w16(p + self.INERTIA, inertia)
        return 'ground', inertia

    def feet(self):
        """Screen position of the point between his feet, and the ground angle in
        degrees counter-clockwise (0 on the floor, 90 on a right-hand wall)."""
        m, p = self.ram, self.PLAYER
        turn = (-m.u8(p + self.ANGLE) & 0xFF) * 360 / 256
        h = m.u8(p + self.HEIGHT)
        sx, sy = self.on_screen(p)
        return (sx + round(h * np.sin(np.radians(turn))), sy + round(h * np.cos(np.radians(turn))), turn)

    def on_screen(self, obj):
        """Screen position of an object. Levels wrap vertically, so the difference from
        the camera is taken as a signed 16-bit number."""
        m = self.ram
        return (((m.u16(obj + self.X) - m.u16(self.CAMERA_X) + 0x8000) & 0xFFFF) - 0x8000,
                ((m.u16(obj + self.Y) - m.u16(self.CAMERA_Y) + 0x8000) & 0xFFFF) - 0x8000)

    def enemies(self):
        """Badniks currently on screen: (slot address, screen x, screen y)."""
        raw, s = self.ram.raw, self.SLOTS
        # bytes are stored swapped in pairs: guest offset k is raw[k ^ 1]
        live = (raw[s + 1] != 0) & (raw[s + 0x21] > 0) & (raw[s + 0x21] < 0x40) & (raw[s] >= 0x80)
        return [(a,) + self.on_screen(a) for a in (0xFF0000 + int(k) for k in s[live])]

    def destroy(self, slot):
        m = self.ram
        m.w8(slot, self.EXPLOSION)
        m.w8(slot + self.ROUTINE, 0)
        m.w8(slot + self.COLLISION, 0)

    # Item monitors are object 19 with collision byte 46. Sonic breaks one by rolling
    # into it, which moves its routine from 2 to 4; the monitor then releases its item
    # and its explosion by itself, and the item goes to the body as usual.
    MONITOR, MONITOR_COLLISION = 0x19, 0x46

    def monitors(self):
        """Unbroken monitors currently on screen: (slot address, screen x, screen y)."""
        raw, s = self.ram.raw, self.SLOTS
        live = ((raw[s + 1] == self.MONITOR) & (raw[s + 0x21] == self.MONITOR_COLLISION)
                & (raw[s + 0x25] == 2) & (raw[s] >= 0x80))
        return [(a,) + self.on_screen(a) for a in (0xFF0000 + int(k) for k in s[live])]

    def open_monitor(self, slot):
        self.ram.w8(slot + self.ROUTINE, 4)

    def alive(self, slot, kind):
        """Is this slot still the same object it was when it was picked?"""
        return self.ram.u8(slot) == kind

    def carry(self, slot, sx, sy):
        """Put an object at a screen position and stop it moving by itself this frame."""
        m = self.ram
        m.w16(slot + self.X, m.u16(self.CAMERA_X) + int(sx))
        m.w16(slot + self.Y, m.u16(self.CAMERA_Y) + int(sy))
        m.w16(slot + self.VEL_X, 0)
        m.w16(slot + self.VEL_Y, 0)

    RINGS, RINGS_CHANGED = 0xFF1512, 0xFF150F

    def add_rings(self, n):
        self.ram.w16(self.RINGS, min(999, self.ram.u16(self.RINGS) + n))
        self.ram.w8(self.RINGS_CHANGED, 1)             # makes the HUD redraw the count
