#!/usr/bin/env python3
# Fetch Tales-of-Arcadia saga in the iPad-VLC-confirmed format: raw 10-bit HEVC
# video (copy, no transcode) + first audio (copy) + all subtitle tracks (copy).
# Reads JOBS from a JSON config file passed as argv[1] so the same script runs
# the TD half and the USB half. Skips episodes already present & valid.
import os, re, json, sys, subprocess
from concurrent.futures import ThreadPoolExecutor
import threading

PM = ""
# args: <config.json> [--res 1080p|1440p|4k|720p|...]  (--res overrides config "res"; default 1080p)
_ARGV = sys.argv[1:]
def _arg(flag):
    return _ARGV[_ARGV.index(flag)+1] if flag in _ARGV and _ARGV.index(flag)+1 < len(_ARGV) else None
_cfg_path = next(a for i, a in enumerate(_ARGV)
                 if not a.startswith('--') and (i == 0 or _ARGV[i-1] != '--res'))
CFG = json.load(open(_cfg_path))
JOBS = CFG["jobs"]          # list of {imdb, kind:'series'|'movie', dest, seasons:{s:[start,end]}, name}
WORKERS = CFG.get("workers", 5)
RES = (_arg('--res') or CFG.get('res') or '1080p')

def res_spec(res):
    # -> (title tokens, qualityfilter EXCLUDES, size cap, MIN size GB). Default 1080p.
    # min size guards against streams mislabeled at a higher res than they really are
    # (e.g. a 3.9GB file tagged "2160p" that's actually 1080p).
    r = str(res).lower().replace('p', '').strip()
    if r in ('2160', '4k', 'uhd'):  return (['2160', '4k', 'uhd'], 'threed,480p,scr,cam,unknown', '30GB', 6.0)
    if r in ('1440', '2k'):         return (['1440'],              'threed,480p,scr,cam,unknown', '20GB', 2.5)
    if r == '720':                  return (['720'],               'threed,480p,4k,scr,cam,unknown', '4GB', 0.0)
    return (['1080'], 'threed,480p,720p,4k,scr,cam,unknown', '6GB', 0.0)   # default & fallback

plog = threading.Lock()
def log(m):
    with plog: print(m, flush=True)

def sh_json(url):
    # Prefer a DIRECT curl (works when the laptop can reach torrentio itself, e.g.
    # on the VPN); fall back to proxying through milan if the direct call fails or
    # returns nothing. Lets downloads keep working when milan is offline.
    try:
        r = subprocess.run(['curl','-s','--max-time','30', url], capture_output=True, text=True)
        if r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    r = subprocess.run(['ssh','-o','ConnectTimeout=15','milan', f"curl -s --max-time 30 '{url}'"],
                       capture_output=True, text=True)
    try: return json.loads(r.stdout)
    except Exception: return None

def cinemeta_titles(imdb):
    m = sh_json(f"https://v3-cinemeta.strem.io/meta/series/{imdb}.json")
    d = {}
    for v in (m or {}).get('meta', {}).get('videos', []):
        if v.get('season') and v.get('episode'):
            d[(v['season'], v['episode'])] = v.get('name') or f"Episode {v['episode']}"
    return d

HDR_RX = re.compile(r'\bhdr10?\+?\b|dolby.?vision|\bdo?vi\b|\bdv\b|\bhlg\b|\bpq\b', re.I)

