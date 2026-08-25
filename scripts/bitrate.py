#!/usr/bin/env python3
"""Bits-per-pixel measurement — the check that catches a starved encode.

WHY THIS EXISTS (2026-08-22)
Dragon Ball S01E02 macroblocks badly on motion at ~15:43. The file decodes with ZERO
ffmpeg errors, and `blurdetect` rates the corrupt window 4.97 against 5.02 for a clean
one — marginally SHARPER, because block edges are hard edges. Every check we had (size
floor, duration bounds, audio track order, resolution, decode integrity) is blind to
this by construction. The cause is not damage, it is bitrate starvation: the encoder
runs out of bits on the only moving object in an otherwise static frame.

WHY A FLAT THRESHOLD DOES NOT WORK
The obvious fix — "reject below N bits/pixel" — is wrong, and the library proves it:

    Naruto S03E01    HEVC 1432x1080   1661 kbps  ->  0.0448 bpp   (looks fine)
    Dragon Ball S01E02  AV1  708x480   425 kbps  ->  0.0522 bpp   (visibly broken)

The known-GOOD file scores LOWER than the known-BAD one. Required bpp falls steeply
with resolution — a big frame has far more spatial redundancy to exploit — and codec
generation shifts it again. So thresholds MUST be per-resolution-tier and calibrated
against this library's own distribution, never taken from published rules of thumb.

MEASUREMENT DISCIPLINE
Always container-level bitrate (format=bit_rate), never per-stream. Per-stream bit_rate
is usually N/A in mkv, so mixing the two silently moves files across the threshold.
Container bitrate includes audio and subs; that is fine as long as EVERY consumer
(scan, calibration, verify, rank) measures the same quantity.
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
FLOORS_PATH = os.path.join(HERE, 'bpp-floors.json')

# Tier by frame HEIGHT. Keep the boundaries generous: anime is routinely cropped to
# odd heights (1432x1080, 708x480) and must not fall into the tier below.
def tier(h, w=None):
    # Tier on the SHORT side: tiering by height alone files 1080x1920 portrait video
    # as 'uhd' and compares it to floors calibrated for 3840-wide frames.
    if w:
        h = min(w, h)
    if h <= 576:  return 'sd'
    if h <= 720:  return 'hd720'
    if h <= 1088: return 'hd1080'
    return 'uhd'


def load_floors():
    """Per-tier reject/warn thresholds. Absent or empty => enforcement OFF.

    Deliberately starts empty: enforcing uncalibrated numbers would reject the
    working library. Populate with `bitrate.py --calibrate` after a full scan.
    """
    try:
        with open(FLOORS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def probe(path):
    """-> dict(width, height, fps, dur, bitrate, codec, bpp) or None."""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,avg_frame_rate,codec_name',
             '-show_entries', 'format=duration,bit_rate,size',
             '-of', 'json', path], capture_output=True, text=True, timeout=120).stdout
        d = json.loads(out)
        st, fmt = d['streams'][0], d['format']
        w, h = int(st['width']), int(st['height'])
        num, den = (st.get('avg_frame_rate') or '0/1').split('/')
        fps = float(num) / float(den) if float(den) else 0.0
        dur = float(fmt.get('duration') or 0)
        # container bitrate; fall back to size/duration when the tag is missing
        br = float(fmt.get('bit_rate') or 0)
        if not br and dur:
            br = float(fmt.get('size') or os.path.getsize(path)) * 8 / dur
        if not (w and h and fps and br):
            return None
        return {'path': path, 'w': w, 'h': h, 'fps': fps, 'dur': dur,
                'bitrate': br, 'codec': st.get('codec_name', '?'),
                'tier': tier(h, w), 'bpp': br / (w * h * fps)}
    except Exception:
        return None


def check(path):
    """-> (verdict, info). verdict in ok|warn|reject|unreadable.

    Returns 'ok' when no floor is configured for the tier: an uncalibrated tier must
    never block a download.
    """
    info = probe(path)
    if not info:
        return 'unreadable', None
    f = load_floors().get(info['tier'])
    if not f:
        return 'ok', info
    if info['bpp'] < f.get('reject', 0):
        return 'reject', info
    if info['bpp'] < f.get('warn', 0):
        return 'warn', info
    return 'ok', info



def tail_ok(path, dur=None):
    """Can the LAST few seconds actually be decoded?

    This is what caught Gravity Falls S01E17 and Fresh Off the Boat S01E09: both are
    truncated downloads, and both sailed through every previous check because the
    matroska HEADER still advertises the full runtime. Duration checks read that
    header, so a half-downloaded file reports 21.6 min and passes. Only decoding near
    the end reveals "File ended prematurely".

    Cheap: one seek + one frame, ~1s, versus a full decode.
    """
    if dur is None:
        info = probe(path)
        dur = info['dur'] if info else 0
    if not dur:
        # No probeable duration at all: no video stream, or an unreadable header. That is
        # NOT "a short clip" — it is unknown, and must not be reported as healthy.
        return None, 'no duration — nothing to check'
    if dur < 30:
        # A genuinely short file is NOT a failure. Now that default mode tail-checks every
        # file, returning False here would dump every trailer, featurette and 24s extra
        # into the TRUNCATED list on each full-library scan.
        return True, 'too short to check'
    # Retry once before believing a failure. The exFAT/FSKit mount stalls under
    # concurrent seek-heavy reads and returns "Device not configured" / "No such file"
    # for files that are perfectly fine — a parallel sweep reported 326 broken files
    # when only 2 were real (2026-08-22). A transient mount error must never be
    # reported as a corrupt file.
    #
    # We test BEHAVIOUR, not stderr prose: decode one frame near the end and require
    # actual pixels back. ffmpeg exits 0 with EMPTY stderr when it decodes ZERO frames
    # (verified: seeking past EOF on a healthy file gives rc=0, no output), so an
    # exit-code-plus-substring check both misses clean truncations and false-positives
    # on benign trailing bytes. Bytes on stdout is the only honest signal.
    err = ''
    for attempt in (0, 1):
        try:
            r = subprocess.run(
                ['ffmpeg', '-hide_banner', '-v', 'error', '-ss', str(max(0, dur - 12)),
                 '-i', path, '-map', '0:v:0', '-frames:v', '1',
                 '-vf', 'scale=64:64', '-f', 'rawvideo', '-'],
                capture_output=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            # A hung read is a stalled mount, not a corrupt file — and tail checks are
            # the seek-heaviest thing here. Letting this escape kills the whole scan
            # inside ex.map and loses every result.
            err = f'transient: {type(e).__name__}'
            if attempt == 0:
                time.sleep(2)
                continue
            return None, err          # None = unknown, distinct from False = broken
        if len(r.stdout) > 0:
            return True, 'ok'
        err = (r.stderr or b'').decode('utf-8', 'replace').strip()
        if attempt == 0:
            time.sleep(0.5)
    return False, (err.split('\n')[0][:70] or 'no frame decoded near end')


# Parser chatter that does NOT mean the file is damaged. Every Dragon Ball episode emits
# the Opus line, and all 153 decode and play perfectly — treating stderr as a verdict
# without this filter condemns the entire library.
BENIGN_ERR = (
    'error parsing opus packet header',
    # null-muxer artifact on B-frame streams, NOT corruption. This one has now faked
    # "corrupt" verdicts TWICE: 120 files in an ad-hoc Beyblade sweep, and Gravity Falls
    # S02E17 in the 2026-08-25 library sweep (which decodes perfectly).
    'non monotonically increasing dts',
    # carries no information of its own — if the repeated line were a real error, that
    # line itself is still present and will trigger. Without this, filtering the DTS
    # message above still leaves "Last message repeated 38185 times" reading as an error.
    'last message repeated',
    'could not update timestamps for skipped samples',
    'could not update timestamps for discarded samples',
)


def _real_errors(err):
    return [l for l in err.split('\n')
            if l.strip() and not any(b in l.lower() for b in BENIGN_ERR)]


def demux_ok(path, timeout=900):
    """Read EVERY byte, checking container integrity end to end — without decoding.

    Measured 273x realtime vs 30x for a full decode of the same file, and it reads
    SEQUENTIALLY (~4 MB/s across 6 workers) — the access pattern verified safe on the
    FSKit exFAT mount, unlike seek-heavy parallel reads which once produced 326 phantom
    failures.

    Catches truncation and container damage ANYWHERE in the file, not just the last 12
    seconds like tail_ok. It does NOT catch bitstream garbage inside intact packets
    (needs a full decode), and it CANNOT catch bitrate starvation at all — starved video
    demuxes and decodes perfectly, it just looks bad. Only bpp floors catch that.

    NOTE: `-xerror` does NOT work here. Measured: a half-truncated mkv prints
    "File ended prematurely" and still exits **0**. The return code carries no
    information, so the verdict comes from stderr being empty or not — a clean demux at
    `-v error` prints nothing at all.

    -> (True|False|None, detail). None = could not verify (transient), never 'broken'.
    """
    err = ''
    for attempt in (0, 1):
        try:
            r = subprocess.run(
                ['ffmpeg', '-hide_banner', '-nostats', '-v', 'error',
                 '-i', path, '-map', '0', '-dn', '-sn', '-c', 'copy', '-f', 'null', '-'],
                capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            err = f'transient: {type(e).__name__}'
            if attempt == 0:
                time.sleep(2); continue
            return None, err
        err = (r.stderr or b'').decode('utf-8', 'replace').strip()
        real = _real_errors(err)
        if not real:
            return True, 'ok'
        err = '\n'.join(real)
        low = err.lower()
        # a stalled mount is not a corrupt file
        if 'device not configured' in low or 'no such file' in low or 'input/output error' in low:
            if attempt == 0:
                time.sleep(2); continue
            return None, 'transient: mount stall'
        if attempt == 0:
            time.sleep(0.5)
    return False, (err.split('\n')[0][:80] or f'demux failed rc={r.returncode}')


def coverage(path, dur=None):
    """Fraction of the advertised duration that actually has packets.

    The cleanest truncation signal found: a machine-readable NUMBER rather than matched
    stderr prose. Measured on a half-truncated mkv whose header still claimed the full
    runtime: healthy 0.999, truncated 0.499.

    Costs a full packet enumeration, which is ~9-14s for a TV episode but TIMED OUT past
    300s on a 2-hour 4K remux — so callers should gate it, not run it on everything.
    """
    if dur is None:
        i = probe(path)
        dur = i['dur'] if i else 0
    if not dur:
        return None
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'packet=pts_time', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=600).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    last = 0.0
    for line in out.split('\n'):
        v = line.strip().rstrip(',')
        try:
            last = max(last, float(v))
        except ValueError:
            pass
    return last / dur if dur else None


def relative_outliers(rows, ratio=0.45):
    """Flag files far thinner than their OWN season, rather than an absolute floor.

    Absolute thresholds cannot work across this library: good 1080p Naruto sits at
    0.0448 bpp while visibly-broken SD Dragon Ball sits HIGHER at 0.0522. Required
    bits/pixel depends on resolution, codec and content, none of which a single number
    captures. But within one season — same release, same encoder, same source — the
    files should be consistent, so a big relative gap is a real signal and needs no
    calibration at all.

    Gravity Falls S01E17 is 10x thinner than its siblings; Fresh Off the Boat S01E09
    is 6.5x thinner. Both truncated. That separation is unmissable relatively and
    invisible absolutely.
    """
    groups, by_tier = {}, {}
    for r in rows:
        # Key on (dir, tier): a folder holding 2 UHD rips and 3 SD rips would otherwise
        # take one cross-tier median and flag every SD file as "8x thinner than its season".
        groups.setdefault((os.path.dirname(r['path']), r['tier']), []).append(r)
        by_tier.setdefault(r['tier'], []).append(r)
    # Global per-tier medians, so a group too small to self-compare is still covered.
    # Without this, 3 odd-resolution episodes dropped into a 150-episode season form
    # their own group of 3, fall under the minimum, and become invisible to the check —
    # which is exactly the "one bad episode in the pack" case this exists to catch.
    # Fallback is the SHOW (parent of the season dir) at the same tier — never a global
    # median. A library-wide SD median is meaningless: Dragon Ball's 153 unusually
    # generous files (0.42 bpp) dominated it and false-flagged Adventure Time's 640x360
    # episodes at 0.066 as "6.4x thin" when they are simply a different, lower-bitrate
    # show. Like-for-like or not at all.
    show_med = {}
    _by_show = {}
    for r in rows:
        show = os.path.dirname(os.path.dirname(r['path']))   # .../Show/Season N/file
        _by_show.setdefault((show, r['tier']), []).append(r)
    for k, grp in _by_show.items():
        v = sorted(x['bpp'] for x in grp)
        if len(v) >= 4:
            show_med[k] = v[len(v) // 2]
    flagged = []
    for (d, t), grp in groups.items():
        if len(grp) >= 4:
            vals = sorted(x['bpp'] for x in grp)
            med = vals[len(vals) // 2]
        elif (os.path.dirname(d), t) in show_med:
            med = show_med[(os.path.dirname(d), t)]   # other seasons of the SAME show
        else:
            continue                     # genuinely nothing to compare against
        for r in grp:
            if r['bpp'] < med * ratio:
                flagged.append((r, med, r['bpp'] / med))
    flagged.sort(key=lambda x: x[2])
    return flagged

def walk(root):
    out = []
    for dp, _, names in os.walk(root):
        for n in names:
            if n.lower().endswith(('.mkv', '.mp4', '.m4v')) and not n.startswith('.'):
                out.append(os.path.join(dp, n))
    return sorted(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    calibrate = '--calibrate' in sys.argv
    truncheck = '--no-trunc' not in sys.argv
    flaggedonly = '--flagged-only' in sys.argv  # fast path: check only bpp outliers
    quick       = '--quick' in sys.argv         # 12s tail check instead of full demux sweep
    if not args:
        print('usage: bitrate.py <dir-or-file> [...] [--calibrate] [--quick] [--flagged-only] [--no-trunc]'); sys.exit(2)

    files = []
    for a in args:
        files += walk(a) if os.path.isdir(a) else [a]
    if not files:
        print('no video files found'); sys.exit(1)

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for r in ex.map(probe, files) if r]
    bad = len(files) - len(rows)

    floors = load_floors()
    by_tier = {}
    for r in rows:
        by_tier.setdefault(r['tier'], []).append(r)

    for t in ('sd', 'hd720', 'hd1080', 'uhd'):
        grp = by_tier.get(t)
        if not grp:
            continue
        grp.sort(key=lambda r: r['bpp'])
        vals = [r['bpp'] for r in grp]
        med = vals[len(vals) // 2]
        f = floors.get(t) or {}
        print(f"\n=== {t}  ({len(grp)} files)  median {med:.4f} bpp"
              f"  range {vals[0]:.4f}-{vals[-1]:.4f}"
              + (f"  [reject<{f.get('reject',0):.4f} warn<{f.get('warn',0):.4f}]" if f else "  [no floor set]")
              + " ===")
        for r in grp[:12]:
            mark = ''
            if f:
                mark = ' REJECT' if r['bpp'] < f.get('reject', 0) else (
                       ' warn' if r['bpp'] < f.get('warn', 0) else '')
            print(f"  {r['bpp']:.4f}  {r['codec']:>5} {r['w']}x{r['h']}"
                  f" {r['bitrate']/1000:7.0f}kbps  {os.path.basename(r['path'])[:64]}{mark}")
        if len(grp) > 12:
            print(f"  ... {len(grp)-12} more, up to {vals[-1]:.4f}")

    if bad:
        # Name them. A file whose v:0 is cover art reports avg_frame_rate 0/0, probe()
        # returns None, and it then appears in NO tier table, NO outlier test and NO tail
        # check — a truncated mp4 can hide there. A bare count made them invisible.
        readable = {r['path'] for r in rows}
        print(f"\n{bad} file(s) UNREADABLE — not checked by anything, inspect manually:")
        for f in files:
            if f not in readable:
                print(f"  {os.path.relpath(f, args[0] if os.path.isdir(args[0]) else '/')}")

    # --- relative outliers: the check that needs no calibration ---
    print("\n" + "=" * 70)
    flagged = relative_outliers(rows)
    if not flagged:
        print("no episode is anomalously thin versus its own season")
    else:
        print(f"{len(flagged)} file(s) far thinner than their own season:")
        for r, med, frac in flagged:
            rel = os.path.relpath(r['path'], args[0] if os.path.isdir(args[0]) else '/')
            print(f"  {1/frac:5.1f}x thinner  {r['bpp']:.4f} vs {med:.4f} median"
                  f"  {r['bitrate']/1000:6.0f}kbps  {rel}")

    # --- truncation: header duration lies, so decode near the end ---
    if truncheck:
        # Tail-check EVERY file. The earlier version checked only bpp outliers whenever
        # any file was flagged — which skipped exactly the failure this tool exists to
        # catch: a truncation whose bitrate still looks normal (Fresh Off the Boat S01E09
        # sat near its season median and was half-downloaded). --flagged-only restores
        # the old fast path deliberately.
        targets = [r for r, _, _ in flagged] if (flagged and flaggedonly) else rows
        print(f"\n{'tail-checking' if quick else 'full demux sweep of'} {len(targets)} file(s)...", flush=True)
        # 3 workers, not 6. MEASURED 2026-08-25: a 6-wide demux sweep of 1294 files
        # stalled the FSKit mount 28 times, leaving those files unverifiable. Sequential
        # reads are gentler than seeks but NOT unlimited — the mount is the constraint,
        # not the access pattern. Full sweep takes ~80 min either way; it is I/O-bound on
        # total library bytes, not on per-file overhead.
        with ThreadPoolExecutor(max_workers=3) as ex:
            res = list(ex.map(lambda r: (r, (tail_ok(r['path'], r['dur'])
                                             if quick else demux_ok(r['path']))), targets))
        broken   = [(r, m) for r, (o, m) in res if o is False]
        unknown  = [(r, m) for r, (o, m) in res if o is None]
        if unknown:
            print(f"\n{len(unknown)} file(s) COULD NOT BE CHECKED (mount stall, retry later):")
            for r, m in unknown:
                print(f"  {os.path.basename(r['path'])}  — {m}")
        if broken:
            print(f"\n{len(broken)} CORRUPT / TRUNCATED:")
            for r, m in broken:
                print(f"  {os.path.relpath(r['path'], args[0] if os.path.isdir(args[0]) else '/')}"
                      f"\n      {m}")
        else:
            print("  all files intact end to end")

    if calibrate:
        # Floors are derived from THIS library, not from published figures. reject is
        # set below the observed good corpus so calibrating on a clean library cannot
        # condemn it; anything meaningfully thinner than everything you own is suspect.
        # Derive floors from a ROBUST statistic. Using min*0.8 poisons the floor with the
        # very files you are hunting: calibrating on this library while Gravity Falls S01E17
        # (1/10th of its season) was still present would have set the sd floor around
        # 0.008 bpp — permanently useless, and printed as authoritative JSON.
        bad_paths = {r['path'] for r, _, _ in relative_outliers(rows)}
        out = {}
        for t, grp in by_tier.items():
            clean = [r['bpp'] for r in grp if r['path'] not in bad_paths] or [r['bpp'] for r in grp]
            vals = sorted(clean)
            # int(n*0.05) is 0 for every n <= 19 — i.e. the bare minimum, the exact
            # statistic this was meant to avoid, and movie tiers are the small ones.
            # Below 20 samples a percentile is meaningless; anchor on the median instead.
            if len(vals) >= 20:
                p05 = vals[int(len(vals) * 0.05)]
            else:
                p05 = vals[len(vals) // 2] * 0.60
            out[t] = {'reject': round(p05 * 0.80, 4), 'warn': round(p05 * 0.95, 4),
                      'n': len(vals), 'dropped_outliers': len(grp) - len(clean),
                      'p05': round(p05, 4),
                      'observed_median': round(vals[len(vals)//2], 4)}
        print('\nproposed floors (review before saving):')
        print(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
