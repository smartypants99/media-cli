#!/usr/bin/env python3
"""media ai — describe what you want in plain English; it runs the right command.

Providers: Ollama Cloud (https://ollama.com) and NVIDIA (https://build.nvidia.com).
Both expose OpenAI-compatible /chat/completions, so one client covers both. Whichever
has a key configured is used; if both do, --provider picks.

DESIGN RULES, all deliberate:

1. The model NEVER sees a credential. It is told `premiumize: configured` as a boolean
   and nothing more. Keys live in ~/.config/media/keys.json (0600) and are substituted
   by this runner at execution time. A cloud model cannot leak what it was never given.

2. The model does not get a shell. It emits a JSON action which is validated against a
   whitelist of media subcommands and flags. Anything unrecognised is refused, not run.

3. Nothing runs without confirmation. The exact command is printed for approval first.

4. It calls `media fetch`, so every existing check still applies: probe-before-download,
   bits-per-pixel quality tiers, English-audio-track-order enforcement, truncation
   detection, season locking.
"""
import json, os, subprocess, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keystore import load_key, configured, KNOWN, CONFIG_DIR


def load_dest():
    try:
        return json.load(open(os.path.join(CONFIG_DIR, 'prefs.json'))).get('dest')
    except Exception:
        return None


def save_dest(d):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        p = os.path.join(CONFIG_DIR, 'prefs.json')
        cur = {}
        try:
            cur = json.load(open(p))
        except Exception:
            pass
        cur['dest'] = d
        json.dump(cur, open(p, 'w'), indent=2)
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

PROVIDERS = {
    'ollama': {'url': 'https://ollama.com/api/chat',
               'models_url': 'https://ollama.com/api/tags',
               'prefer': ['qwen3-coder:480b', 'deepseek-v3.1:671b', 'gpt-oss:120b',
                          'qwen3:235b', 'llama3.3:70b', 'gpt-oss:20b']},
    'nvidia': {'url': 'https://integrate.api.nvidia.com/v1/chat/completions',
               'models_url': 'https://integrate.api.nvidia.com/v1/models',
               'prefer': ['deepseek-ai/deepseek-r1', 'qwen/qwen2.5-coder-32b-instruct',
                          'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct']},
}

# Only these may ever be executed. The model cannot invent a command.
ALLOWED = {
    'fetch':   {'--movie', '--name', '--season', '--eps', '--dest', '--res',
                '--quality', '--depth', '--audio', '--release', '--no-lock',
                '--minmin', '--maxmin'},
    'bitrate': {'--calibrate', '--quick', '--flagged-only', '--no-trunc'},
    'quality': {'--samples', '--workers'},
    'status':  set(),
    'checksum': set(),
}


def http_json(url, payload=None, key=None, timeout=180):
    req = urllib.request.Request(url)
    req.add_header('Content-Type', 'application/json')
    if key:
        req.add_header('Authorization', f'Bearer {key}')
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data, timeout=timeout) as r:
        return json.loads(r.read().decode())


def pick_provider(explicit=None):
    have = {p: load_key(p) for p in PROVIDERS if load_key(p)}
    if explicit:
        if explicit not in PROVIDERS:
            sys.exit(f'unknown provider {explicit!r}; use ollama|nvidia')
        if explicit not in have:
            sys.exit(f'no {explicit} key. run: media keys set {explicit}')
        return explicit, have[explicit]
    if not have:
        print('No AI key configured. Set one of:\n')
        for p in PROVIDERS:
            print(f'  media keys set {p:<8} # {KNOWN[p][1]} — {KNOWN[p][2]}')
        sys.exit(2)
    p = 'ollama' if 'ollama' in have else list(have)[0]
    return p, have[p]


def pick_model(provider, key):
    """Auto-pick: the highest-preference model the account can actually see."""
    cfg = PROVIDERS[provider]
    try:
        d = http_json(cfg['models_url'], key=key, timeout=30)
        if provider == 'ollama':
            avail = {m['name'] for m in d.get('models', [])}
        else:
            avail = {m['id'] for m in d.get('data', [])}
        for want in cfg['prefer']:
            for a in avail:
                if a.startswith(want.split(':')[0]) or a == want:
                    return a
        return sorted(avail)[0] if avail else cfg['prefer'][0]
    except Exception:
        return cfg['prefer'][-1]      # smallest/safest fallback


