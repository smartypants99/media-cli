#!/usr/bin/env python3
# batch-fetch.py — download a COMPLETE season/series batch straight from Premiumize.
#
# Why this exists (learned the hard way on Naruto, 2026-08-04):
# torrentio's per-episode file mapping is unreliable for anime batch torrents. It
# mapped interviews/OVAs/recut "Kai" files into episode slots (E03 came down as an
# 8-min interview), silently skipped E01/E02 entirely, and preferred sub-only
# releases so the audio was Japanese. Never trust per-episode mapping for anime.
#
# Instead: resolve the batch torrent ONCE via Premiumize /transfer/directdl, which
# returns every file with its real name + a direct CDN link. Episode numbers are
# parsed from the filenames themselves, so mapping is exact and verifiable up front.
#
# Speed vs the old franchise-fetch path:
#   - 1 API call total instead of 220 torrentio queries (kills ~220 round-trips of
#     latency and the per-IP rate-limiting that stalled earlier runs)
#   - NO ffmpeg remux pass. The old flow re-read and re-wrote every file on the
#     drive (double I/O on a slow exFAT disk). Audio default is set with
#     mkvpropedit, a metadata-only edit that takes milliseconds.
#   - more parallel workers, so the connection stays saturated between episodes
# What's left is the raw internet transfer, which is the real floor.
#
# Usage: batch-fetch.py <config.json>
import os, time, re, sys, json, subprocess, threading
from concurrent.futures import ThreadPoolExecutor
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

CFG = json.load(open(sys.argv[1]))
PM = CFG["apikey"]
INFOHASH = CFG["infohash"]
DEST = CFG["dest"]
PATTERN = re.compile(CFG["pattern"])          # must capture (episode_number, title)
SEASONS = CFG["seasons"]                      # e.g. [35,48,48,48,41] -> absolute ep -> S/E
WORKERS = CFG.get("workers", 6)
TOTAL = sum(SEASONS)
MIN_MIN = CFG.get("min_minutes", 15)          # reject extras/fragments
WANT_LANG = CFG.get("audio", "eng")

plog = threading.Lock()
def log(m):
    with plog: print(m, flush=True)

def clean(t):
    return re.sub(r'\s+', ' ', re.sub(r'[\\/:*?"<>|]', '', t or '').strip()) or 'Episode'

def se_for(n):
    """absolute episode number -> (season, episode)"""
    start = 1
    for si, count in enumerate(SEASONS, 1):
        if n < start + count:
            return si, n - start + 1
        start += count
    return len(SEASONS), n

_links_lock = threading.Lock()

