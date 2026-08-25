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


PREFS = os.path.join(CONFIG_DIR, 'prefs.json')


def prefs():
    try:
        return json.load(open(PREFS))
    except Exception:
        return {}


def set_pref(k, v):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        d = prefs(); d[k] = v
        json.dump(d, open(PREFS, 'w'), indent=2)
    except Exception:
        pass


def load_dest():
    return prefs().get('dest')


def save_dest(d):
    set_pref('dest', d)


def candidate_roots():
    """Writable places a library could plausibly live, largest free space first."""
    out = []
    SKIP = ('macintosh hd', 'com.apple.timemachine', 'recovery', 'preboot', 'vm')
    for base in ('/Volumes',):
        if not os.path.isdir(base):
            continue
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
            subs = [d for d in sorted(os.listdir(p_))
                    if os.path.isdir(os.path.join(p_, d)) and not d.startswith('.')
                    and d.lower() in ('shows', 'tv', 'movies', 'media', 'series')]
            for sub in (subs or ['']):
                out.append((os.path.join(p_, sub).rstrip('/'), free))
    for h in (os.path.expanduser('~/Movies'), os.path.expanduser('~/Media')):
        if os.path.isdir(h):
            try:
                st = os.statvfs(h)
                out.append((h, st.f_bavail * st.f_frsize / 1e9))
            except Exception:
                pass
    return sorted(out, key=lambda x: -x[1])


def ensure_library():
    """Ask ONCE for where the library lives, then never again.

    Asking the model to negotiate a destination every run means the user retypes an
    absolute path from memory each time. This is a plain prompt, not a model turn: it
    offers what is mounted, takes a number or a path, and saves it.
    """
    lib = prefs().get('library')
    if lib:
        return lib
    roots = candidate_roots()
    print('Where should downloads go? (asked once, saved to '
          f'{PREFS})\n')
    for i, (p_, free) in enumerate(roots, 1):
        print(f'  {i}) {p_}  ({free:.0f} GB free)')
    print(f'  {len(roots) + 1}) somewhere else — type a full path\n')
    ans = input('choice: ').strip()
    if ans.isdigit() and 1 <= int(ans) <= len(roots):
        lib = roots[int(ans) - 1][0]
    else:
        lib = os.path.expanduser(ans if not ans.isdigit() else
                                 input('full path: ').strip())
    if not lib:
        sys.exit('no library path given')
    os.makedirs(lib, exist_ok=True)
    set_pref('library', lib)
    print(f'saved. change it later with:  media ai --set-library <path>\n')
    return lib

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