def build_context():
    """Everything the model needs, assembled from the project itself so it stays
    current: the operating notes, the CLI surface, and which providers exist."""
    parts = []
    # KNOWLEDGE.md is the whole point: without it the model knows the flags but none of
    # the reasons, and will happily ask for 10-bit on a title where that costs 8x the
    # bitrate, or trust a "1080p" label that is really a 1 GB upscale.
    for name in ('KNOWLEDGE.md', 'README.md'):
        p = os.path.join(PROJECT, name)
        if os.path.exists(p):
            parts.append(f'--- {name} ---\n' + open(p).read()[:14000])
    try:
        h = subprocess.run([os.path.join(HERE, 'media')], capture_output=True,
                           text=True, timeout=20).stdout
        parts.append('--- media CLI ---\n' + h)
    except Exception:
        pass
    # Where things can actually go. Without this the model asks "where should I put it?"
    # with no idea what exists, and the user has to type an absolute path from memory.
    dests, last = [], load_dest()
    for base in ('/Volumes', os.path.expanduser('~/Movies'), os.path.expanduser('~/Media')):
        if not os.path.isdir(base):
            continue
        if base == '/Volumes':
            # The boot volume and Time Machine snapshots are not download targets; the
            # first fills your system disk, the second is not writable in any useful way.
            SKIP = ('macintosh hd', 'com.apple.timemachine', 'recovery', 'preboot', 'vm')
            for v in sorted(os.listdir(base)):
                p_ = os.path.join(base, v)
                if v.startswith('.') or not os.path.isdir(p_):
                    continue
                if any(k in v.lower() for k in SKIP) or not os.access(p_, os.W_OK):
                    continue
                try:
                    st = os.statvfs(p_)
                    free = st.f_bavail * st.f_frsize / 1e9
                except Exception:
                    continue
                subs = [d for d in sorted(os.listdir(p_))[:40]
                        if os.path.isdir(os.path.join(p_, d)) and not d.startswith('.')
                        and d.lower() in ('shows', 'tv', 'movies', 'media', 'series')]
                for sub in (subs or ['']):
                    d_ = os.path.join(p_, sub).rstrip('/')
                    dests.append(f"{d_}  ({free:.0f} GB free)")
        else:
            dests.append(base)
    parts.append('--- destinations available on this machine ---\n' +
                 ('\n'.join(dests) if dests else 'none detected — ask the user for a path') +
                 (f'\nLAST USED (prefer this unless told otherwise): {last}' if last else ''))

    cfg = configured()
    parts.append('--- available services (booleans only; you are NOT given keys) ---\n' +
                 '\n'.join(f'{k}: {"configured" if v else "not configured"}'
                           for k, v in cfg.items()))
    debrid = [k for k in ('premiumize', 'alldebrid', 'realdebrid', 'torbox') if cfg.get(k)]
    parts.append('debrid available: ' + (', '.join(debrid) if debrid else
                 'NONE — downloads fall back to public indexers only, which is slower '
                 'and may fail for uncached content. Tell the user they can add one with '
                 '"media keys set premiumize" and that it makes downloads dramatically faster.'))
    return '\n\n'.join(parts)


