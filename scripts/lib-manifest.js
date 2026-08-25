'use strict';
// Lightweight per-drive checksum manifest for bit-rot detection.
//
// WHY: A separate detector (detect-corruption.js) hunts macroblock corruption in
// files that still decode with ZERO ffmpeg errors (flipped bytes during drive
// dropouts). That's expensive and heuristic. This module is the *reliable*
// single-drive detector: after a file is written we record its md5 + size +
// mtime; later a `verify` re-hashes and any file whose md5 changed while its size
// stayed identical is bit-rot, full stop. No guessing.
//
// DESIGN
//  - One manifest per drive root, stored AT the root as `.media-manifest.json`,
//    so the manifest travels with the drive and paths are stored *relative* to
//    the root (mount point can change: /Volumes/TD-storage vs remounted).
//  - Entry: { size, md5, mtimeMs, hashedAt }  keyed by root-relative POSIX path.
//  - Hash: md5 via node's crypto (streaming, no external dep). xxhash/xxhsum are
//    not installed on this box; md5 over a 300-600MB episode is a few seconds and
//    only runs once per file at download time (or on an explicit rebuild).
//  - Atomic + exFAT-safe writes: write sibling tmp file, fsync, rename over the
//    target (rename is atomic within a filesystem; exFAT honours replace-rename).
//  - Concurrency-safe: download-fast.js finishes up to 5 files at once, all
//    wanting to update the same manifest. A mkdir-based lock (mkdir is atomic on
//    exFAT and APFS) serialises read-modify-write; stale locks self-expire.
//
// This module has NO side effects on import and never throws out of its public
// helpers in a way that could break the download pipeline — callers wrap in
// try/catch but the functions also fail soft.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const MANIFEST_NAME = '.media-manifest.json';
const LOCK_SUFFIX = '.lock';
const LOCK_STALE_MS = 60 * 1000;   // a lock older than this is presumed abandoned
const LOCK_WAIT_MS = 15 * 1000;    // max time to wait to acquire a lock
const LOCK_POLL_MS = 100;

// Drive roots we manage. A file under one of these gets recorded in that root's
// manifest. Kept in sync with verify-integrity.js ROOTS.
const ROOTS = [
  '/Volumes/TD-storage/Shows',
  '/Volumes/Shows',
];

function roots() {
  // Allow override for testing via MANIFEST_ROOTS (colon-separated).
  if (process.env.MANIFEST_ROOTS) {
    return process.env.MANIFEST_ROOTS.split(':').filter(Boolean);
  }
  return ROOTS;
}

// Return the managed root that contains `file`, or null if it's outside all
// roots. Longest match wins (roots don't nest today, but be safe).
function rootForFile(file) {
  const abs = path.resolve(file);
  let best = null;
  for (const r of roots()) {
    const rr = path.resolve(r);
    if (abs === rr || abs.startsWith(rr + path.sep)) {
      if (!best || rr.length > best.length) best = rr;
    }
  }
  return best;
}

function manifestPathForRoot(root) {
  return path.join(root, MANIFEST_NAME);
}

function relKey(root, file) {
  return path.relative(root, path.resolve(file)).split(path.sep).join('/');
}

// ---- hashing -------------------------------------------------------------

function hashFile(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('md5');
    const s = fs.createReadStream(file);
    s.on('error', reject);
    s.on('data', d => h.update(d));
    s.on('end', () => resolve(h.digest('hex')));
  });
}

// ---- locking -------------------------------------------------------------

async function acquireLock(manifestPath) {
  const lockDir = manifestPath + LOCK_SUFFIX;
  const deadline = Date.now() + LOCK_WAIT_MS;
  for (;;) {
    try {
      fs.mkdirSync(lockDir);
      return lockDir;
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      // Break a stale lock (previous process died mid-write).
      try {
        const st = fs.statSync(lockDir);
        if (Date.now() - st.mtimeMs > LOCK_STALE_MS) {
          try { fs.rmdirSync(lockDir); } catch {}
          continue;
        }
      } catch {}
      if (Date.now() > deadline) throw new Error(`manifest lock timeout: ${lockDir}`);
      await new Promise(r => setTimeout(r, LOCK_POLL_MS));
    }
  }
}

function releaseLock(lockDir) {
  try { fs.rmdirSync(lockDir); } catch {}
}

// ---- manifest read / atomic write ---------------------------------------

function readManifest(manifestPath) {
  try {
    const raw = fs.readFileSync(manifestPath, 'utf8');
    const j = JSON.parse(raw);
    if (!j || typeof j !== 'object' || !j.files) return { version: 1, files: {} };
    return j;
  } catch {
    return { version: 1, files: {} };
  }
}

function writeManifestAtomic(manifestPath, manifest) {
  manifest.updatedAt = new Date().toISOString();
  const tmp = manifestPath + '.tmp';
  const fd = fs.openSync(tmp, 'w');
  try {
    fs.writeFileSync(fd, JSON.stringify(manifest, null, 2));
    try { fs.fsyncSync(fd); } catch {}
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, manifestPath); // atomic within the filesystem (exFAT-safe)
}

// Run fn(manifest) under the root's lock, persisting the (possibly mutated)
// manifest fn returns. fn may return null/undefined to skip writing.
async function withManifest(root, fn) {
  const manifestPath = manifestPathForRoot(root);
  const lock = await acquireLock(manifestPath);
  try {
    const manifest = readManifest(manifestPath);
    const out = await fn(manifest);
    if (out !== false) writeManifestAtomic(manifestPath, manifest);
    return out;
  } finally {
    releaseLock(lock);
  }
}

