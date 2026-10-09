"""Fusion host: Shyna from Silhouette Mirage playing inside Sonic CD's stages.

Both games run in this process, one frame each per tick.

  * Silhouette Mirage is the guest. It is held in its tutorial room as a treadmill:
    it takes the controls and decides how Shyna moves, animates and fires.
  * Sonic CD is the host. Sonic stays in the level as an invisible body that is pushed
    by Shyna's motion, so the level's own collision and objects act on her.
  * The picture is Sonic CD's, with Shyna cut out of the guest's frame and drawn where
    the body is.
  * Her attacks destroy badniks; badniks hurt the body, which costs rings as usual.
  * The guest's own (invisible) enemy stands in for the badnik within her reach, so
    she can grab it, hit it and throw it; what happens to the stand-in happens to
    the badnik.

    python -I host/fusion.py                    live window
    python -I host/fusion.py --debug            live window with the guest's screen beside it
    python -I host/fusion.py --script "60:right;20:right+jump;40:" --sheet out/run.png

Keys in the live window: arrows move, Z jumps, X fires the equipped parasite, C puts up
the reflector, S swaps attribute, A sets off the parasite bomb, Enter is Start, Esc
quits. Direction twice dashes, down twice lies prone (up twice stands), diagonal-down
+ Z slides. Next to a badnik C grabs it; then C hits it and a direction + C throws it.
1-7 pick the parasite, Q and E step through them, - and = lower and raise its level.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import games  # noqa: E402

KEYS = {'Left': {'left'}, 'Right': {'right'}, 'Up': {'up'}, 'Down': {'down'},
        'z': {'jump'}, 'x': {'shoot'}, 'c': {'grab'}, 's': {'swap'}, 'a': {'shoot', 'grab', 'jump'},
        'Return': {'start'}}

BODY = (22, 56, 4)        # Shyna's own outline around her feet: half-width, above, below
REACH = 14                # half-size of the box a badnik is hit in
PAD = 64                  # spare rows under the guest's strip, so it can be rotated
CHEST = 16                # height above her feet at which she holds an enemy
NEAR = (72, 26)           # a badnik this close (x, y from her chest) gets the stand-in


class Fusion:
    def __init__(self, speed=1.0, god=False):
        self.guest = games.SilhouetteMirage()
        self.host = games.SonicCD()
        self.guest.boot()
        self.host.boot()
        self.guest.build_plate()
        self.speed = speed
        self.god = god
        self.carried = False      # the level is moving the body: Sonic's own physics run
        self.calm = 0             # frames of undisturbed flight while carried
        self.target = None        # (slot, object id, collision byte) of the badnik the stand-in mirrors
        self.gripped = False      # she is holding it, or it is flying from her throw
        self.parasite = [0, 1]    # kind and level of the parasite she has equipped
        self.opened = 0           # monitors broken open
        self.money = self.guest.money()
        self.kills = 0
        self.step(set())                         # one tick, so both cores have a picture
        self.frame = 0

    def step(self, actions):
        guest, host = self.guest, self.host
        airborne = host.airborne()
        control = host.controllable()

        x0, y0 = guest.hold(airborne or not control, self.reach_for(), self.parasite)
        guest.step(actions - {'start'})          # Start would open the tutorial's own menu
        shyna = guest.player()
        dx, dy = (shyna['x'] - x0) * self.speed, shyna['y'] - y0
        move = shyna['action'] >> 8
        self.sprite = guest.sprite()
        self.grip()

        jump, wrote = False, None
        pad = actions & {'start'}
        host.hide()
        if control:
            if self.carried:
                pad |= actions & {'left', 'right', 'jump'}     # Sonic's own steering
            else:
                jump = not airborne and shyna['vel_y'] < 0
                wrote = host.drive(dx, dy, jump)
            self.attack(move)
        before = host.player()
        host.step(pad)
        body = host.player()
        flying = host.airborne()
        self.arbitrate(wrote, before, body, flying, dx)

        # the body is on the ground but she is still coming down in her own room
        if not flying and host.controllable() and shyna['y'] < guest.FLOOR_Y - 0.01 and shyna['vel_y'] >= 0:
            guest.land()
        self.frame = getattr(self, 'frame', 0) + 1
        self.trace = dict(dx=dx, dy=dy, jump=jump, airborne=flying, carried=self.carried,
                          x=body['x'], y=body['y'], action=shyna['action'], control=control)

    def arbitrate(self, wrote, before, body, flying, dx):
        """Decide who moves the body next frame. Normally Shyna does. When the level
        gives it a speed she could not have produced (launcher, spring, running off a
        ramp at speed) Sonic's own physics take over until it lands or slows to her pace."""
        guest, host = self.guest, self.host
        vx, vy, gravity = body['vel_x'], body['vel_y'], host.GRAVITY
        if not host.controllable():
            self.carried = False
        elif self.carried:
            if not flying:
                self.calm = 0
                if abs(body['inertia']) <= max(abs(dx) * 256, 0x200) + 0x40:
                    self.carried = False
            else:
                free = abs(vy - before['vel_y'] - gravity) <= 4 and abs(vx - before['vel_x']) <= 0x30
                self.calm = self.calm + 1 if free else 0
                if self.calm >= 8 and abs(vx) <= 0x400 and abs(vy) <= 0x400:
                    guest.set_vel_y(vy / 256)        # gentle enough for her to take over in mid-air
                    self.carried = False
        elif wrote and wrote[0] == 'ground':
            if flying:                                # left the ground without jumping
                thrown = abs(vy) > 0x400 or abs(vx) > abs(dx) * 256 + 0x100
            else:
                thrown = abs(body['inertia']) > abs(wrote[1]) + 0x80
            if thrown:
                self.carried, self.calm = True, 0
        elif wrote and flying:
            off = abs(vy - wrote[2] - gravity) > 0x40
            if abs(vx) > abs(wrote[1]) + 0x100 or (off and abs(vy) > 0x500):
                self.carried, self.calm = True, 0
            elif off:
                guest.set_vel_y(vy / 256)             # ceiling, or a push she can follow herself

    def reach_for(self):
        """Pick the badnik within her reach and say where the guest's stand-in enemy
        must be for it: the same place relative to her, level with her when the badnik
        is at a height she could grab it from."""
        host = self.host
        if self.gripped:
            return None
        bx, by, _ = host.feet()
        if self.target and not host.alive(*self.target[:2]):
            self.target = None
        best = None
        for slot, ex, ey in host.enemies():
            rx, ry = ex - bx, ey - (by - CHEST)
            if abs(rx) <= NEAR[0] and abs(ry) <= NEAR[1] and (best is None or abs(rx) < abs(best[1])):
                best = (slot, rx)
        if best is None:
            self.target = None
            return None
        if not self.target or self.target[0] != best[0]:
            self.target = (best[0], host.ram.u8(best[0]), host.ram.u8(best[0] + host.COLLISION))
        return best[1], 0

    def grip(self):
        """Carry over to the badnik whatever she did to the stand-in during the frame."""
        guest, host = self.guest, self.host
        state = guest.enemy_state()
        earned = (guest.money() - self.money) // 10
        self.money = guest.money()
        if not self.target or not host.alive(*self.target[:2]):
            if self.gripped or state in (guest.DOWN, guest.GETTING_UP):
                guest.enemy_reset()                  # nothing left for it to stand in for
            self.target, self.gripped = None, False
            return
        slot, _, collision = self.target
        if state in (guest.HELD, guest.THROWN):
            if earned > 0:
                host.add_rings(earned)               # hitting a held enemy shakes rings out of it
            self.gripped = True
            bx, by, _ = host.feet()
            ox, oy = guest.enemy_offset()
            sx, sy = bx + ox, by - CHEST + oy
            host.ram.w8(slot + host.COLLISION, 0)    # in her hands it cannot hurt her
            host.carry(slot, sx, sy)
            if state == guest.THROWN:                # a thrown badnik takes others with it
                for other, ex, ey in host.enemies():
                    if other != slot and abs(ex - sx) < 2 * REACH and abs(ey - sy) < 2 * REACH:
                        host.destroy(other)
                        self.kills += 1
        elif self.gripped:
            self.gripped = False
            if state in (guest.DOWN, guest.GETTING_UP):
                host.destroy(slot)                   # thrown, and it has hit the ground
                self.kills += 1
                self.target = None
            else:
                host.ram.w8(slot + host.COLLISION, collision)     # it broke free
            guest.enemy_reset()

    def attack(self, move):
        """Whatever the guest drew outside Shyna's own outline is one of her attacks:
        a badnik standing in it is destroyed and an item monitor is broken open. Some
        moves also make her body itself dangerous or untouchable for the frame."""
        guest, host = self.guest, self.host
        rgba, (fx, fy) = self.sprite
        hit = rgba[..., 3] > 0
        half, above, below = BODY
        hit[max(0, fy - above):fy + below, max(0, fx - half):fx + half] = False
        bx, by, _ = host.feet()
        ramming = move == guest.PRONE_DASH
        harmless = self.gripped or move in (guest.GRAB, guest.THROW_SIDE, guest.THROW_UP,
                                            guest.THROW_DOWN, guest.AROUND, guest.REFLECT)
        targets = [(slot, ex, ey, True) for slot, ex, ey in host.enemies()]
        targets += [(slot, ex, ey, False) for slot, ex, ey in host.monitors()]
        for slot, ex, ey, badnik in targets:
            gx, gy = fx + ex - bx, fy + ey - by
            # the reflector, and the sparks and coins from hitting a held enemy, are not attacks
            struck = not harmless and hit[max(0, gy - REACH):max(0, gy + REACH),
                                          max(0, gx - REACH):max(0, gx + REACH)].sum() >= 8
            touching = abs(ex - bx) < half + REACH and -above - REACH < ey - by < below + REACH
            if struck or (ramming and touching):
                if badnik:
                    host.destroy(slot)
                    self.kills += 1
                else:
                    host.open_monitor(slot)
                    self.opened += 1
        if self.god or move in (guest.SLIDE, guest.PRONE_DASH, guest.BOMB):
            host.shield()

    def picture(self):
        """Sonic CD's frame with the guest's sprites drawn at the body's feet."""
        shot = self.host.core.image()
        frame = Image.fromarray(shot).convert('RGBA')
        flash = self.host.flash()
        if (flash > 4 and not flash & 4) or shot[::8, ::8].max() < 24:
            return frame.convert('RGB')          # blinking after a hit; or the screen is faded out
        rgba, (fx, fy) = self.sprite
        hx, hy, turn = self.host.feet()
        sprite = Image.new('RGBA', (rgba.shape[1], rgba.shape[0] + PAD))
        sprite.paste(Image.fromarray(rgba, 'RGBA'), (0, 0))
        turn = round(turn / 45) % 8 * 45         # eight directions, as Sonic's own sprite has
        if turn:
            sprite = sprite.rotate(turn, Image.NEAREST, center=(fx, fy))
        box = (max(0, fx - hx), max(0, fy - hy),
               min(sprite.width, fx - hx + frame.width), min(sprite.height, fy - hy + frame.height))
        if box[0] < box[2] and box[1] < box[3]:      # some of it is on screen
            frame.alpha_composite(sprite.crop(box), (max(0, hx - fx), max(0, hy - fy)))
        return frame.convert('RGB')

    def equip(self, kind=None, step=0, level=0):
        """Change parasite: a kind by number, or `step` kinds along, or `level` levels up."""
        guest = self.guest
        if kind is not None:
            self.parasite[0] = kind
        self.parasite[0] = (self.parasite[0] + step) % len(guest.PARASITES)
        self.parasite[1] = min(guest.LEVELS, max(1, self.parasite[1] + level))

    def compose(self, scale=3, debug=False):
        fused = self.picture()
        fused = fused.resize((fused.width * scale, fused.height * scale), Image.NEAREST)
        label = '%s  nivel %d' % (self.guest.PARASITES[self.parasite[0]], self.parasite[1])
        draw = ImageDraw.Draw(fused)
        draw.text((fused.width - 8 * len(label) - 9, 9), label, fill=(0, 0, 0))
        draw.text((fused.width - 8 * len(label) - 10, 8), label, fill=(255, 240, 120))
        if not debug:
            return fused
        side = Image.fromarray(self.guest.core.image())
        side = side.resize((side.width * 2, side.height * 2), Image.NEAREST)
        canvas = Image.new('RGB', (fused.width + side.width, fused.height), (16, 16, 24))
        canvas.paste(fused, (0, 0))
        canvas.paste(side, (fused.width, 0))
        t = self.trace
        ImageDraw.Draw(canvas).text(
            (fused.width + 8, side.height + 8),
            'frame %d\nbody x=%d y=%d %s\nstep dx=%+.2f dy=%+.2f\nShyna action=%04X'
            % (self.frame, t.get('x', 0), t.get('y', 0), 'air' if t.get('airborne') else 'ground',
               t.get('dx', 0), t.get('dy', 0), t.get('action', 0)), fill=(200, 200, 200))
        return canvas