SYSTEM = """You help a user drive a media-library CLI. You are given the project's
operating notes and the CLI surface.

Ask SHORT clarifying questions one at a time until you know: what title, movie or
series, which seasons/episodes, the destination folder, and the quality tier.

You are given the destinations that exist on this machine with their free space. SUGGEST
one rather than asking the user to type a path from memory — e.g. "I'll put it in
/Volumes/Media/Shows (1.2 TB free), or tell me somewhere else." If a LAST USED path is
given, default to it. Warn if free space looks tight for what was asked. Prefer
sensible defaults over interrogating the user — only ask what you genuinely cannot
infer.

You are NEVER given API keys and must never ask the user to paste one to you. If a
service is not configured, tell them to run `media keys set <name>` themselves.

When you have enough to act, reply with ONLY a JSON object, no prose:
{"action":"run","cmd":"fetch","args":["tt0417299","--season","2","--eps","12-12",
 "--dest","/path","--quality","balanced"],"why":"one line"}

To ask a question instead reply with ONLY:
{"action":"ask","question":"..."}

Rules for args:
- IMDb ids look like tt0417299. If unsure of the id, ask.
- --quality: best | balanced | small. Default balanced.
- --depth 10 only if the user asked for 10-bit; warn it can cost picture quality.
- --audio lossless|surround only if asked.
- Never invent flags outside the CLI help you were given."""


def chat(provider, key, model, messages):
    if provider == 'ollama':
        d = http_json(PROVIDERS[provider]['url'],
                      {'model': model, 'messages': messages, 'stream': False}, key)
        return d.get('message', {}).get('content', '')
    d = http_json(PROVIDERS[provider]['url'],
                  {'model': model, 'messages': messages, 'temperature': 0.2,
                   'max_tokens': 900}, key)
    return d['choices'][0]['message']['content']


def parse_action(txt):
    t = txt.strip()
    if '```' in t:
        t = t.split('```')[1].lstrip('json').strip()
    i, j = t.find('{'), t.rfind('}')
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def validate(cmd, args):
    if cmd not in ALLOWED:
        return f'command {cmd!r} is not allowed'
    flags = {a for a in args if a.startswith('--')}
    bad = flags - ALLOWED[cmd]
    if bad:
        return f'flags not permitted for {cmd}: {", ".join(sorted(bad))}'
    for a in args:
        if any(c in a for c in ';|&$`\n') or a.startswith('$('):
            return f'refusing suspicious argument: {a!r}'
    return None


def main():
    argv = sys.argv[1:]
    provider_arg = None
    if '--provider' in argv:
        i = argv.index('--provider'); provider_arg = argv[i + 1]; del argv[i:i + 2]
    dry = '--dry-run' in argv
    argv = [a for a in argv if a != '--dry-run']
    provider, key = pick_provider(provider_arg)
    model = pick_model(provider, key)
    print(f'[{provider} · {model}]', flush=True)

    msgs = [{'role': 'system', 'content': SYSTEM + '\n\n' + build_context()}]
    first = ' '.join(argv).strip() or input('what do you want? ').strip()
    msgs.append({'role': 'user', 'content': first})

    for _ in range(12):
        try:
            reply = chat(provider, key, model, msgs)
        except Exception as e:
            sys.exit(f'{provider} request failed: {e}')
        act = parse_action(reply)
        if not act:
            print(reply.strip()[:600])
            more = input('\n> ').strip()
            if not more:
                return
            msgs += [{'role': 'assistant', 'content': reply},
                     {'role': 'user', 'content': more}]
            continue
        if act.get('action') == 'ask':
            ans = input(f"\n{act['question']}\n> ").strip()
            msgs += [{'role': 'assistant', 'content': reply},
                     {'role': 'user', 'content': ans}]
            continue
        if act.get('action') == 'run':
            cmd, args = act.get('cmd', ''), [str(a) for a in act.get('args', [])]
            err = validate(cmd, args)
            if err:
                msgs += [{'role': 'assistant', 'content': reply},
                         {'role': 'user', 'content': f'Rejected: {err}. Try again.'}]
                print(f'  refused: {err}')
                continue
            full = [os.path.join(HERE, 'media'), cmd] + args
            print('\n  ' + ' '.join(f'"{a}"' if ' ' in a else a for a in full))
            if act.get('why'):
                print(f'  ({act["why"]})')
            if dry:
                print('  --dry-run: not executed'); return
            if input('\nrun this? [y/N] ').strip().lower() not in ('y', 'yes'):
                print('cancelled'); return
            if '--dest' in args:
                save_dest(args[args.index('--dest') + 1])
            sys.exit(subprocess.run(full).returncode)
        print(reply.strip()[:600]); return
    print('gave up after 12 turns')


if __name__ == '__main__':
    main()
