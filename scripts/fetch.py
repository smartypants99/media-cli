#!/usr/bin/env python3
# fetch.py — download a movie or episodes, Premiumize-first, many finders behind it.
#
#   fetch.py tt1059905 --movie --name "The Clique (2008)" --dest DIR [--res 1080p]
#   fetch.py tt0799922 --season 4 --eps 25-27 --dest DIR [--res 1080p]
#
# WHY THIS REPLACES THE TORRENTIO-ONLY PATH (2026-08-05):
# torrentio went down (Cloudflare 522 on every request, from here AND from milan) and
# the whole pipeline stopped, because franchise-fetch.py and download-fast.js only ever
# asked torrentio. torrentio was never the downloader — it is just a lookup that turns
# an IMDb id into torrent hashes. Premiumize does the actual fetching and is the piece
# that matters, but it has NO title search, so it always needs a hash from somewhere.
#
# So: ask EVERY known finder (they're independent, one being down no longer blocks us),
# pool the hashes, then let PREMIUMIZE decide — anything already in its cache downloads
# instantly at full speed. Only if nothing is cached do we fall back to asking Premiumize
# to pull the torrent from the swarm, which is slow and can stall on poorly-seeded files.
#
# Items are processed ONE AT A TIME so each download gets the full connection.
import json, os, re, subprocess, sys, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from keystore import load_key as _load_key
PM = os.environ.get('PM_KEY') or _load_key('premiumize') or ''
if not PM:
    print('No Premiumize key configured. Run:  media keys set premiumize', file=sys.stderr)

# Finders: every Stremio addon the project knows about (from no-buffer/config.js) plus
# apibay. Each is queried independently; failures are ignored, not fatal.
ADDONS = [
    'https://torrentsdb.com',
    'https://torrentio.strem.fun',
    'https://mediafusion.elfhosted.com',
    'https://comet.elfhosted.com',
    'https://knightcrawler.elfhosted.com',
    'https://thepiratebay-plus.strem.fun',
    'https://bt4g-stremio.elfhosted.com',
    'https://jackett-stremio.elfhosted.com',
    'https://yts-addon.strem.fun',
    'https://peerflix.mov',
    'https://stremio.torbox.app',
]

RES_TOKENS = {
    '2160p': ['2160', '4k', 'uhd'], '4k': ['2160', '4k', 'uhd'],
    '1080p': ['1080'], '720p': ['720'], '480p': ['480'],
}
# junk we never want: cam rips, and (for movies) season packs
BAD = re.compile(r'\b(cam|hdcam|ts|hdts|telesync|camrip|tsrip|predvd|hq.?cam|hdtc)\b', re.I)


def sh(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ''


def curl_json(url, timeout=20):
    out = sh(['curl', '-sL', '--max-time', str(timeout), url], timeout + 10)
    try:
        return json.loads(out)
    except Exception:
        return None


def find_candidates(imdb, season=None, episode=None):
    """Ask every finder at once -> {infohash: title}. One dead source can't block us."""
    path = f'series/{imdb}:{season}:{episode}' if season else f'movie/{imdb}'

    def q(base):
        d = curl_json(f'{base}/stream/{path}.json')
        out = {}
        for s in (d or {}).get('streams', []):
            h = s.get('infoHash')
            if not h:
                m = re.search(r'btih:([a-fA-F0-9]{40})|/([a-fA-F0-9]{40})/', s.get('url', '') or '')
                h = (m.group(1) or m.group(2)) if m else None
            if h:
                out[h.lower()] = (s.get('title') or s.get('name') or '').replace('\n', ' ')
        return out

    found = {}
    with ThreadPoolExecutor(max_workers=len(ADDONS)) as ex:
        for r in ex.map(q, ADDONS):
            found.update(r)
    # apibay is a plain torrent index (movies only — its episode naming is unreliable)
    if not season:
        meta = curl_json(f'https://v3-cinemeta.strem.io/meta/movie/{imdb}.json')
        title = ((meta or {}).get('meta') or {}).get('name', '')
        if title:
            d = curl_json('https://apibay.org/q.php?q=' + urllib.parse.quote(title))
            for x in (d or []):
                if x.get('info_hash', '').strip('0'):
                    found.setdefault(x['info_hash'].lower(), x.get('name', ''))
    return found


def pm_cached(hashes):
    """Ask Premiumize which hashes it already has. Cached = instant, full-speed."""
    ok = set()
    def one(h):
        d = curl_json(f'https://www.premiumize.me/api/cache/check?apikey={PM}&items%5B%5D={h}', 25)
        r = (d or {}).get('response') or [False]
        return h if r and r[0] else None
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, list(hashes)):
            if r: ok.add(r)
    return ok


