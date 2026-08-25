#!/usr/bin/env python3
# set-default-sub.py <dir> <match-text>
# Make the subtitle track whose title matches <match-text> the default one, so it
# turns on automatically in VLC. Every other subtitle track gets default=0.
#
# These are Naruto's two English sub tracks: "Dialogue (PGS)" (full captions) and
# "Signs & Songs (PGS)" (on-screen text only). The release ships with Signs & Songs
# default, which suits dub viewers; this flips it to full Dialogue on request.
#
# mkvpropedit is a metadata-only edit — it rewrites a few header bytes, not the
# video — so this runs in seconds across hundreds of files instead of re-encoding.
import os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

ROOT, MATCH = sys.argv[1], sys.argv[2].lower()
# --lang eng restricts the match to that subtitle language, so "signs" can't land on
# a French or Spanish signs track in a multi-language release.
LANG = (sys.argv[sys.argv.index('--lang') + 1].lower() if '--lang' in sys.argv else None)

def sub_tracks(path):
    """-> [(title, language)] in mkvpropedit s1..sN order.
    ffprobe is asked for both tags at once and prints them as 'title|lang' so the two
    lists can never drift out of sync (a missing tag prints empty, keeping alignment)."""
    # compact output is one line per stream: tag:title=X|tag:language=Y
    res = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 's',
         '-show_entries', 'stream_tags=title,language',
         '-of', 'compact=p=0:nk=0', path], capture_output=True, text=True).stdout.strip()
    tracks = []
    for line in res.split('\n'):
        if not line.strip():
            continue
        d = {}
        for part in line.split('|'):
            if '=' in part:
                k, v = part.split('=', 1)
                d[k.replace('tag:', '')] = v
        tracks.append((d.get('title', ''), d.get('language', '')))
    return tracks

def fix(path):
    tracks = sub_tracks(path)
    if not tracks:
        return ('nosubs', path)
    titles = [t for t, _ in tracks]
    if MATCH in ('none', 'off'):
        want = 0          # no track matches -> every subtitle track gets default=0
    else:
        want = next((i for i, (t, lg) in enumerate(tracks, 1)
                     if MATCH in t.lower() and (LANG is None or lg.lower() == LANG)), None)
        if want is None:
            return ('nomatch', path)
    args = ['mkvpropedit', path]
    for i in range(1, len(titles) + 1):
        args += ['--edit', f'track:s{i}', '--set', f'flag-default={1 if i == want else 0}']
    r = subprocess.run(args, capture_output=True, text=True)
    return ('ok' if r.returncode == 0 else 'fail', path)

files = []
for dirpath, _, names in os.walk(ROOT):
    for n in names:
        if n.endswith('.mkv') and not n.startswith('.'):
            files.append(os.path.join(dirpath, n))

with ThreadPoolExecutor(max_workers=8) as ex:
    res = list(ex.map(fix, files))

from collections import Counter
c = Counter(r[0] for r in res)
print(f"{ROOT}: {c['ok']} set to '{sys.argv[2]}' | nomatch={c['nomatch']} nosubs={c['nosubs']} fail={c['fail']}")
for status, p in res:
    if status in ('fail', 'nomatch'):
        print(f"   {status}: {os.path.basename(p)}")