def run_script(fusion, script, sheet, every, first=0):
    """script: "frames:action+action;..." -- an empty action list means no input."""
    shots = []
    for part in filter(None, script.split(';')):
        n, _, acts = part.partition(':')
        actions = {a for a in acts.split('+') if a}
        for _ in range(int(n)):
            fusion.step(actions)
            if sheet and fusion.frame >= first and fusion.frame % every == 0:
                shots.append((fusion.frame, np.asarray(fusion.picture())))
        t = fusion.trace
        print('%5d %-16s body x=%5d y=%5d %-6s dx=%+.2f dy=%+.2f action=%04X rings=%d kills=%d monitors=%d%s%s' % (
            fusion.frame, acts or '(none)', t['x'], t['y'], 'air' if t['airborne'] else 'ground', t['dx'], t['dy'],
            t['action'], fusion.host.ram.u16(0xFF1512), fusion.kills, fusion.opened, '' if t['control'] else '  [no control]',
            '  [carried]' if t['carried'] else ''))
    if sheet:
        sys.path.insert(0, os.path.join(games.ROOT, 'tools'))
        from run_core import contact_sheet
        os.makedirs(os.path.dirname(os.path.abspath(sheet)), exist_ok=True)
        contact_sheet(shots, sheet, columns=4)
        print('wrote', sheet)