// ---- public API ----------------------------------------------------------

// Record (or refresh) one file in its drive's manifest. Fail-soft: returns a
// status string, never throws through to the caller's happy path.
async function recordFile(file) {
  try {
    const root = rootForFile(file);
    if (!root) return 'outside-roots';
    const st = fs.statSync(file);
    const md5 = await hashFile(file);
    const key = relKey(root, file);
    await withManifest(root, (m) => {
      m.files[key] = {
        size: st.size,
        md5,
        mtimeMs: Math.round(st.mtimeMs),
        hashedAt: new Date().toISOString(),
      };
    });
    return 'recorded';
  } catch (e) {
    return 'error:' + (e && e.message || e);
  }
}

// Build/refresh the manifest for every media file under a path (root or subdir).
// onProgress(done,total,file) optional. Returns { recorded, roots:{...} }.
const MEDIA_EXTS = new Set(['.mkv', '.mp4', '.avi', '.m4v', '.mov']);
function walkMedia(dir, out = []) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    if (e.name.startsWith('.') || e.name.endsWith('.part') || e.name.endsWith('.tmp')) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walkMedia(p, out);
    else if (MEDIA_EXTS.has(path.extname(e.name).toLowerCase())) out.push(p);
  }
  return out;
}

async function buildManifest(targetPath, onProgress) {
  const files = walkMedia(path.resolve(targetPath));
  let recorded = 0;
  // Group by root so we take each lock once and batch writes.
  const byRoot = new Map();
  for (const f of files) {
    const root = rootForFile(f);
    if (!root) continue;
    if (!byRoot.has(root)) byRoot.set(root, []);
    byRoot.get(root).push(f);
  }
  let done = 0;
  for (const [root, group] of byRoot) {
    // Hash outside the lock (slow), then take the lock briefly to write.
    const hashed = [];
    for (const f of group) {
      try {
        const st = fs.statSync(f);
        const md5 = await hashFile(f);
        hashed.push({ key: relKey(root, f), size: st.size, md5, mtimeMs: Math.round(st.mtimeMs) });
      } catch {}
      done++;
      if (onProgress) onProgress(done, files.length, f);
    }
    await withManifest(root, (m) => {
      for (const h of hashed) {
        m.files[h.key] = { size: h.size, md5: h.md5, mtimeMs: h.mtimeMs, hashedAt: new Date().toISOString() };
        recorded++;
      }
    });
  }
  return { recorded, total: files.length, roots: [...byRoot.keys()] };
}

// Verify recorded files against current bytes. Classifies each into:
//   bitrot   — md5 changed but size identical  → silent corruption (the point)
//   resized  — size changed (edited/re-encoded/re-downloaded) → md5 also updated
//   missing  — recorded file no longer on disk
//   ok       — unchanged
// Also reports `unrecorded` media files present on disk but absent from manifest.
// verify is read-only by default; pass { refresh:true } to update entries whose
// size legitimately changed (so re-encodes don't nag forever).
async function verifyManifest(targetPath, opts = {}) {
  const abs = path.resolve(targetPath || '');
  const result = { bitrot: [], resized: [], missing: [], ok: 0, unrecorded: [], checked: 0 };
  const targetRoots = targetPath ? roots().filter(r => {
    const rr = path.resolve(r);
    return abs === rr || abs.startsWith(rr + path.sep) || rr.startsWith(abs + path.sep);
  }) : roots();

  for (const root of targetRoots) {
    const rr = path.resolve(root);
    const manifestPath = manifestPathForRoot(rr);
    const manifest = readManifest(manifestPath);
    const updates = {};
    for (const [key, rec] of Object.entries(manifest.files || {})) {
      const file = path.join(rr, key);
      // If a subdir was targeted, only check files under it.
      if (targetPath && abs !== rr && !path.resolve(file).startsWith(abs + path.sep) && path.resolve(file) !== abs) continue;
      result.checked++;
      let st;
      try { st = fs.statSync(file); } catch { result.missing.push(file); continue; }
      let md5;
      try { md5 = await hashFile(file); } catch { result.missing.push(file); continue; }
      if (md5 === rec.md5) { result.ok++; continue; }
      if (st.size === rec.size) {
        // Same size, different bytes = bit-rot.
        result.bitrot.push({ file, recordedMd5: rec.md5, currentMd5: md5, size: st.size });
      } else {
        result.resized.push({ file, recordedSize: rec.size, currentSize: st.size });
        if (opts.refresh) {
          updates[key] = { size: st.size, md5, mtimeMs: Math.round(st.mtimeMs), hashedAt: new Date().toISOString() };
        }
      }
    }
    // Find media on disk not in the manifest.
    const onDisk = walkMedia(targetPath && abs.startsWith(rr) ? abs : rr);
    for (const f of onDisk) {
      const key = relKey(rr, f);
      if (!(manifest.files && manifest.files[key])) result.unrecorded.push(f);
    }
    if (opts.refresh && Object.keys(updates).length) {
      await withManifest(rr, (m) => { Object.assign(m.files, updates); });
    }
  }
  return result;
}

module.exports = {
  MANIFEST_NAME,
  ROOTS,
  roots,
  rootForFile,
  manifestPathForRoot,
  hashFile,
  recordFile,
  buildManifest,
  verifyManifest,
  walkMedia,
};
