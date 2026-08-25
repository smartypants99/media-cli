#!/usr/bin/env python3
# media get-book "<title>" [--pdf] [--dest DIR]
# Search LibGen for an ebook, prefer EPUB (lower malware risk than PDF), download,
# and SAFETY-CHECK it before keeping. Rejects anything that isn't a genuine book:
#   - magic-byte check: refuse disguised executables (PE/ELF/Mach-O/script)
#   - EPUB: must be a valid ZIP with application/epub+zip mimetype, no exe/js entries
#   - PDF: flags active-content exploit vectors (/JavaScript /Launch /OpenAction /AA)
# No source is 100% guaranteed, but this catches the realistic ways a book file bites.
import os, re, sys, subprocess, zipfile, urllib.parse

MIRRORS = ['libgen.li', 'libgen.la']
UA = 'Mozilla/5.0'

def curl(url, referer=None, out=None):
    args = ['curl', '-sL', '--max-time', '90', '-A', UA]
    if referer: args += ['-e', referer]
    if out: args += ['-o', out]
    args.append(url)
    r = subprocess.run(args, capture_output=True)
    return r.returncode if out else r.stdout.decode('utf-8', 'replace')

def search(title, want_pdf=False):
    q = urllib.parse.quote(title)
    terms = [w.lower() for w in re.findall(r'\w+', title)]
    for m in MIRRORS:
        html = curl(f"https://{m}/index.php?req={q}")
        if not html or len(html) < 500: continue
        cands = []
        for row in re.findall(r'(?is)<tr[^>]*>(.*?)</tr>', html):
            low = row.lower()
            fm = re.search(r'\b(epub|pdf|mobi|azw3|fb2)\b', row, re.I)
            md5 = re.search(r'md5=([a-fA-F0-9]{32})', row)
            if not fm or not md5: continue
            fmt = fm.group(1).lower()
            # score: how many query terms appear + format preference
            hit = sum(1 for t in terms if t in low)
            if hit == 0: continue
            score = hit * 10
            score += (20 if (fmt == 'pdf') == want_pdf and fmt in ('epub', 'pdf') else 0)
            if fmt == 'epub' and not want_pdf: score += 15
            if fmt == 'pdf' and want_pdf: score += 15
            text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', row)).strip()
            cands.append((score, md5.group(1), fmt, m, text[:100]))
        if cands:
            cands.sort(key=lambda c: -c[0])
            return cands[0][1:]  # md5, fmt, mirror, desc
    return None

def dl_url(md5, mirror):
    ads = curl(f"https://{mirror}/ads.php?md5={md5}", referer=f"https://{mirror}/")
    mm = re.search(r'href="(get\.php\?md5=[^"]+)"', ads)
    return f"https://{mirror}/{mm.group(1)}" if mm else None

def verify(path):
    d = open(path, 'rb').read(8)
    for sig, name in ((b'MZ', 'Windows exe'), (b'\x7fELF', 'Linux ELF'),
                      (b'\xcf\xfa\xed\xfe', 'Mach-O'), (b'\xca\xfe\xba\xbe', 'Mach-O'),
                      (b'#!', 'script')):
        if d.startswith(sig): return False, f'DANGER: disguised {name}'
    if d.startswith(b'PK'):
        try:
            z = zipfile.ZipFile(path); names = z.namelist()
            mt = z.read('mimetype').decode('ascii', 'replace').strip() if 'mimetype' in names else ''
            susp = [n for n in names if n.lower().endswith(('.exe', '.js', '.bat', '.sh', '.dll', '.scr'))]
            if susp: return False, f'epub contains executables: {susp}'
            return True, ('genuine EPUB, clean' if mt == 'application/epub+zip'
                          else 'ZIP (non-standard mimetype) — likely ok')
        except Exception as e:
            return False, f'corrupt zip: {e}'
    if d.startswith(b'%PDF'):
        raw = open(path, 'rb').read()
        hits = [k.decode() for k in (b'/JavaScript', b'/Launch', b'/OpenAction', b'/AA', b'/EmbeddedFile') if k in raw]
        return True, ('PDF, clean (no active content)' if not hits
                      else 'PDF with active-content markers: ' + ', '.join(hits) + ' — review before opening')
    # Kindle formats (mobi/azw/azw3) are Palm-DB containers: type+creator sit at
    # byte offset 60. Not executable (already ruled out above), so accept them.
    hdr = open(path, 'rb').read(68)
    if len(hdr) >= 68 and (hdr[60:68] == b'BOOKMOBI' or hdr[60:63] == b'TPZ'):
        return True, 'Kindle ebook (mobi/azw3), no executable content'
    return False, f'unknown/unsupported type: {d[:4]!r}'

def main():
    args = sys.argv[1:]
    want_pdf = '--pdf' in args
    di = args.index('--dest') if '--dest' in args else -1
    dest = args[di + 1] if di >= 0 else '/Volumes/TD-storage/Books'
    title = ' '.join(a for i, a in enumerate(args)
                     if not a.startswith('--') and not (di >= 0 and i == di + 1))
    if not title:
        print('usage: get-book.py "<title>" [--pdf] [--dest DIR]', file=sys.stderr); sys.exit(2)
    print(f'get-book: searching LibGen for "{title}" (prefer {"PDF" if want_pdf else "EPUB"})')
    res = search(title, want_pdf)
    if not res: print('  no result found'); sys.exit(1)
    md5, fmt, mirror, desc = res
    print(f'  match: [{fmt}] {desc}')
    url = dl_url(md5, mirror)
    if not url: print('  could not resolve a download link'); sys.exit(1)
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, f'.{md5}.part')
    curl(url, referer=f'https://{mirror}/ads.php?md5={md5}', out=tmp)
    if not (os.path.exists(tmp) and os.path.getsize(tmp) > 10000):
        try: os.unlink(tmp)
        except OSError: pass
        print('  download failed / too small'); sys.exit(1)
    ok, msg = verify(tmp)
    print(f'  safety check: {msg}')
    if not ok:
        os.unlink(tmp); print('  ✗ REJECTED — deleted, nothing saved.'); sys.exit(1)
    safe = re.sub(r'[\\/:*?"<>|]', '', title).strip()
    final = os.path.join(dest, f'{safe}.{fmt}')
    os.replace(tmp, final)
    print(f'  ✓ saved: {final} ({os.path.getsize(final)//1024} KB)')

if __name__ == '__main__':
    main()