PROVIDERS = {
    'ollama': {'url': 'https://ollama.com/api/chat',
               'models_url': 'https://ollama.com/api/tags',
               'prefer': ['qwen3-coder:480b', 'deepseek-v3.1:671b', 'gpt-oss:120b',
                          'qwen3:235b', 'llama3.3:70b', 'gpt-oss:20b']},
    # NVIDIA order is LATENCY-FIRST, measured on a real account: llama-3.1-8b answers in
    # 0.7s while the 70B models take 41-54s for the SAME two-token reply. That is the
    # difference between a conversation and a frozen terminal. Bigger models sit lower as
    # a fallback, and --model overrides.
    'nvidia': {'url': 'https://integrate.api.nvidia.com/v1/chat/completions',
               'models_url': 'https://integrate.api.nvidia.com/v1/models',
               'prefer': ['meta/llama-3.1-8b-instruct', 'qwen/qwen2.5-coder-32b-instruct',
                          'meta/llama-3.3-70b-instruct', 'meta/llama-3.1-70b-instruct']},
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


def pick_model(provider, key, override=None):
    """Auto-pick a model that is actually SERVABLE, not merely listed.

    The models endpoint lists far more than it will serve: on a real account, 95 models
    were listed and most returned 404 on first use. Listing is not availability, so each
    candidate gets a cheap liveness probe before being chosen.
    """
    if override:
        return override
    cfg = PROVIDERS[provider]
    try:
        d = http_json(cfg['models_url'], key=key, timeout=30)
        avail = ({m['name'] for m in d.get('models', [])} if provider == 'ollama'
                 else {m['id'] for m in d.get('data', [])})
    except Exception:
        return cfg['prefer'][0]
    for want in cfg['prefer']:
        for a in sorted(avail):
            if a == want or a.startswith(want.split(':')[0]):
                try:
                    _chat(provider, key, a, [{'role': 'user', 'content': 'hi'}])
                    return a
                except Exception:
                    break        # listed but not servable — try the next preference
    return sorted(avail)[0] if avail else cfg['prefer'][0]


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
    # The library root is settled ONCE by ensure_library(), outside the model. The
    # model is told where it is and builds show folders under it, rather than asking
    # the user to type an absolute path on every run.
    lib = prefs().get('library')
    extra = [f'{p_}  ({free:.0f} GB free)' for p_, free in candidate_roots()]
    parts.append('--- library root (use this; put shows in subfolders under it) ---\n' +
                 (lib or 'NOT SET') +
                 ('\nother writable locations:\n' + '\n'.join(extra) if extra else ''))

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
series, which seasons/episodes, and the quality tier.

DO NOT ask where to save things. A library root is configured for you. Build the --dest
under it, e.g. <library root>/<Show Name>. Only mention the path in passing ("saving to
.../Avatar"), and only ask if the user wants somewhere different. Warn if free space
looks tight for what was asked. Prefer
sensible defaults over interrogating the user — only ask what you genuinely cannot
infer.

You are NEVER given API keys and must never ask the user to paste one to you. If a
service is not configured, tell them to run `media keys set <name>` themselves.

Talk normally. You are having a conversation — greet the user, answer questions about
the library or about video quality, think out loud. Only switch to JSON when you are
actually ready to run something.

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


def chat(provider, key, model, messages, quiet=False):
    """Talk to the model, showing progress. A silent 30-second HTTPS read looks like a
    hang and gets Ctrl+C'd, so always print something."""
    import threading, itertools, time as _t
    stop = threading.Event()

    def spin():
        for c in itertools.cycle('|/-\\'):
            if stop.is_set():
                break
            sys.stdout.write(f'\r  thinking {c} '); sys.stdout.flush(); _t.sleep(0.12)
        sys.stdout.write('\r' + ' ' * 20 + '\r'); sys.stdout.flush()

    th = None
    if not quiet and sys.stdout.isatty():
        th = threading.Thread(target=spin, daemon=True); th.start()
    try:
        return _chat(provider, key, model, messages)
    finally:
        stop.set()
        if th:
            th.join(timeout=1)


def _chat(provider, key, model, messages):
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
    model_arg = None
    if '--model' in argv:
        i = argv.index('--model'); model_arg = argv[i + 1]; del argv[i:i + 2]
    dry = '--dry-run' in argv
    argv = [a for a in argv if a != '--dry-run']
    if '--set-library' in argv:
        i = argv.index('--set-library')
        set_pref('library', os.path.expanduser(argv[i + 1]))
        print(f'library root set to {prefs()["library"]}'); return
    ensure_library()
    provider, key = pick_provider(provider_arg)
    model = pick_model(provider, key, model_arg)
    print(f'[{provider} · {model}]', flush=True)

    msgs = [{'role': 'system', 'content': SYSTEM + '\n\n' + build_context()}]
    print("  (chat normally — say what you want. 'q' to quit)\n")
    try:
        first = ' '.join(argv).strip() or input('> ').strip()
    except (EOFError, KeyboardInterrupt):
        print(); return
    if not first:
        return
    msgs.append({'role': 'user', 'content': first})

    for _ in range(40):
        try:
            reply = chat(provider, key, model, msgs)
        except KeyboardInterrupt:
            print('\n(cancelled)'); return
        except Exception as e:
            print(f'\n  {provider} request failed: {e}')
            if input('  retry? [Y/n] ').strip().lower() in ('n', 'no'):
                return
            continue
        act = parse_action(reply)
        if not act:
            print(reply.strip())
            try:
                more = input('\n> ').strip()
            except (EOFError, KeyboardInterrupt):
                print(); return
            if not more or more.lower() in ('q', 'quit', 'exit', 'bye'):
                return
            msgs += [{'role': 'assistant', 'content': reply},
                     {'role': 'user', 'content': more}]
            continue
        if act.get('action') == 'ask':
            try:
                ans = input(f"\n{act['question']}\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(); return
            if ans.lower() in ('q', 'quit', 'exit'):
                return
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
    print('(conversation limit reached)')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n(cancelled)')
        sys.exit(130)
