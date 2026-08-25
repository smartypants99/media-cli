#!/usr/bin/env python3
"""Credential storage for the media CLI.

Keys live in ~/.config/media/keys.json with 0600 permissions, NEVER in the repo and
never in a config file that sits next to the media. This exists because the original
private version had a Premiumize key hardcoded as a default argument in fetch.py and
copied verbatim into every per-show JSON config — three plaintext copies that would
have been published the moment the project was shared.

Lookup order for any key: explicit argument -> environment variable -> this store.
"""
import json, os, stat, sys

CONFIG_DIR = os.path.expanduser('~/.config/media')
KEYS_PATH = os.path.join(CONFIG_DIR, 'keys.json')

# name -> (env var, human label, where to get one)
KNOWN = {
    'premiumize':  ('PM_KEY',      'Premiumize',  'https://www.premiumize.me/account'),
    'alldebrid':   ('AD_KEY',      'AllDebrid',   'https://alldebrid.com/apikeys'),
    'realdebrid':  ('RD_KEY',      'Real-Debrid', 'https://real-debrid.com/apitoken'),
    'torbox':      ('TORBOX_KEY',  'TorBox',      'https://torbox.app/settings'),
    'ollama':      ('OLLAMA_API_KEY', 'Ollama Cloud', 'https://ollama.com/settings/keys'),
    'nvidia':      ('NVIDIA_API_KEY', 'NVIDIA',    'https://build.nvidia.com'),
    'opensubtitles': ('OS_KEY',    'OpenSubtitles', 'https://www.opensubtitles.com/consumers'),
}


def _read():
    try:
        with open(KEYS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def load_key(name):
    """-> key string or None. Env var wins so CI/one-offs can override the store."""
    name = (name or '').strip().lower()
    env = KNOWN.get(name, (None,))[0]
    if env and os.environ.get(env):
        return os.environ[env]
    return _read().get(name) or None


def save_key(name, value):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    d = _read()
    d[name] = value
    # write 0600 BEFORE the secret goes in, so it is never briefly world-readable
    fd = os.open(KEYS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, 'w') as f:
        json.dump(d, f, indent=2)
    os.chmod(KEYS_PATH, stat.S_IRUSR | stat.S_IWUSR)


def configured():
    """-> {name: bool}. Booleans only. This is what an AI model is allowed to see:
    whether a provider is available, never the credential itself."""
    d = _read()
    return {k: bool(d.get(k) or (v[0] and os.environ.get(v[0])))
            for k, v in KNOWN.items()}


def main():
    a = sys.argv[1:]
    if not a or a[0] in ('list', 'status'):
        print(f'key store: {KEYS_PATH}')
        for name, ok in configured().items():
            label = KNOWN[name][1]
            print(f"  {'set  ' if ok else 'unset'}  {name:<14} {label}")
        print('\n  media keys set <name>      (prompts, input hidden)')
        print('  media keys rm  <name>')
        return
    if a[0] == 'set' and len(a) > 1:
        # accept NVIDIA / Nvidia / nvidia — nobody should have to guess the casing
        name = a[1].strip().lower()
        if name not in KNOWN:
            print(f'unknown key {name!r}. known: {", ".join(KNOWN)}'); sys.exit(2)
        import getpass
        print(f'{KNOWN[name][1]} — get one at {KNOWN[name][2]}')
        v = getpass.getpass(f'paste {name} key (hidden): ').strip()
        if not v:
            print('nothing entered, unchanged'); return
        save_key(name, v)
        print(f'saved to {KEYS_PATH} (0600)')
        return
    if a[0] == 'rm' and len(a) > 1:
        d = _read(); d.pop(a[1].strip().lower(), None)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd = os.open(KEYS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, 'w') as f:
            json.dump(d, f, indent=2)
        print(f'removed {a[1]}'); return
    print('usage: media keys [list|set <name>|rm <name>]'); sys.exit(2)


if __name__ == '__main__':
    main()