def pick_best(streams, tokens, bad_rx=None, minsize=0.0, prefer_sdr=False):
    # Only pick streams already CACHED on Premiumize (torrentio marks them
    # '[PM+]'). Uncached '[PM download]' streams return a ~1.5MB placeholder clip
    # and fail. If nothing's cached yet, return None so the caller retries later.
    # `tokens` are the resolution strings to match in the title (e.g. ['1080'] or
    # ['2160','4k','uhd']). bad_rx drops mislabeled uploads. prefer_sdr: when a 4K
    # SDR exists, take it over HDR (HDR looks dark in plain VLC).
    cands = []
    for s in streams:
        t = (s.get('title') or '').lower()
        name = s.get('name') or ''
        if not s.get('url'): continue
        if '[PM+]' not in name:  # skip uncached
            continue
        if bad_rx and bad_rx.search(t): continue
        if 'upscal' in t: continue                      # skip fake "4K" upscaled from lower res
        if not any(tok in t for tok in tokens): continue
        g = re.search(r'(\d+(?:\.\d+)?)\s*gb', t); mb = re.search(r'(\d+(?:\.\d+)?)\s*mb', t)
        gb = float(g.group(1)) if g else (float(mb.group(1))/1000 if mb else 99)
        if minsize and gb < minsize: continue           # too small to really be this res (mislabeled)
        hevc = bool(re.search(r'x265|hevc|h\.?265', t)); tenbit = ('10bit' in t) or ('10-bit' in t)
        is_sdr = not HDR_RX.search(t)
        cands.append((s['url'], gb, hevc, tenbit, is_sdr))
    if not cands: return None
    if prefer_sdr:
        sdr = [c for c in cands if c[4]]
        if sdr: cands = sdr        # only keep SDR when any SDR at this res exists
    pool = [c for c in cands if c[2] and c[3]] or [c for c in cands if c[2]] or cands
    return min(pool, key=lambda c: c[1])

def clean(t): return re.sub(r'\s+',' ', re.sub(r'[\\/:*?"<>|]','',t or '').strip()) or 'Episode'

def valid_existing(path):
    if not (os.path.exists(path) and os.path.getsize(path) > 20_000_000): return False
    d = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',path],
                       capture_output=True, text=True).stdout.strip()
    try: return float(d) > 300
    except Exception: return False

def find_existing(sd, prefix):
    if not os.path.isdir(sd): return None
    for f in os.listdir(sd):
        if f.startswith(prefix) and f.endswith('.mkv') and not f.startswith('.'):
            return os.path.join(sd, f)
    return None