def size_gb(t):
    g = re.search(r'(\d+(?:\.\d+)?)\s*GB', t, re.I)
    m = re.search(r'(\d+(?:\.\d+)?)\s*MB', t, re.I)
    return float(g.group(1)) if g else (float(m.group(1)) / 1024 if m else 0.0)


# Nominal frame geometry per resolution token, for estimating quality BEFORE download.
RES_DIMS = {'2160p': (3840, 2160), '4k': (3840, 2160), '1080p': (1920, 1080),
            '720p': (1280, 720), '480p': (720, 480), 'dvd': (720, 480)}


def detect_res(title):
    low = (title or '').lower()
    for tok, dims in RES_DIMS.items():
        if tok in low:
            return tok, dims
    if re.search(r'1920\s*x\s*1080', low): return '1080p', (1920, 1080)
    if re.search(r'1280\s*x\s*720', low):  return '720p', (1280, 720)
    return None, None


def est_bpp(gb, minutes, dims, fps=23.976):
    """Estimated bits-per-pixel from the ADVERTISED size, before downloading.

    This is what lets the ranker tell 'small because well encoded' apart from 'small
    because starved' — the distinction a plain GB floor cannot make, and the reason a
    78MB macroblocking Dragon Ball pack was chosen over better options (2026-08-22).
    """
    if not (gb and minutes and dims):
        return None
    w, h = dims
    return (gb * 8 * 1e9) / (minutes * 60) / (w * h * fps)


def probe_remote(link, timeout=120):
    """Read a CACHED candidate's real specs straight off the Premiumize CDN.

    Titles lie constantly: "1080p" that is really 1428x1068, releases that state no
    resolution at all (the six best Dragon Ball candidates named none), "2160p" that is
    a 1GB upscale. Guessing from the title is why a starved 78MB pack was picked and
    why a 1.8GB upscale then looked like the only 1080p option.

    Anything already cached can be probed over HTTP for a few seconds without
    downloading it, which turns every guess into a measurement: true resolution, true
    bitrate, true bits/pixel, and the AUDIO TRACK ORDER that decides playback language.
    """
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'stream=codec_type,codec_name,width,height,avg_frame_rate,pix_fmt,channels,'
             'color_transform,color_primaries:stream_tags=language',
             '-show_entries', 'format=duration,bit_rate,size', '-of', 'json', link],
            capture_output=True, text=True, timeout=timeout).stdout
        d = json.loads(out); fmt = d['format']
        v = [x for x in d['streams'] if x['codec_type'] == 'video'][0]
        w, h = v['width'], v['height']
        n, dn = (v.get('avg_frame_rate') or '0/1').split('/')
        fps = float(n) / float(dn) if float(dn) else 0
        dur = float(fmt.get('duration') or 0)
        br = float(fmt.get('bit_rate') or 0) or (float(fmt.get('size') or 0) * 8 / dur if dur else 0)
        if not (w and h and fps and br):
            return None
        auds = [x for x in d['streams'] if x['codec_type'] == 'audio']
        langs = [x.get('tags', {}).get('language', '?') for x in auds]
        pix = v.get('pix_fmt', '') or ''
        # English audio stream specifically — its codec/channels are what you will hear
        eng_i = next((i for i, l in enumerate(langs) if l in ('eng', 'en')), None)
        ea = auds[eng_i] if eng_i is not None else (auds[0] if auds else {})
        LOSSLESS = ('truehd', 'dts_hd', 'dtshd', 'flac', 'alac', 'pcm', 'mlp')
        acodec = (ea.get('codec_name') or '').lower()
        return {'w': w, 'h': h, 'fps': fps, 'dur': dur, 'bitrate': br,
                'codec': v['codec_name'], 'bpp': br / (w * h * fps), 'langs': langs,
                'pix_fmt': pix,
                'depth': 12 if '12' in pix else (10 if '10' in pix else 8),
                'hdr': bool(v.get('color_transform') or v.get('color_primaries') == 'bt2020'),
                'acodec': acodec,
                'achannels': int(ea.get('channels') or 0),
                'alossless': any(k in acodec for k in LOSSLESS),
                'eng_first': bool(langs) and langs[0] in ('eng', 'en'),
                'has_eng': any(l in ('eng', 'en') for l in langs)}
    except Exception:
        return None


