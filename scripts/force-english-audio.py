#!/usr/bin/env python3
# force-english-audio.py <dir-or-file>...
#
# Make English audio actually PLAY, not merely be "default".
#
# Why this exists (2026-08-05): the [uP] Naruto release ships French as audio track 1,
# Japanese as 2, English as 3. Setting flag-default=1 on the English track with
# mkvpropedit LOOKED correct — ffprobe confirmed DISPOSITION:default=1 on eng — but VLC
# played French anyway, because it takes the FIRST audio track when its own preferred-
# language setting doesn't decide it. A default flag is not enough; English has to be
# physically first in the file.
#
# So: drop every non-English audio track entirely and remux English as the only one.
# The user watches in English and does not want the other dubs, so there is nothing to
# preserve, and removing them makes it impossible for a player to pick the wrong one.
# mkvmerge remuxes without re-encoding, so this is fast and lossless.
#
# Subtitles are kept but all set non-default (user asked for no auto subs).
import os, subprocess, sys

def audio_langs(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                          '-show_entries', 'stream_tags=language',
                          '-of', 'default=noprint_wrappers=1:nokey=1', path],
                         capture_output=True, text=True).stdout.strip().split('\n')
    return [t for t in out if t]

def fix(path):
    langs = audio_langs(path)
    if not langs:
        return ('nosubs', path, 'no audio')
    # track 0 being English is what actually decides playback — leave those alone
    # rather than needlessly remuxing tens of GB.
    if langs[0] == 'eng':
        return ('ok-already', path, 'english already first')
    if 'eng' not in langs:
        return ('NO-ENGLISH', path, f'tracks={langs}')
    tmp = os.path.join(os.path.dirname(path), '.forceeng.tmp.mkv')
    # -a eng keeps only English audio; mkvmerge renumbers it as track 1
    r = subprocess.run(['mkvmerge', '-o', tmp, '-a', 'eng', '--no-track-tags', path],
                       capture_output=True, text=True)
    ok = os.path.exists(tmp) and os.path.getsize(tmp) > 10_000_000
    if r.returncode not in (0, 1) or not ok:      # 1 = warnings only
        if os.path.exists(tmp): os.unlink(tmp)
        return ('FAIL', path, r.stderr.strip()[:80])
    # verify the remux really has exactly one, English, audio track
    new = audio_langs(tmp)
    if new != ['eng']:
        os.unlink(tmp)
        return ('FAIL', path, f'verify failed: {new}')
    # subs present but never auto-on
    n_subs = len(subprocess.run(['ffprobe','-v','error','-select_streams','s',
        '-show_entries','stream=index','-of','default=noprint_wrappers=1:nokey=1', tmp],
        capture_output=True, text=True).stdout.split())
    if n_subs:
        args = ['mkvpropedit', tmp]
        for i in range(1, n_subs+1):
            args += ['--edit', f'track:s{i}', '--set', 'flag-default=0']
        subprocess.run(args, capture_output=True)
    os.replace(tmp, path)
    return ('FIXED', path, f'{langs} -> [eng]')

targets = []
for a in sys.argv[1:]:
    if os.path.isdir(a):
        for dp, _, ns in os.walk(a):
            targets += [os.path.join(dp, n) for n in ns if n.endswith('.mkv') and not n.startswith('.')]
    elif os.path.isfile(a):
        targets.append(a)

for t in sorted(targets):
    status, p, msg = fix(t)
    if status != 'ok-already':
        print(f'  {status:11} {os.path.basename(p)[:52]:52} {msg}')
print(f'\nchecked {len(targets)} file(s)')