def run_live(fusion, debug, max_frames=0):
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title('Fusao - Shyna em Sonic CD')
    label = tk.Label(root, bd=0)
    label.pack()
    held = set()

    def press(e):
        if e.keysym in KEYS:
            held.add(e.keysym)
        elif e.keysym in '1234567':
            fusion.equip(kind=int(e.keysym) - 1)
        elif e.keysym in ('q', 'e'):
            fusion.equip(step=-1 if e.keysym == 'q' else 1)
        elif e.keysym in ('minus', 'equal', 'plus'):
            fusion.equip(level=-1 if e.keysym == 'minus' else 1)

    root.bind('<KeyPress>', press)
    root.bind('<KeyRelease>', lambda e: held.discard(e.keysym))
    root.bind('<Escape>', lambda e: root.destroy())
    period = 1.0 / 60
    clock = [time.perf_counter()]

    def tick():
        fusion.step(set().union(*(KEYS[k] for k in held)))
        photo = ImageTk.PhotoImage(fusion.compose(debug=debug))
        label.configure(image=photo)
        label.image = photo
        if max_frames and fusion.frame >= max_frames:
            root.destroy()
            return
        clock[0] += period
        delay = clock[0] - time.perf_counter()
        if delay < -0.25:                       # fell behind: do not try to catch up
            clock[0] = time.perf_counter()
            delay = 0
        root.after(max(1, int(delay * 1000)), tick)

    root.after(1, tick)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--script')
    ap.add_argument('--sheet')
    ap.add_argument('--every', type=int, default=20)
    ap.add_argument('--speed', type=float, default=1.0, help="multiplier on Shyna's horizontal speed")
    ap.add_argument('--god', action='store_true', help='nothing can hurt her')
    ap.add_argument('--parasite', default='1,1', help='kind 1-7 and level 1-6 to start with')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--frames', type=int, default=0, help='live mode: close after this many frames')
    ap.add_argument('--from-frame', type=int, default=0, help='script mode: start the sheet here')
    a = ap.parse_args()
    fusion = Fusion(a.speed, a.god)
    kind, level = (int(x) for x in a.parasite.split(','))
    fusion.equip(kind=kind - 1, level=level - 1)
    if a.script:
        run_script(fusion, a.script, a.sheet, a.every, a.from_frame)
    else:
        run_live(fusion, a.debug, a.frames)


if __name__ == '__main__':
    main()
