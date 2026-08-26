#!/usr/bin/env python3
"""Argument repair and validation for the media CLI.

Lives here, not in ai.py, because guards belong in the TOOL rather than in one caller.
When they sat in the AI front end, anything else driving the CLI — opencode, a script,
a person typing — bypassed them entirely, which is exactly the wrong way round.

Every rule below exists because a model produced that exact malformed command during
testing, and the result would have been silent: a download landing somewhere other than
where it was asked to go.
"""
import os

# flags that consume exactly one value
TAKES_VALUE = {'--name', '--season', '--eps', '--dest', '--res', '--quality',
               '--depth', '--audio', '--release', '--minmin', '--maxmin',
               '--samples', '--workers'}

BAD_CHARS = ';|&$`\n'


def volumes():
    try:
        return [v for v in os.listdir('/Volumes') if not v.startswith('.')]
    except Exception:
        return []


def resolve_dest(dest, library=None):
    """-> (path, note, error). Repairs the three ways a destination goes wrong.

    1. A relative path silently creates a folder in the working directory rather than on
       the drive. Seen: "TD-storage/kjbkhb" with no leading /Volumes.
    2. A drive name the user said may not be the volume that exists. Seen: "TD" when the
       mount is "TD-storage".
    3. A path built through a parent traversal escapes the intended root entirely.
    """
    if not dest:
        return None, None, 'no value for --dest'
    dest = os.path.expanduser(dest)
    note = None

    if '..' in dest.split('/'):
        return None, None, f'refusing parent traversal in --dest: {dest!r}'

    if not dest.startswith('/'):
        head = dest.split('/')[0]
        vols = volumes()
        if head in vols or any(v.lower().startswith(head.lower()) for v in vols):
            dest = '/Volumes/' + dest
            note = f'anchored to /Volumes: {dest}'
        elif library:
            dest = os.path.join(library, dest)
            note = f'anchored to library root: {dest}'
        else:
            return None, None, f'--dest must be absolute, got {dest!r}'

    # map a said-name to the volume that actually exists
    if dest.startswith('/Volumes/'):
        parts = dest.split('/')
        if len(parts) > 2:
            want = parts[2]
            vols = volumes()
            if want and want not in vols:
                m = [v for v in vols if v.lower().startswith(want.lower())
                     or want.lower() in v.lower()]
                if len(m) == 1:
                    dest = dest.replace(f'/Volumes/{want}', f'/Volumes/{m[0]}', 1)
                    note = f'drive {want!r} -> {m[0]!r}'
                elif not m:
                    return None, None, (f'no mounted volume matches {want!r}; '
                                        f'available: {", ".join(vols) or "none"}')
    return dest, note, None


def repair(args, library=None):
    """-> (args, note, error). Rejoins a --dest split across arguments, then resolves it.

    Seen from a model: ["--dest", "/Volumes/TD/", "kjbkhb"] — two arguments. A naive flag
    check passes it and the download goes to /Volumes/TD/ with a stray argument.
    """
    args = list(args)
    if '--dest' not in args:
        return args, None, None
    i = args.index('--dest')
    j, parts = i + 1, []
    while j < len(args) and not args[j].startswith('--'):
        parts.append(args[j]); j += 1
    if not parts:
        return args, None, 'no value for --dest'
    joined = parts[0] if len(parts) == 1 else os.path.join(*[parts[0].rstrip('/')] + parts[1:])
    dest, note, err = resolve_dest(joined, library)
    if err:
        return args, None, err
    return args[:i] + ['--dest', dest] + args[j:], note, None


def validate(args, allowed_flags=None):
    """-> error string or None. Flag/value pairing and stray positionals."""
    if allowed_flags is not None:
        bad = {a for a in args if a.startswith('--')} - set(allowed_flags)
        if bad:
            return f'unknown flags: {", ".join(sorted(bad))}'
    i, positional = 0, 0
    while i < len(args):
        a = args[i]
        if any(c in a for c in BAD_CHARS) or a.startswith('$('):
            return f'refusing suspicious argument: {a!r}'
        if a.startswith('--'):
            if a in TAKES_VALUE:
                vals, j = [], i + 1
                while j < len(args) and not args[j].startswith('--'):
                    vals.append(args[j]); j += 1
                if len(vals) != 1:
                    return (f'{a} needs exactly one value, got {len(vals)}: {vals}'
                            if vals else f'{a} is missing its value')
                i = j
                continue
            i += 1
        else:
            positional += 1
            if positional > 1:
                return f'unexpected extra argument: {a!r}'
            i += 1
    return None


def guard(args, library=None, allowed_flags=None):
    """Repair then validate. Returns (args, note, error)."""
    args, note, err = repair(args, library)
    if err:
        return args, note, err
    return args, note, validate(args, allowed_flags)
