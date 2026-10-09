"""Boot one game in one libretro core, with scripted input, screenshots and RAM dumps.

    python -I tools/run_core.py saturn --frames 900 --shots 300,600,900
    python -I tools/run_core.py segacd --frames 1200 --input "600-610:START" --shots 1200 --save-state title

--input   semicolon-separated "first-last:BTN+BTN" (RetroPad names), frames are 1-based
--shots   frame numbers to save as PNG under out/<system>/ ("a:b:step" ranges allowed)
--sheet   also tile the shots into one labelled contact sheet, out/<system>/<name>.png
--dump    frame numbers at which every RAM region is written to out/<system>/
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'host'))

import retro  # noqa: E402
from PIL import Image  # noqa: E402

SYSTEMS = {
    'saturn': dict(core='cores/mednafen_saturn/mednafen_saturn_libretro.dll',
                   game='discs/silhouette_mirage/silhouette_mirage.cue', options={}),
    'saturn-hle': dict(core='cores/yabause/yabause_libretro.dll',
                       game='discs/silhouette_mirage/silhouette_mirage.cue', options={}),
    'segacd': dict(core='cores/genesis_plus_gx/genesis_plus_gx_libretro.dll',
                   game='discs/sonic_cd/sonic_cd.cue',
                   # no backup RAM cart: the game then saves to the console's internal BRAM
                   options={'genesis_plus_gx_cart_size': 'disabled'}),
}


def parse_input(spec):
    out = []
    for part in filter(None, (spec or '').split(';')):
        span, buttons = part.split(':')
        first, _, last = span.partition('-')
        out.append((int(first), int(last or first), {retro.BUTTONS[b.strip().upper()] for b in buttons.split('+')}))
    return out


def frames_arg(s):
    out = set()
    for part in filter(None, (s or '').split(',')):
        if ':' in part:
            first, last, step = (int(x) for x in part.split(':'))
            out.update(range(first, last + 1, step))
        else:
            out.add(int(part))
    return sorted(out)


def contact_sheet(images, path, columns=5):
    """Tile (frame_number, rgb_array) pairs into one labelled PNG."""
    from PIL import ImageDraw
    w = max(im.shape[1] for _, im in images)
    h = max(im.shape[0] for _, im in images)
    rows = -(-len(images) // columns)
    sheet = Image.new('RGB', (columns * w, rows * (h + 12)), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for k, (n, im) in enumerate(images):
        x, y = k % columns * w, k // columns * (h + 12)
        sheet.paste(Image.fromarray(im), (x, y + 12))
        draw.text((x + 2, y), 'frame %d' % n, fill=(255, 255, 0))
    sheet.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('system', choices=SYSTEMS)
    ap.add_argument('--frames', type=int, default=600)
    ap.add_argument('--input', default='')
    ap.add_argument('--shots', default='')
    ap.add_argument('--sheet')
    ap.add_argument('--dump', default='')
    ap.add_argument('--load-state')
    ap.add_argument('--save-state')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--info', action='store_true', help='print core options, input map and memory map')
    a = ap.parse_args()

    cfg = SYSTEMS[a.system]
    out = os.path.join(ROOT, 'out', a.system)
    os.makedirs(out, exist_ok=True)
    log = None if a.quiet else (lambda lvl, msg: print('  [%s] %s' % ('DIWE'[min(lvl, 3)], msg)))
    core = retro.Core(os.path.join(ROOT, cfg['core']), os.path.join(ROOT, 'bios'),
                      os.path.join(ROOT, 'saves', a.system), cfg['options'], log)
    print('core:', core.library)
    core.load(os.path.join(ROOT, cfg['game']))
    print('loaded: %.3f fps, pixel format %d' % (core.fps, core.pixel_format))

    if a.load_state:
        with open(os.path.join(out, a.load_state + '.state'), 'rb') as f:
            core.load_state(f.read())

    if a.info:
        print('options:')
        for k, v in core.variables.items():
            print('   %-40s %s' % (k, v))
        print('input map (port 0):')
        names = {v: k for k, v in retro.BUTTONS.items()}
        for port, dev, idx, bid, desc in core.descriptors:
            if port == 0 and dev == retro.DEVICE_JOYPAD:
                print('   %-7s -> %s' % (names.get(bid, bid), desc))
        print('memory map:')
        for d in core.memory_map:
            print('   start=%08X len=%08X flags=%X select=%X offset=%X %s' %
                  (d['start'], d['len'], d['flags'], d['select'], d['offset'], d['name']))
        for mid, nm in ((0, 'SAVE_RAM'), (2, 'SYSTEM_RAM'), (3, 'VIDEO_RAM')):
            m = core.memory(mid)
            print('   retro_get_memory %-10s %s' % (nm, 'len=%X' % m[1] if m else '-'))

    script = parse_input(a.input)
    shots, dumps = set(frames_arg(a.shots)), set(frames_arg(a.dump))
    sheet = []
    t0 = time.time()
    for n in range(1, a.frames + 1):
        pressed = set()
        for first, last, buttons in script:
            if first <= n <= last:
                pressed |= buttons
        core.run(1, pressed)
        if n in shots and core.frame:
            if a.sheet:
                sheet.append((n, core.image()))
            else:
                Image.fromarray(core.image()).save(os.path.join(out, 'f%05d.png' % n))
        if n in dumps:
            for d in core.memory_map:
                if d['ptr'] and d['len']:
                    reg = core.region(d['name'], d['start'], d['ptr'], d['len'])
                    reg.raw.tofile(os.path.join(out, 'f%05d_%08X.ram' % (n, d['start'])))
    dt = time.time() - t0
    print('ran %d frames in %.1fs (%.0f fps)' % (a.frames, dt, a.frames / dt if dt else 0))
    if core.frame:
        print('last frame: %dx%d' % core.frame[1:3])
    if sheet:
        contact_sheet(sheet, os.path.join(out, a.sheet + '.png'))

    if a.save_state:
        with open(os.path.join(out, a.save_state + '.state'), 'wb') as f:
            f.write(core.save_state())
        print('state saved:', a.save_state)


if __name__ == '__main__':
    main()
