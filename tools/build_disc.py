"""Rebuild an ISO+MP3 rip as an emulator-ready CUE: data track + raw PCM audio tracks.

    python -I tools/build_disc.py <src_dir> <out_dir> [--ffmpeg ffmpeg.exe]

The source rips store each audio track as an MP3 with the *next* track's 2-second
pregap appended. Two layouts are produced:

  exact   The ISO9660 directory holds one .DA entry per audio track (Saturn games
          that start CD-DA by disc position). Each track is cut to the .DA length
          and placed at the .DA LBA, so in-game seeks land on the right sample.
  simple  No .DA entries (Sega CD plays by track number). Tracks are kept whole and
          laid out back to back after a standard 2-second pregap.
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys

SEC = 2048          # MODE1 user data per sector
RAW = 2352          # CD-DA bytes per sector


def iso_da_entries(iso):
    """Return [(lba, sectors, name)] for the root directory's .DA files, sorted by LBA."""
    with open(iso, 'rb') as f:
        f.seek(16 * SEC)
        pvd = f.read(SEC)
        if pvd[1:6] != b'CD001':
            return []
        ext, size = struct.unpack_from('<I', pvd, 158)[0], struct.unpack_from('<I', pvd, 166)[0]
        f.seek(ext * SEC)
        d = f.read(size)
    out = []
    i = 0
    while i < len(d):
        ln = d[i]
        if ln == 0:
            i = (i // SEC + 1) * SEC
            continue
        rec = d[i:i + ln]
        i += ln
        name = rec[33:33 + rec[32]].decode('ascii', 'replace').split(';')[0]
        if name.endswith('.DA'):
            out.append((struct.unpack_from('<I', rec, 2)[0], struct.unpack_from('<I', rec, 10)[0] // SEC, name))
    return sorted(out)


def msf(frames):
    return '%02d:%02d:%02d' % (frames // 4500, frames // 75 % 60, frames % 75)


def decode(ffmpeg, path):
    r = subprocess.run([ffmpeg, '-v', 'error', '-i', path, '-f', 's16le', '-acodec', 'pcm_s16le',
                        '-ar', '44100', '-ac', '2', 'pipe:1'], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise SystemExit('ffmpeg failed on %s: %s' % (path, r.stderr.decode('utf-8', 'replace')))
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--ffmpeg', default='ffmpeg')
    a = ap.parse_args()

    isos = [p for p in os.listdir(a.src) if p.lower().endswith('.iso')]
    if len(isos) != 1:
        raise SystemExit('expected exactly one .iso in %s' % a.src)
    iso = os.path.join(a.src, isos[0])
    mp3s = sorted((int(m.group(1)), p) for p in os.listdir(a.src)
                  if (m := re.search(r'(\d+)\.mp3$', p, re.I)))
    data_sectors = os.path.getsize(iso) // SEC
    da = iso_da_entries(iso)
    exact = len(da) == len(mp3s) and len(da) > 0
    print('%s: data track %d sectors, %d audio tracks, layout=%s'
          % (isos[0], data_sectors, len(mp3s), 'exact' if exact else 'simple'))

    os.makedirs(a.out, exist_ok=True)
    dst_iso = os.path.join(a.out, 'track01.iso')
    if not os.path.exists(dst_iso) or os.path.getsize(dst_iso) != os.path.getsize(iso):
        shutil.copyfile(iso, dst_iso)

    cue = ['FILE "track01.iso" BINARY', '  TRACK 01 MODE1/2048', '    INDEX 01 00:00:00']
    pos = data_sectors
    for k, (num, name) in enumerate(mp3s):
        pcm = decode(a.ffmpeg, os.path.join(a.src, name))
        if exact:
            lba, sectors, da_name = da[k]
            pregap = lba - pos
            if pregap < 0:
                raise SystemExit('track %d overlaps the previous one' % num)
            want = sectors * RAW
            pcm = pcm[:want].ljust(want, b'\0')
        else:
            pregap = 150 if k == 0 else 0
            sectors = -(-len(pcm) // RAW)
            pcm = pcm.ljust(sectors * RAW, b'\0')
            da_name = ''
        fn = 'track%02d.bin' % num
        with open(os.path.join(a.out, fn), 'wb') as o:
            o.write(pcm)
        cue += ['FILE "%s" BINARY' % fn, '  TRACK %02d AUDIO' % num]
        if pregap:
            cue.append('    PREGAP %s' % msf(pregap))
        cue.append('    INDEX 01 00:00:00')
        pos += pregap
        print('  track %02d  lba=%6d  sectors=%6d  %s' % (num, pos, sectors, da_name))
        pos += sectors
    print('  lead-out   lba=%6d  (%s)' % (pos, msf(pos)))

    cue_name = re.sub(r'[^a-z0-9]+', '_', os.path.splitext(isos[0])[0].lower()).strip('_') + '.cue'
    with open(os.path.join(a.out, cue_name), 'w', newline='\n') as o:
        o.write('\n'.join(cue) + '\n')
    print('wrote', os.path.join(a.out, cue_name))


if __name__ == '__main__':
    main()