def rank(cands, res, cached, min_gb=0.15, runtime_min=None):
    """Rank candidates: cached > requested resolution > SMALLEST acceptable.

    The old version gave a bonus for 0.7-8GB, which backfired: a well-encoded 1080p
    x265 anime episode at 0.46GB scored BELOW a near-source 2.3GB encode of the same
    thing, so it kept choosing the file 5x larger for no visible gain (Witch Hat
    Atelier, 2026-08-11). Size is not a quality signal — bitrate need depends entirely
    on runtime and content, and animation compresses extremely well.

    So: reject only implausibly tiny files (below min_gb, which the caller sets from
    the content type), then among equally-scored candidates take the smallest.
    """
    toks = RES_TOKENS.get(res, ['1080'])

    # --- quality floor from the candidate set itself, no calibration needed ---
    # "Best and smallest" needs a notion of ENOUGH. Absolute bits/pixel thresholds do
    # not transfer across resolution or codec (good 1080p Naruto sits at 0.0448 bpp,
    # visibly broken SD Dragon Ball HIGHER at 0.0522), so instead compare candidates
    # against each other: releases of the same episode at the same resolution should
    # cluster, and anything far below that cluster is starved. Above the floor, the
    # smallest file wins — which is what stops a 1.8GB upscale of a 1986 SD cartoon
    # from being treated as "better" than a 400MB one.
    est = {}
    for h, t in cands.items():
        _r, dims = detect_res(t)
        e = est_bpp(size_gb(t), runtime_min, dims)
        if e:
            est[h] = (_r, e)
    floor_by_res = {}
    for _r in {v[0] for v in est.values()}:
        vals = sorted(e for r2, e in est.values() if r2 == _r)
        if len(vals) >= 3:
            floor_by_res[_r] = vals[len(vals) // 2] * 0.60   # 60% of the median

    scored = []
    for h, t in cands.items():
        low = t.lower()
        if BAD.search(low): continue
        gb = size_gb(t)
        if gb and gb < min_gb: continue                # too small to be real
        if h in est:
            _r, e = est[h]
            fl = floor_by_res.get(_r)
            if fl and e < fl:
                continue                               # bitrate-starved for its resolution
        s = 0
        if h in cached: s += 1000                      # cached beats everything
        if any(k in low for k in toks): s += 100       # requested resolution
        # English is non-negotiable for this library, so an advertised dual-audio/dub
        # release outranks a smaller sub-only one. Picking purely by size grabbed a
        # 332MB Japanese-only encode (2026-08-11).
        if re.search(r'dual[\s._-]?audio|multi[\s._-]?audio|\bdub\b|\bdual\b|\beng\b', low):
            s += 300
        # unknown size sorts last among equals rather than winning by looking "smallest"
        scored.append((s, gb if gb else 99.0, h, t))
    scored.sort(key=lambda x: (-x[0], x[1]))           # best score, then SMALLEST
    return scored


# --- quality tiers -----------------------------------------------------------------
# Minimum bits-per-pixel to accept, per resolution tier. Derived from THIS library
# (1285 clean files, 2026-08-25): hd1080 median 0.0441, clean minimum 0.0179.
#   small    = above the clean minimum with margin. Cheapest thing that isn't starved.
#   balanced = comfortably above the library median. Looks right, without paying for
#              a near-source encode. This is the default.
#   best     = highest bpp available, whatever it costs.
# Measured effect on Steven Universe S05E23: best 1.43GB, balanced 0.67GB (53% smaller
# and still 2.2x better than the 0.0353 bpp file it replaced), small 0.23GB.
QUALITY_BPP = {
    'small':    {'sd': 0.08, 'hd720': 0.030, 'hd1080': 0.025, 'uhd': 0.045},
    'balanced': {'sd': 0.20, 'hd720': 0.075, 'hd1080': 0.065, 'uhd': 0.090},
    'best':     None,          # no floor; take the highest bpp on offer
}


def _tier(w, h):
    s = min(w, h)
    return 'sd' if s <= 576 else 'hd720' if s <= 720 else 'hd1080' if s <= 1088 else 'uhd'


def select_release(cands, cached, season, lo, hi, res='1080p', quality='balanced',
                   depth='any', audio='any', probe_max=8):
    """Pick a release by PROBING the real episode file inside each candidate.

    Ranking on advertised torrent size is wrong and cost us a good file: a 1.33GB season
    pack looked "bigger" than a 0.28GB single episode when per-episode it was 1.43GB vs
    0.30GB. The advertised number describes the torrent, not the episode. Probing over
    the CDN costs a few seconds and needs no download, so do it once per season.

    -> (infohash, title, info) or (None, None, None)
    """
    floors = QUALITY_BPP.get(quality)
    ranked = [r for r in rank(cands, res, cached) if r[2] in cached][:probe_max]
    scored = []
    for _s, _gb, h, t in ranked:
        content = directdl(h)
        if not content:
            continue
        # the release must hold the WHOLE run, else season-locking is pointless
        if not all(pick_file(content, season, e) for e in range(lo, hi + 1)):
            continue
        pick = pick_file(content, season, lo)
        link = pick.get('link') or pick.get('stream_link')
        info = probe_remote(link) if link else None
        if not info or not info['has_eng']:
            continue
        info['tier'] = _tier(info['w'], info['h'])
        info['gb'] = int(pick.get('size', 0)) / 1e9
        scored.append((h, t, info))
    if not scored:
        return None, None, None

    # --- hard requirements: depth and audio -----------------------------------------
    # Applied as FILTERS, not score bonuses. A bonus lets a great-on-paper file win on
    # one attribute while being terrible overall (a 10-bit bonus once beat an 8x higher
    # bitrate). If a requirement cannot be met we say so and fall back, rather than
    # silently returning nothing or silently ignoring what was asked for.
    def _want(x):
        i = x[2]
        if depth == '10' and i['depth'] < 10:
            return False
        if audio == 'lossless' and not i['alossless']:
            return False
        if audio == 'surround' and i['achannels'] < 6:
            return False
        return True

    if depth != 'any' or audio != 'any':
        strict = [x for x in scored if _want(x)]
        if strict:
            # Say so when a requirement COSTS you picture quality. Asking for 10-bit on
            # a title whose only 10-bit release is 1074kbps gets you 10-bit at an 8x
            # bitrate penalty — correct, but you should not discover that later.
            best_any = max(x[2]['bpp'] for x in scored)
            best_req = max(x[2]['bpp'] for x in strict)
            if best_req < best_any * 0.75:
                print(f"    WARNING: depth={depth} audio={audio} costs picture quality — "
                      f"best meeting it is {best_req:.4f} bpp vs {best_any:.4f} unconstrained "
                      f"({best_any/best_req:.1f}x). Drop the requirement for a better encode.",
                      flush=True)
            scored = strict
        else:
            have_d = sorted({x[2]['depth'] for x in scored})
            have_a = sorted({f"{x[2]['acodec']}/{x[2]['achannels']}ch" for x in scored})
            print(f"    NOTE: no candidate meets depth={depth} audio={audio}; "
                  f"available depth={have_d} audio={have_a} — using best available",
                  flush=True)

    # RESOLUTION FIRST, always. bits-per-pixel is normalised PER PIXEL, so a 720p file
    # scores higher than a 1080p one at the same bitrate despite having 2.25x fewer
    # pixels — sorting on bpp alone hands you 720p when 1080p exists (measured on
    # Steven Universe S05E23: 720p 0.1175 bpp beat 1080p 0.0793). So never compare
    # across tiers on bpp: choose the largest frame that has an acceptable candidate,
    # then rank within it.
    TIER_ORDER = {'uhd': 4, 'hd1080': 3, 'hd720': 2, 'sd': 1}

    def acceptable(x):
        return floors is None or x[2]['bpp'] >= floors.get(x[2]['tier'], 0)

    usable = [x for x in scored if acceptable(x)] or scored
    top = max(TIER_ORDER.get(x[2]['tier'], 0) for x in usable)
    pool = [x for x in usable if TIER_ORDER.get(x[2]['tier'], 0) == top]

    def extras(i):
        """Everything that is not resolution or raw bitrate but still decides how it
        looks and sounds. 10-bit is explicitly wanted (banding on gradients is the
        8-bit tell); English on track 0 is non-negotiable for this library; lossless
        and more channels are strictly better audio; HEVC/AV1 carry more real detail
        than H.264 at equal bits."""
        return ((i['depth'] >= 10) * 3.0
                + i['eng_first'] * 2.0
                + i['alossless'] * 1.5
                + min(i['achannels'], 8) * 0.15
                + i['hdr'] * 1.0
                + (i['codec'] in ('hevc', 'av1', 'vp9')) * 0.5)

    if floors is None:
        # best: bitrate DOMINATES, extras only break near-ties. Adding extras to bpp
        # directly is wrong and was measured doing real damage: a 10-bit bonus of 3.0 is
        # worth 0.3 bpp, more than the whole spread between candidates, so a 10-bit AV1
        # at 1074kbps beat an 8-bit h264 at 8426kbps. Take the top bpp, then upgrade
        # only among files within 20% of it.
        pool.sort(key=lambda x: -x[2]['bpp'])
        topbpp = pool[0][2]['bpp']
        near = [x for x in pool if x[2]['bpp'] >= topbpp * 0.80]
        h, t, i = max(near, key=lambda x: extras(x[2]))
    else:
        # balanced/small: cheapest that clears the bar, but prefer the better-equipped
        # file when two are within 15% of each other on size
        pool.sort(key=lambda x: x[2]['gb'])
        cheapest = pool[0][2]['gb']
        near = [x for x in pool if x[2]['gb'] <= cheapest * 1.15]
        h, t, i = max(near, key=lambda x: extras(x[2]))
    return h, t, i


def directdl(h):
    out = sh(['curl', '-s', '--max-time', '90', '-X', 'POST',
              'https://www.premiumize.me/api/transfer/directdl',
              '--data-urlencode', f'apikey={PM}',
              '--data-urlencode', f'src=magnet:?xt=urn:btih:{h}'], 100)
    try:
        d = json.loads(out)
    except Exception:
        return []
    return d.get('content', []) if d.get('status') == 'success' else []


def pick_file(content, season=None, episode=None):
    """Choose the right file inside the torrent.

    For a MOVIE the biggest video is correct. For an EPISODE it is NOT: many hits are
    season packs or collections, and taking the biggest file downloaded the same
    94-minute file twice, once as S04E25 and once as S04E26 (2026-08-05). An episode
    must be matched by its NUMBER in the filename, and if the torrent doesn't contain
    that episode we reject the whole candidate rather than grabbing something else.
    """
    vids = [c for c in content if c.get('path', '').lower().endswith(('.mkv', '.mp4', '.avi', '.m4v'))]
    if not vids:
        return None
    if season is None:
        return max(vids, key=lambda c: int(c.get('size', 0)))
    pats = [
        rf's0*{season}\s*e\s*0*{episode}\b',      # S04E25 / s4 e25
        rf'\b0*{season}\s*x\s*0*{episode}\b',      # 4x25
        rf'\be(?:p(?:isode)?)?\s*0*{episode}\b',   # ep25 / episode 25
        # season + 2-digit episode (425 = S04E25). Must zero-pad: building this as
        # plain '{season}{episode}' made S01E01 search for "11", which matched the
        # anime-style filename "..._-_11_..." and downloaded episode 11 instead.
        rf'[\s._-]{season}{episode:02d}[\s._-]',
        # anime packs numbered absolutely within a season: "Title - 01 (1080p)"
        rf'[-_]\s*0*{episode}\s*[\s._(\[]',
    ]
    for p in pats:
        hits = [c for c in vids if re.search(p, os.path.basename(c['path']), re.I)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:                          # ambiguous -> largest of the matches
            return max(hits, key=lambda c: int(c.get('size', 0)))
    # single-file torrent that the finder returned for this episode: trust it
    if len(vids) == 1:
        return vids[0]
    return None


def verify(path, min_minutes, max_minutes=None):
    if not os.path.exists(path) or os.path.getsize(path) < 20_000_000:
        return False, 'too small'
    d = sh(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', path], 60).strip()
    try:
        mins = float(d) / 60
    except ValueError:
        return False, 'unreadable'
    if mins < min_minutes:
        return False, f'only {mins:.1f} min'
    # An upper bound catches the wrong-file case: a 22-minute episode arriving as a
    # 94-minute file means the torrent gave us a movie or the wrong entry.
    if max_minutes and mins > max_minutes:
        return False, f'{mins:.1f} min — too long, wrong file'
    # Bitrate starvation: decodes cleanly, passes every other check, and macroblocks on
    # motion (Dragon Ball, 2026-08-22). Enforcement is a no-op until bpp-floors.json is
    # calibrated from the library, so this can never reject on uncalibrated guesses.
    try:
        import bitrate as _br
        verdict, info = _br.check(path)
        ok_tail, why = _br.tail_ok(path)
        if ok_tail is False:
            return False, f'truncated download: {why}'
        if verdict == 'reject':
            return False, (f"bitrate-starved: {info['bpp']:.4f} bpp "
                           f"({info['bitrate']/1000:.0f}kbps {info['w']}x{info['h']})")
    except Exception:
        pass
    return True, f'{mins:.1f} min'


def force_english_first(path):
    """Make English audio the FIRST audio track.

    A default flag is NOT enough: VLC plays whichever audio track comes first, which
    is how a Disney WEB-DL played Mandarin and a Naruto episode played French despite
    ffprobe reporting DISPOSITION:default=1 on English. Track ORDER is what decides.

    Keeps ONLY the English audio: the user watches in English and does not want other
    dubs taking up space, and if a foreign track is ever wanted it can be pulled from
    the same release later. One track also makes it impossible for a player to choose
    wrong. Lossless remux (no re-encode).
    """
    try:
        info = json.loads(sh(['mkvmerge', '-J', path], 90) or '{}')
    except Exception:
        return False, 'unreadable'
    tracks = info.get('tracks') or []
    if not tracks:
        return False, 'unreadable'
    def lang(t):
        p = t.get('properties') or {}
        raw = (p.get('language_ietf') or p.get('language') or '').lower()
        base = re.split(r'[-_]', raw)[0]          # en-US -> en
        # releases tag English as 'en' OR 'eng' depending on the muxer; treating them
        # as different made a perfectly good dual-audio file look Japanese-only.
        return 'eng' if base in ('en', 'eng') else (base[:3] or '')
    audio = [t for t in tracks if t.get('type') == 'audio']
    if not audio:
        return False, 'no audio'
    if lang(audio[0]) == 'eng':
        # English already plays first, so there is nothing to fix. Leave the file
        # completely untouched rather than remuxing 10GB to drop a spare track —
        # the remux is lossless but still a full read+write of the whole file.
        return True, f'eng already first, untouched ({len(audio)} audio track(s))'
    if not any(lang(t) == 'eng' for t in audio):
        # Hard failure. The user watches in English only, so a Japanese-only release is
        # useless — it must be rejected and the next candidate tried, never kept.
        return False, f'{lang(audio[0])} — NO ENGLISH TRACK'
    tmp = path + '.eng.tmp.mkv'
    # -a eng keeps only the English audio; subtitles and video are untouched
    r = subprocess.run(['mkvmerge', '-o', tmp, '-a', 'eng', path],
                       capture_output=True, text=True)
    ok = os.path.exists(tmp) and os.path.getsize(tmp) > 10_000_000
    if r.returncode in (0, 1) and ok:
        # confirm the reorder actually took effect before replacing the original
        first = sh(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                    '-show_entries', 'stream_tags=language',
                    '-of', 'default=noprint_wrappers=1:nokey=1', tmp], 60).strip()
        if first == 'eng':
            os.replace(tmp, path)
            return True, 'eng only'
    if os.path.exists(tmp):
        os.unlink(tmp)
    return False, f'{lang(audio[0])} (remux failed)'


def grab(imdb, dest, name, res, season=None, episode=None,
         min_minutes=15, max_minutes=None):
    os.makedirs(dest, exist_ok=True)
    label = name
    forced = os.environ.get('FORCE_INFOHASH')
    if forced:
        # Pin one release. Automatic ranking picks by title metadata, which can land on
        # a near-source encode 5x bigger than needed (Witch Hat Atelier: 2.3GB/ep vs
        # 0.46GB/ep for the same 1080p dual-audio content). When the right release is
        # already known, say so explicitly instead of re-guessing per episode.
        ranked = [(9999, 0.0, forced.lower(), 'forced release')]
        cached = {forced.lower()}
        print(f'  {label}: using pinned release {forced[:12]}…', flush=True)
    else:
        cands = find_candidates(imdb, season, episode)
        if not cands:
            print(f'  {label}: NO SOURCES FOUND', flush=True); return False
        cached = pm_cached(cands)
        # A resolution-aware floor: now that ties break toward the SMALLEST file, a
        # 1GB file mislabelled "2160p" would otherwise beat a real 4K remux.
        floor = {'4k': 3.0, '2160p': 3.0, '1080p': 0.3}.get(res, 0.15)
        if season is None:
            floor = max(floor, 0.5)          # a feature film is never 200MB
        # Runtime estimate for the bpp calculation: midpoint of the accepted window
        # when we have both bounds, otherwise the lower bound is the best guess.
        rt = ((min_minutes + max_minutes) / 2) if max_minutes else min_minutes
        ranked = [r for r in rank(cands, res, cached, min_gb=floor, runtime_min=rt)
                  if r[2] in cached]
    if not forced:
        print(f'  {label}: {len(cands)} candidates, {len(cached)} cached on Premiumize', flush=True)
    if not ranked:
        print(f'  {label}: nothing cached — skipping (uncached needs a swarm pull)', flush=True)
        return False
    # walk the ranked cached candidates until one actually yields this episode
    for score, gb, h, title in ranked[:6]:
        content = directdl(h)
        f = pick_file(content, season, episode)
        if not f:
            print(f'    skip {title[:44]} — episode not in this torrent', flush=True)
            continue
        ext = os.path.splitext(f['path'])[1] or '.mkv'
        final = os.path.join(dest, f'{name}{ext}')
        tmp = os.path.join(dest, f'.{name}.part{ext}')
        r = subprocess.run(['curl', '-sS', '--fail', '-L', '--connect-timeout', '60',
                            '--max-time', '3600', '-o', tmp, f['link']],
                           capture_output=True, text=True)
        ok, msg = verify(tmp, min_minutes, max_minutes)
        if r.returncode != 0 or not ok:
            if os.path.exists(tmp): os.unlink(tmp)
            print(f'    reject {os.path.basename(f["path"])[:40]} — {msg}', flush=True)
            continue
        aud_ok, aud = force_english_first(tmp)
        if not aud_ok:
            os.unlink(tmp)
            print(f'    reject {os.path.basename(f["path"])[:38]} — {aud}', flush=True)
            continue
        os.replace(tmp, final)
        print(f'  {label}: OK  [{msg}]  {os.path.getsize(final)/1048576:.0f} MB  audio={aud}',
              flush=True)
        return True
    print(f'  {label}: no candidate contained a valid episode file', flush=True)
    return False


def _library_root():
    try:
        import json as _j
        return _j.load(open(os.path.expanduser(
            '~/.config/media/prefs.json'))).get('library')
    except Exception:
        return None


def main():
    a = sys.argv[1:]
    # Guard the arguments HERE, so every caller is covered — the AI front end, opencode,
    # a script, or a person typing. When these checks lived only in the AI layer,
    # anything else driving the CLI bypassed them, which is backwards.
    if a and not a[0].startswith('-'):
        try:
            from argguard import guard
            fixed, note, err = guard(a, library=_library_root())
            if err:
                print(f'  {err}', file=sys.stderr); sys.exit(2)
            if note:
                print(f'  {note}', flush=True)
            a = fixed
            sys.argv = [sys.argv[0]] + a
        except ImportError:
            pass
    if not a:
        print('usage: fetch.py <imdb> [--movie --name NAME | --season N --eps 25-27] '
              '--dest DIR [--res 1080p]'); sys.exit(2)
    imdb = a[0]
    def opt(f, d=None):
        return a[a.index(f) + 1] if f in a else d
    dest = opt('--dest') or '.'
    res = opt('--res', '1080p')
    if '--movie' in a:
        grab(imdb, dest, opt('--name', imdb), res, min_minutes=int(opt('--minmin', '60')))
        return
    season = int(opt('--season', '1'))
    rng = opt('--eps')
    if not rng:
        # "--season 1" with no --eps means THE WHOLE SEASON, not episode 1. Defaulting to
        # a single episode silently gave one file when a season was asked for, which looks
        # like a broken download rather than a misunderstood flag.
        _m = curl_json(f'https://v3-cinemeta.strem.io/meta/series/{imdb}.json') or {}
        _eps = [v.get('episode') for v in (_m.get('meta') or {}).get('videos', [])
                if v.get('season') == season and v.get('episode')]
        if _eps:
            rng = f'1-{max(_eps)}'
            print(f'  no --eps given; taking the whole season: {rng} ({len(_eps)} episodes)',
                  flush=True)
        else:
            rng = '1'
            print('  no --eps given and episode list unavailable; fetching episode 1 only',
                  flush=True)
    # Lock the whole season to ONE release. Ranking each episode independently pulled
    # Witch Hat Atelier S1 from four different packs across three codecs — including a
    # VVC file VLC can't decode — with mismatched subtitle layouts (2026-08-11). A
    # season must be visually and structurally consistent, so resolve the release once
    # from the first episode and pin it for the rest.
    quality = opt('--quality', 'balanced')
    if quality not in ('best', 'balanced', 'small'):
        print(f'  unknown --quality {quality!r}; use best|balanced|small'); sys.exit(2)
    depth_req = opt('--depth', 'any')
    audio_req = opt('--audio', 'any')
    if depth_req not in ('10', '8', 'any'):
        print(f'  unknown --depth {depth_req!r}; use 10|8|any'); sys.exit(2)
    if audio_req not in ('lossless', 'surround', 'any'):
        print(f'  unknown --audio {audio_req!r}; use lossless|surround|any'); sys.exit(2)
    # --release pins an exact infohash. Without this there was no way to say "use THAT
    # one", which is why a throwaway download script kept getting written by hand.
    if opt('--release'):
        os.environ['FORCE_INFOHASH'] = opt('--release')
        print(f"  pinned release {opt('--release')[:12]}…", flush=True)
    if '--no-lock' not in a and not os.environ.get('FORCE_INFOHASH'):
        _parts = rng.split('-')
        lo0, hi0 = int(_parts[0]), int(_parts[-1])
        c0 = find_candidates(imdb, season, lo0)
        if c0:
            cached0 = pm_cached(c0)
            _h, _t, _i = select_release(c0, cached0, season, lo0, hi0, res, quality,
                                        depth=depth_req, audio=audio_req)
            if _h:
                os.environ['FORCE_INFOHASH'] = _h
                print(f"  [{quality}] locked to: {_t[:48]}", flush=True)
                print(f"    real episode file: {_i['gb']:.2f}GB {_i['w']}x{_i['h']} "
                      f"{_i['bitrate']/1000:.0f}kbps bpp={_i['bpp']:.4f} "
                      f"{'eng-first' if _i['eng_first'] else 'has-eng'}", flush=True)
    lo, hi = (rng.split('-') + [rng])[:2]
    meta = curl_json(f'https://v3-cinemeta.strem.io/meta/series/{imdb}.json') or {}
    titles = {(v['season'], v['episode']): v.get('name', '')
              for v in meta.get('meta', {}).get('videos', []) if v.get('episode')}
    sd = os.path.join(dest, f'Season {season}')
    # one at a time, so each download gets the full connection
    for e in range(int(lo), int(hi) + 1):
        t = re.sub(r'[\\/:*?"<>|]', '', titles.get((season, e), f'Episode {e}')).strip()
        nm = f'S{season:02d}E{e:02d} - {t}'
        # Must be an actual video file. Matching on the prefix alone means a leftover
        # '.part', a staged 'S02E12 ....mkv.OLD', or any stray sidecar counts as "have it"
        # and silently skips the download (2026-08-25).
        if any(x.startswith(f'S{season:02d}E{e:02d}') and not x.startswith('.')
               and x.lower().endswith(('.mkv', '.mp4', '.m4v'))
               for x in (os.listdir(sd) if os.path.isdir(sd) else [])):
            print(f'  {nm}: already have it'); continue
        grab(imdb, sd, nm, res, season, e,
             min_minutes=int(opt('--minmin', '15')),
             max_minutes=int(opt('--maxmin', '75')))


if __name__ == '__main__':
    main()
