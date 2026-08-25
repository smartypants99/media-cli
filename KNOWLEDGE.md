# Operating knowledge

Rules this tool encodes, and the failure that produced each one. Every number here was
measured on a real ~1300-file library, not taken from a guide. This file is loaded as
context by `media ai`.

## Picking a release

- **Resolution tells you almost nothing.** One show's 480p release ran 3656 kbps
  (0.450 bits/pixel) while its "1080p" release ran at 0.043 bpp — the 1080p file was
  *smaller* and looked *worse*, an upscale spreading the same detail over 4x the pixels.
  Of 37 releases probed for that title, the honest ceiling was 708x480.
- **Advertised size describes the torrent, not the episode.** A 1.33 GB season pack looks
  "bigger" than a 0.28 GB single episode; per-episode it was 1.43 GB vs 0.30 GB. Always
  probe the actual file.
- **Titles lie constantly.** Observed: "1080p" that was 1428x1068; "1080p ENG" that was
  1440x1080 and 8-bit; "2160p" that was a 1 GB upscale. Probe, never trust the name.
- **Neither biggest-wins nor smallest-wins is correct.** Smallest-above-a-size-floor picked
  a bitrate-starved encode that macroblocked on motion. Biggest-wins pays for near-source
  files with no visible benefit. Rank on measured bits-per-pixel, then take the smallest
  that clears the bar.
- **Bits-per-pixel is normalised per pixel, so never compare across resolutions with it.**
  720p at 0.1175 bpp outscores 1080p at 0.0793 while having 2.25x fewer pixels. Choose the
  resolution tier first, then rank within it.
- **Hard requirements must be filters, not score bonuses.** Adding a "10-bit" bonus to a
  quality score let a 10-bit file at 1074 kbps beat an 8-bit file at 8426 kbps. Filter
  first; warn when the requirement costs picture quality.
- **Lock a whole season to ONE release.** Ranking each episode independently assembled a
  season from four packs across three codecs, including one the player could not decode.
- **Prefer dual-audio/dub releases explicitly**, or a pure smallest-wins pick lands on a
  subtitle-only file in the original language.

## Audio

- **The wanted language must be audio TRACK 0, not merely flagged default.** Players use
  the first track. Files with `DISPOSITION:default=1` on English still played Mandarin and
  French. Verify track order.
- **`en` and `eng` are the same language.** Comparing only against `eng` made a good
  dual-audio file look like it had none.

## Verifying what you downloaded

- **Header duration lies about a truncated file.** Half-downloaded files still advertise
  their full runtime, so size and duration checks both pass them. One had no decodable
  video at all. Only decoding near the end catches it.
- **Never pick the biggest file in a torrent for an episode.** Season packs mean that grabs
  the wrong thing — it once downloaded one long file three times as three episodes. Match
  the episode number in the filename and reject the candidate if it is not there.
- **Zero-pad when building an episode pattern.** Unpadded, S01E01 searches for "11" and
  matches episode 11.
- **`-xerror` is useless for truncation.** A half-truncated file prints "File ended
  prematurely" and still exits 0. Return codes carry no information; use stderr content or
  packet coverage.
- **Some stderr is benign and will condemn a healthy library if treated as a verdict.**
  `non monotonically increasing dts` plus its `Last message repeated N times` follow-on has
  faked corruption twice, once claiming 120 bad files. Opus header-parse warnings likewise.
  Filter them; a bare count of stderr lines is not a verdict.
- **Whole-file demux (`-c copy -f null -`) runs ~273x realtime vs ~30x for a full decode**,
  and catches damage anywhere rather than only near the end. But a full sweep is I/O-bound
  on total bytes — budget roughly 80 minutes per terabyte, not per file.
- **Packet coverage (last packet pts / header duration) is the cleanest truncation signal.**
  Healthy 0.999, half-truncated 0.499. It costs a full packet enumeration, which times out
  on multi-hour 4K files, so gate it.

## Comparing quality

- **Absolute bits-per-pixel thresholds do not work.** Good 1080p content sits at 0.045 bpp
  while visibly broken SD sits *higher* at 0.052. Required bpp falls steeply with
  resolution and shifts with codec.
- **Compare an episode to its OWN season, then its own show — never a library-wide median.**
  One generous release (0.42 bpp) dominated a global SD median and false-flagged a
  different, legitimately lower-bitrate show at 0.066 as "6.4x thin".
- **Calibrate a floor BELOW your clean corpus's minimum, not at a percentile.** A
  percentile floor rejects your own good files by construction: 0.80 x p05 rejected five
  episodes from a library confirmed 100% intact, because flat animation compresses better
  than anything else in it.
- **A tier with a large n can still be one show.** 156 files in a tier where 153 came from
  a single generous release produced a floor demanding 2.43 Mbps at 480p — enough to reject
  good SD, which typically runs 1-1.5 Mbps.
- **Blur detection is blind to macroblocking, by construction.** A bitrate-starved frame
  scores as *marginally sharper* than a clean one (4.97 vs 5.02), because block edges are
  hard edges. Sharpness metrics detect the opposite failure.
- **There is no cheap packet-level detector for bitrate starvation.** Two were tested
  against a labelled corpus and both died: packet-size variance (measures content tempo,
  false-positived 8 of 11 known-good files) and I-frame-only bpp (healthy panel landed
  inside the starved range). Starvation is only catchable by bpp compared like-for-like.
- **Beware confounded corpora.** A metric separated a 153-pair labelled set perfectly, but
  the "bad" set was one codec and the "good" set another — it was detecting the codec.
  Always validate against held-out known-good files.

## Filesystems

- **Two heavy writers at once can flip an exFAT mount read-only** mid-job. Serialise.
- **Parallel seek-heavy reads destabilise it too, not just writes.** A 6-worker sweep
  reported 326 broken files; a serial recheck confirmed 2. Sequential is gentler but not
  unlimited — 6 concurrent demux readers still stalled the mount 28 times.
- **"Device not configured" or "No such file" from a mounted volume is a transient stall,
  never a corrupt file.** Verification must have three verdicts — intact / corrupt /
  *unverified* — so a stall is retried rather than reported as damage.
- **exFAT refuses to delete filenames containing certain unicode.** Delete with NFC/NFD
  fallbacks.

## Season numbering

- **Databases disagree, especially for anime.** The same series is split 52/52/54/62 by one
  database and 35/48/48/48/41 by another. Check which one your player queries before naming
  files; a mismatch produces missing artwork and wrong episode titles.
- **Absolute episode number is the only stable identifier** across renumberings. Record it.
- **A player merging two copies of a show from two drives will union their season numbers**,
  so a stale copy in an old layout makes the library appear to have extra seasons.

## Process

- **Never pipe a long scan through `head`.** An 80-minute sweep was thrown away because the
  verdict fell past `head -40`. Redirect the whole thing to a file.
- **Verify a replacement is actually better before accepting it.** A re-fetch returned a
  file 40% smaller and measurably worse; keeping it would have been a silent downgrade.
- **A file that decodes without error is not necessarily a good file.** Bitstream-clean and
  visually clean are different properties.