def fetch_links():
    """Resolve the batch on Premiumize -> {abs_ep: (title, url)}. Re-callable:
    directdl links expire after a few hours, and a 60GB pull can outlive them."""
    r = subprocess.run(['curl', '-s', '--max-time', '90', '-X', 'POST',
                        'https://www.premiumize.me/api/transfer/directdl',
                        '--data-urlencode', f'apikey={PM}',
                        '--data-urlencode', f'src=magnet:?xt=urn:btih:{INFOHASH}'],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
    except Exception:
        return {}
    if d.get('status') != 'success':
        log(f"  directdl failed: {d.get('message')}")
        return {}
    out = {}
    for x in d.get('content', []):
        name = x.get('path', '').split('/')[-1]
        m = PATTERN.match(name)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= TOTAL:
            out[n] = (m.group(2), x.get('link'))
    return out

LINKS = fetch_links()
if len(LINKS) < TOTAL:
    log(f"[batch] ABORT: only {len(LINKS)}/{TOTAL} episodes resolved — refusing a partial run")
    sys.exit(1)

def probe(path, entries, stream=None):
    cmd = ['ffprobe', '-v', 'error']
    if stream:
        cmd += ['-select_streams', stream]
    cmd += ['-show_entries', entries, '-of', 'default=noprint_wrappers=1:nokey=1', path]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

def set_default_audio(path):
    """Make the wanted language the default audio track so it just plays in that
    language — no digging through VLC menus. Metadata-only, no re-encode."""
    langs = probe(path, 'stream=index:stream_tags=language', 'a').split('\n')
    langs = [l for l in langs if l and not l.isdigit()]
    if WANT_LANG not in langs:
        return False
    args = ['mkvpropedit', path]
    for i, lang in enumerate(langs, 1):
        args += ['--edit', f'track:a{i}', '--set',
                 f'flag-default={1 if lang == WANT_LANG else 0}']
    subprocess.run(args, capture_output=True)
    return True

def verify(path):
    """A file is only kept if it's a real, full-length episode in the right
    language. This is exactly what was missing before — 8-minute interviews and
    Japanese-only files sailed through unnoticed."""
    if not os.path.exists(path) or os.path.getsize(path) < 50_000_000:
        return False, 'too small'
    dur = probe(path, 'format=duration')
    try:
        mins = float(dur) / 60
    except Exception:
        return False, 'unreadable'
    if mins < MIN_MIN:
        return False, f'only {mins:.1f} min — extra/fragment, not an episode'
    langs = probe(path, 'stream=index:stream_tags=language', 'a')
    if WANT_LANG not in langs:
        return False, f'no {WANT_LANG} audio'
    # Bitrate starvation — decodes cleanly, right length, right language, and still
    # macroblocks on motion (Dragon Ball, 2026-08-22). No-op until bpp-floors.json is
    # calibrated, so it cannot reject on uncalibrated guesses.
    try:
        import bitrate as _br
        v, i = _br.check(path)
        ok_tail, why = _br.tail_ok(path)
        if ok_tail is False:
            return False, f'truncated download: {why}'
        if v == 'reject':
            return False, (f"bitrate-starved: {i['bpp']:.4f} bpp "
                           f"({i['bitrate']/1000:.0f}kbps {i['w']}x{i['h']})")
    except Exception:
        pass
    return True, f'{mins:.1f} min'

def grab(n, _attempt=0):
    """Retries on mount failure.

    The FSKit exFAT mount intermittently flips read-only under concurrent writes and
    every worker then dies on PermissionError, taking the whole 153-episode run with it
    (2026-08-23, lost the run at episode 9). The mount recovers by itself within
    seconds, so a transient OSError must be waited out, never treated as a real failure.
    """
    try:
        return _grab(n)
    except OSError as e:
        if _attempt >= 4:
            log(f"  E{n:03d} FAIL (mount error after {_attempt} retries: {e})")
            return ('fail', n)
        time.sleep(15 * (_attempt + 1))
        return grab(n, _attempt + 1)


def _grab(n, _vretry=0):
    title, url = LINKS[n]
    s, e = se_for(n)
    sd = os.path.join(DEST, f"Season {s}")
    os.makedirs(sd, exist_ok=True)
    final = os.path.join(sd, f"S{s:02d}E{e:02d} - {clean(title)}.mkv")
    if os.path.exists(final) and os.path.getsize(final) > 100_000_000:
        return ('skip', n)
    tmp = os.path.join(sd, f".S{s:02d}E{e:02d}.part.mkv")
    for attempt in (1, 2):
        cr = subprocess.run(['curl', '-sS', '--fail', '-L', '--connect-timeout', '60',
                             '--max-time', '1800', '-o', tmp, url],
                            capture_output=True, text=True)
        if cr.returncode == 0:
            break
        # links go stale on long runs — refresh the whole list once and retry
        if attempt == 1:
            with _links_lock:
                fresh = fetch_links()
                if fresh:
                    LINKS.update(fresh)
            url = LINKS[n][1]
    ok, msg = verify(tmp)
    if not ok:
        try: os.unlink(tmp)
        except OSError: pass
        # A verify failure is often transient, not a bad source: when the exFAT mount
        # stalls mid-write the file lands short and reports "too small" (E008,
        # 2026-08-23). Retry once before writing the episode off, otherwise a healthy
        # 676MB source is abandoned over a momentary mount hiccup.
        if _vretry < 1:
            log(f"  E{n:03d} retry ({msg})")
            time.sleep(10)
            return _grab(n, _vretry + 1)
        log(f"  E{n:03d} FAIL ({msg})")
        return ('fail', n)
    set_default_audio(tmp)
    os.replace(tmp, final)
    log(f"  S{s:02d}E{e:02d} OK  {clean(title)[:44]}  [{msg}]")
    return ('ok', n)

log(f"[batch] {TOTAL} episodes | {WORKERS} workers | direct Premiumize links, no remux")
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    res = list(ex.map(grab, range(1, TOTAL + 1)))
ok = sum(1 for r in res if r[0] == 'ok')
sk = sum(1 for r in res if r[0] == 'skip')
bad = [r[1] for r in res if r[0] == 'fail']
log(f"\nDONE ok={ok} skip={sk} failed={len(bad)}")
if bad:
    log("FAILED episodes: " + str(bad))
