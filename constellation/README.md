# Constellation — watching the graph load

A star chart for `../loader.py`. Every animal that survives entity resolution is a
star; registrations, breed composition, defect panels and DNA cases orbit it; sire
and dam draw the constellation lines in gold. The UI replays a **real load** —
not a mock — event by event, so you can watch dedup happen.

`loader.py` is untouched. `trace_loader.py` subclasses `InMemoryBackend` and
`Loader` and records every graph mutation in the order it actually happened.

## Files

| | |
|---|---|
| `trace_loader.py` | Runs the loader, records the event stream, inlines it into the page. |
| `constellation.template.html` | The UI. Contains `/*__DATA__*/null`, replaced at build time. |
| `constellation.html` | **Open this.** Standalone — data inlined, no external requests. |
| `events.json` | The raw event stream (194 writes for the 12-animal crawl). |
| `constellation-load.mp4` | 20.7 s screen recording of the load, 1280×720. |
| `record_mp4.mjs` | Records that MP4 (see *Recording* below). |
| `capture_stills.mjs` | Same rig, but saves PNG stills at chosen points. |

## Build and run

```bash
python trace_loader.py                    # the six hand-written sample records
python trace_loader.py ../records.jsonl   # output of a real crawl
```

Either rebuilds `constellation.html` around a fresh load and prints the usual
dry-run report. JSON arrays and JSONL both work.

## What you're looking at

The shipped build replays a **real crawl** of the American Chianina Association —
12 animals seeded from `MA430053` (ZNT MOVES LIKE JAGGER), 194 graph writes:

```
26 animals · 36 registrations · 2 associations · 4 breeds · 6 defect loci · 24 parentage links
```

Only 12 records went in, but 26 animals came out. The extra 14 are **parent
stubs** — sires and dams named by a pedigree before their own record was
crawled. They arrive dim grey and brighten when their record lands, which is what
makes load order irrelevant. Six records merged onto a stub that was already
there, and those pulse gold rather than adding a star; that pulse *is* the
resolution ladder firing.

Most animals carry a `MAINE:` cross-registration alongside their `CHIA:` one —
the same animal, registered in two associations, resolved to a single star. That
is the whole point of the graph, and here it is happening on real data.

Defect loci are coloured by the worst result any animal on them returned:
free (muted blue), suspect (orange), carrier (red), affected (bright red).

Transport bar: play/pause, restart, speed (0.5–4×), and a scrubber with a tick at
each record boundary. Hover any star for its properties, EPDs and free-form
attributes. The counters and log on the right are live.

## Recording

There is no ffmpeg here and this box is win32-arm64, so `ffmpeg-static` has no
binary for it. `record_mp4.mjs` instead drives installed Chrome over the DevTools
Protocol and encodes frames with a WASM H.264 encoder:

```bash
npm install h264-mp4-encoder pngjs
node record_mp4.mjs
```

The page exposes `window.__constellation.step(dt)`, which runs exactly one
simulation step and paints one frame. The recorder steps on a fixed `dt` instead
of wall-clock, so the output is frame-accurate and reproducible no matter how slow
each screenshot round-trip is.

Both scripts have absolute paths at the top for Chrome, the page and the output —
edit those if you move things. Node needs a working directory shorter than
Windows' 260-char `MAX_PATH`, which is why they run from a short temp dir.

## Note on timing

The real load is about **0.7 ms**. The replay is deliberately dilated to roughly
21 seconds (halved to ~13 s in the recording) so the resolution ladder is visible.
The masthead reports the true elapsed time.
