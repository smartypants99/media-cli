# media

A CLI for building and verifying a local media library. It finds releases across many
indexers, **measures them before downloading**, and keeps a check on quality afterwards.

Every rule in here came from something going wrong on a real library. The comments say
which.

```bash
./install.sh
media keys set premiumize          # optional, but much faster
media ai "get me avatar season 2 in good quality"
```

---

## Why it exists

Picking releases by filename or file size gets you bad files. Three failures that shaped
this tool, all measured:

**Resolution tells you nothing.** A show's 480p and "1080p" releases, same episode:

| | resolution | bitrate | bits/pixel | size |
|---|---|---|---|---|
| kept | 708×480 | 3656 kbps | 0.450 | 0.68 GB |
| rejected | 1920×1080 | — | 0.043 | 0.40 GB |

The "1080p" file was **smaller and looked worse** — an upscale spreading the same detail
across four times the pixels. Of 37 releases probed for that show, the honest ceiling was
708×480.

**Advertised size describes the torrent, not the episode.** A 1.33 GB season pack looked
"bigger" than a 0.28 GB single episode; per-episode it was 1.43 GB vs 0.30 GB. Ranking on
the advertised number picks the worse file.

**Titles lie.** Observed while building this: "1080p" that was 1428×1068, "1080p ENG"
that was 1440×1080 and 8-bit, "2160p" that was a 1 GB upscale.

So this tool **probes the actual episode file over the CDN before downloading** — real
resolution, bit depth, bitrate, audio codec and track order — and ranks on that.

---

## Commands

```
media ai       ["what you want"] [--provider ollama|nvidia] [--dry-run]
media keys     [list|set <name>|rm <name>]
media fetch    <imdb> [--movie | --season N --eps A-B] --dest DIR
               [--res 1080p] [--quality best|balanced|small]
               [--depth 10|8|any] [--audio lossless|surround|any]
               [--release <infohash>] [--no-lock]
media bitrate  <dir|file> [--quick] [--calibrate] [--flagged-only] [--no-trunc]
media quality  <dir>
media status
```

### Quality tiers

Minimum bits-per-pixel to accept, per resolution tier. `balanced` is the default.

| tier | means | example (one 22-min episode) |
|---|---|---|
| `best` | highest bitrate available | 1.43 GB |
| `balanced` | comfortably above a normal library median | 0.67 GB |
| `small` | cheapest thing that isn't starved | 0.23 GB |

Over ten 26-episode seasons that is 372 GB vs 174 GB vs 78 GB.

`--depth` and `--audio` are **hard filters, not preferences**, and they warn when a
requirement costs you picture quality:

```
WARNING: depth=10 costs picture quality — best meeting it is 0.0218 bpp
         vs 0.1695 unconstrained (7.8x). Drop the requirement for a better encode.
```

That case is real: the only 10-bit release of one show was eight times thinner than the
best 8-bit one.

### Verification

`media bitrate <dir>` is the integrity check:

- **whole-file demux** — reads every byte without decoding, ~273× realtime, catching
  damage anywhere rather than only near the end
- **bits-per-pixel outliers** — compares each episode against **its own season**, falling
  back to the same show. Never a library-wide average: a generous release in one show
  will condemn a perfectly good lower-bitrate show
- **three verdicts** — `intact` / `corrupt` / **`unverified`**. A stalled drive is never
  reported as corruption. On one 1294-file sweep a parallel run claimed 326 broken files;
  a serial recheck confirmed 2

Absolute thresholds do not work and the tool does not use them. Good 1080p content can sit
at 0.045 bits/pixel while visibly broken SD sits *higher* at 0.052. Comparison is always
like-for-like.

---

## media ai

Describe what you want; it asks what it needs and runs the right command.

Two backends:

- **opencode** (default when installed and no API key is set) — ships free models needing
  no key at all, and has real file/shell tools, so it can answer "what folders are on my
  drive?" as well as run downloads. Its own approval prompts govern what executes.
- **built-in** (Ollama Cloud / NVIDIA) — used when a key is configured. Faster and
  guarded by a strict command whitelist, but it can only run `media` subcommands.

`--backend opencode|api` forces one. Measured on the same request: NVIDIA gpt-oss-120b
2.6s, opencode mimo-v2.5-free 18s (both correct); a third free model produced a relative
path that would have written to the working directory instead of the drive.

Prefer MoE models — large total parameters with a small active set gives capability at
near-small-model latency. Dense 70B models took 41-54s for the same work.

Four rules, all deliberate:

1. **The model never receives a credential.** It is told `premiumize: configured` as a
   boolean. Keys are substituted by the runner at execution time. A cloud model cannot
   leak what it was never given, and it will never ask you to paste a key to it.
2. **The model does not get a shell.** It emits a JSON action, validated against a
   whitelist of subcommands and flags. Shell metacharacters are refused.
3. **Nothing runs without confirmation.** The exact command is printed first.
4. **It calls the same commands you would**, so every check still applies.

---

## Credentials

```bash
media keys set premiumize
media keys list
```

Stored in `~/.config/media/keys.json`, mode `0600`, never in the repo. Environment
variables (`PM_KEY`, `OLLAMA_API_KEY`, `NVIDIA_API_KEY`, …) override the store.

A debrid service is optional. Without one, downloads fall back to public indexers, which
is slower and may fail for uncached content.

---

## Requirements

`ffmpeg`, `ffprobe`, `mkvtoolnix` (`mkvmerge`, `mkvpropedit`), `python3`, `node`.

```bash
brew install ffmpeg mkvtoolnix node
```

---

## Notes

Filesystem: on exFAT volumes, concurrent writers can flip the mount read-only and
seek-heavy parallel reads can stall it. Worker counts here are capped accordingly (3 for
the demux sweep) and transient errors are retried, never reported as corruption.

A full sweep is I/O-bound on total library size — budget roughly 80 minutes per terabyte,
not per file.

## Licence

MIT. You are responsible for what you download.