def fetch(kind, imdb, s, e, title, dest):
    if kind == 'movie':
        sd = dest; os.makedirs(sd, exist_ok=True)
        # prefix must be the FULL clean title, not [:8] — in a collection folder
        # ("Despicable Me 1/2/3/4", "Minions ..."), an 8-char prefix collides and
        # falsely "skips" later films by matching an earlier film's file.
        final = f"{sd}/{clean(title)}.mkv"; prefix = clean(title)
        subpath = f"movie/{imdb}"
    else:
        sd = f"{dest}/Season {s}"; os.makedirs(sd, exist_ok=True)
        final = f"{sd}/S{s:02d}E{e:02d} - {clean(title)}.mkv"; prefix = f"S{s:02d}E{e:02d}"
        subpath = f"series/{imdb}:{s}:{e}"
    ex = find_existing(sd, prefix)
    if not CFG.get('force') and ex and valid_existing(ex): return ('skip', s, e)
    tokens, qf, maxsize, minsize = res_spec(RES)
    maxsize = CFG.get('maxsize', maxsize)   # optional per-config override
    filt = (f"sort=qualitysize%7Cqualityfilter={qf}"
            f"%7Climit=30%7Csizefilter={maxsize}%7Cpremiumize={PM}")
    d = sh_json(f"https://torrentio.strem.fun/{filt}/stream/{subpath}.json")
    streams = (d or {}).get('streams', [])
    prefer_sdr = bool(CFG.get('prefer_sdr'))
    # for movies: skip mislabeled uploads (series packs, wrong-title combos) AND cam/telesync junk
    bad_rx = re.compile(r'complete series|complete\.series|season|s0\d|korra|episodes|'
                        r'\b(cam|hdcam|ts|hdts|telesync|camrip|tsrip|predvd|hq.?pre|hqcam)\b', re.I) if kind == 'movie' else None
    pick = pick_best(streams, tokens, bad_rx=bad_rx, minsize=minsize, prefer_sdr=prefer_sdr)
    if not pick and tokens != ['1080']:   # fall back to 1080p if requested res not cached
        pick = pick_best(streams, ['1080'], bad_rx=bad_rx, prefer_sdr=prefer_sdr)
    if not pick: return ('no-stream', s, e)
    url, gb = pick[0], pick[1]
    raw = f"{sd}/.{prefix}.raw.mkv"; tmp = f"{sd}/.{prefix}.rmx.mkv"
    for x in (raw, tmp):
        try: os.unlink(x)
        except OSError: pass
    maxt = '5400' if kind=='movie' else '2400'
    cr = subprocess.run(['curl','-sS','--fail','-L','--connect-timeout','60','--max-time',maxt,'-o',raw,url],
                        capture_output=True, text=True)
    ok_size = os.path.exists(raw) and os.path.getsize(raw) > 20_000_000
    # curl returncode != 0 == INCOMPLETE transfer (18=partial file, 28=--max-time hit,
    # 56=recv error). A partial download can still be >20MB, so the size check alone is
    # not enough — this is exactly what let the truncated S02E11 (13.6min vs 23.6) through.
    # Any curl error -> dl-fail so it retries rather than silently keeping half an episode.
    if cr.returncode != 0 or not ok_size:
        try: os.unlink(raw)
        except OSError: pass
        return ('dl-fail', s, e)
    # remux: video copy (keep 10-bit HEVC) + first audio copy + all subs copy. NO transcode.
    # -metadata title=<clean stem>: overwrite the source's raw release title (e.g.
    # "PSArips.com | Show.S03E09...HEVC-PSA") so players show a clean name, not junk.
    disp_title = os.path.splitext(os.path.basename(final))[0]
    subprocess.run(['ffmpeg','-y','-v','error','-i',raw,'-map','0:v:0','-map','0:a:0','-map','0:s?',
                    '-c','copy','-dn','-metadata',f'title={disp_title}',tmp], capture_output=True, text=True)
    # remux truncation guard: remuxed duration must be within 2% of the raw's, else the
    # copy dropped a chunk. Compares decoded durations, not byte size.
    def _dur(p):
        try: return float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',p],capture_output=True,text=True).stdout.strip())
        except Exception: return 0
    if os.path.exists(tmp) and _dur(tmp) and _dur(raw) and _dur(tmp) < 0.98*_dur(raw):
        try: os.unlink(tmp)
        except OSError: pass
    if os.path.exists(tmp) and os.path.getsize(tmp) > 20_000_000:
        try: os.unlink(raw)
        except OSError: pass
        os.replace(tmp, final)
    else:
        os.replace(raw, final)  # fallback: keep raw
    log(f"  {prefix} OK ({clean(title)}) [{gb:.2f}GB]")
    return ('ok', s, e)

# build work list
work = []
for j in JOBS:
    if j['kind'] == 'movie':
        work.append(('movie', j['imdb'], None, None, j.get('name','Movie'), j['dest']))
    else:
        tt = cinemeta_titles(j['imdb'])
        for s, rng in j['seasons'].items():
            for e in range(rng[0], rng[1]+1):
                se = (int(s), e)
                work.append(('series', j['imdb'], int(s), e, tt.get(se, f"Episode {e}"), j['dest']))
log(f"[franchise] {len(work)} items, {WORKERS} workers, res={RES}, raw HEVC + subs, no transcode")
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    res = list(ex.map(lambda w: fetch(*w), work))
ok=sum(1 for r in res if r[0]=='ok'); sk=sum(1 for r in res if r[0]=='skip')
bad=[r for r in res if r[0] not in ('ok','skip')]
log(f"\nDONE ok={ok} skip={sk} failed={len(bad)}")
if bad: log("FAILED: "+str(bad))
