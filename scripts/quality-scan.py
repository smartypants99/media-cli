#!/usr/bin/env python3
# quality-scan.py <dir> [--samples N] [--workers N] [--window W] [--thresh DB]
#
# Flag episodes that look SOFTER than the ones around them — i.e. the thing a
# viewer actually notices, a drop in quality partway through a series.
#
# METRIC: round-trip PSNR. Downscale each sampled frame to 1/3, scale it back, and
# measure how much changed. An image with real fine detail loses a lot in that trip
# (LOW psnr); an already-soft image survives almost unchanged (HIGH psnr). So higher
# score = softer. This beat ffmpeg's blurdetect clearly on the Naruto ground truth:
# the sharp uP BDRip scored ~36 vs the soft Anime Time encode's ~40, a ~4dB gap,
# where blurdetect's values overlapped and produced mostly false positives.
#
# COMPARISON: against a LOCAL window of neighbouring episodes, not the whole series.
# This matters. On Naruto, S01E16-E19 score 46-53 (a genuinely soft production block)
# while E20-E25 sit at 39-42. Judged globally, E16-E19 look like the "worst" episodes
# and the actual complaint — E26 at 42.9 — never stands out. Judged locally, E26 is
# +2.9dB above its immediate neighbours and gets flagged, which is exactly what the
# user saw by eye after watching E25.
#
# Sort order matters: files are processed in sorted (episode) order so the window is
# genuinely "nearby episodes".
import os, re, sys, subprocess, statistics
from concurrent.futures import ThreadPoolExecutor

ROOT = sys.argv[1]
def _opt(name, default, cast=int):
    return cast(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default
SAMPLES = _opt('--samples', 3)
WORKERS = _opt('--workers', 5)
WINDOW  = _opt('--window', 4)      # episodes either side to compare against
THRESH  = _opt('--thresh', 1.8, float)   # dB softer than neighbours before flagging

FF = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg-gpu')
if not os.path.exists(FF):
    FF = 'ffmpeg'

# scale2ref forces the upscale back to the source's exact dimensions — plain
# scale=iw*3 breaks on widths that don't divide evenly (Naruto is 1432 wide).
FILTER = ('[0:v]split=2[a][b];[b]scale=iw/3:ih/3:flags=bicubic[s];'
          '[s][a]scale2ref=flags=bicubic[up][aref];[aref][up]psnr')

def duration(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=noprint_wrappers=1:nokey=1', path],
                         capture_output=True, text=True).stdout.strip()
    try: return float(out)
    except ValueError: return 0.0

def softness(path):
    d = duration(path)
    if d < 60: return None
    lo, hi = d * 0.10, d * 0.90          # skip intro/credits
    points = [lo + (hi - lo) * i / max(1, SAMPLES - 1) for i in range(SAMPLES)]
    vals = []
    for t in points:
        r = subprocess.run([FF, '-v', 'info', '-ss', f'{t:.0f}', '-t', '5',
                            '-i', path, '-filter_complex', FILTER, '-f', 'null', '-'],
                           capture_output=True, text=True)
        m = re.findall(r'average:([0-9.]+)', r.stderr)
        if m: vals.append(float(m[-1]))
    return statistics.mean(vals) if vals else None

files = sorted(os.path.join(dp, n)
               for dp, _, ns in os.walk(ROOT) for n in ns
               if n.endswith('.mkv') and not n.startswith('.'))
if not files:
    print('no video files found'); sys.exit(1)

print(f'scanning {len(files)} files ({SAMPLES} samples each, {WORKERS} workers)...', flush=True)
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    scores = list(ex.map(softness, files))

pairs = [(f, s) for f, s in zip(files, scores) if s is not None]
flagged = []
for i, (f, s) in enumerate(pairs):
    lo, hi = max(0, i - WINDOW), min(len(pairs), i + WINDOW + 1)
    neigh = [v for j, (_, v) in enumerate(pairs[lo:hi], lo) if j != i]
    if len(neigh) < 3: continue
    base = statistics.median(neigh)
    if s - base >= THRESH:
        flagged.append((f, s, base, s - base))

print(f'\nflagging episodes >= {THRESH}dB softer than their {WINDOW}-either-side neighbours\n')
for f, s, base, d in sorted(flagged, key=lambda x: -x[3]):
    print(f'  FLAG  {os.path.basename(f)[:52]:52} {s:5.1f} vs {base:5.1f} neighbours  (+{d:.1f}dB softer)')
print(f'\n{len(flagged)} of {len(pairs)} episodes stand out from their neighbours')
if not flagged:
    print('nothing stands out — quality is consistent across the set')
print('\nNOTE: a flag means "go look at it", not proof. Extract a frame and eyeball it;'
      '\ndeliberately hazy or flat-looking scenes can raise the score on their own.')
