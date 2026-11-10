# Qualitative Personality Annotator

A free, open-source tool for **manual personality annotation of video, audio or text**.
Annotators watch, listen to or read an item, mark the segments they think reveal
personality, and rate one or more variables per segment (e.g. HEXACO domains or facets,
narcissism, depression) with a score, a written explanation and optional notes.
Researchers then see which parts of each item were marked most often, export
everything in long format, and cut the annotated segments into clips for AI training
or rater training.

This is the **pilot (local) version**: it runs on one computer and opens in the browser.
Items can come from a local folder or a public Zenodo record.

![Annotating a video](docs/video_annotate.png)

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Annotating](#annotating)
- [Results, exports and clips](#results-exports-and-clips)
- [Several raters on different computers](#several-raters-on-different-computers)
- [How annotations are counted](#how-annotations-are-counted)
- [Limitations of this version](#limitations-of-this-version)
- [For developers](#for-developers)

## Quick start

### Option A: the ready-made program (no Python needed)

1. Go to the **Actions** tab of this repository, open the latest successful
   *test-and-build* run and download the zip for your system
   (`PersonalityAnnotator-windows`, `-macos` or `-linux`). Tagged versions also appear
   under **Releases**.
2. Unzip it. The folder contains the program, a `config.yaml` and `sample_data`.
3. Start `PersonalityAnnotator` (on Windows: `PersonalityAnnotator.exe`). A console
   window opens and your browser shows the tool at `http://127.0.0.1:8765`.
   Keep the console window open while you work; closing it stops the tool.

The program is not code-signed, so your system will warn you the first time:

- **Windows**: "Windows protected your PC" → *More info* → *Run anyway*.
- **macOS**: right-click the program → *Open* → *Open*. If macOS still refuses, run
  `xattr -d com.apple.quarantine PersonalityAnnotator` in Terminal in that folder.
  You may also need `chmod +x PersonalityAnnotator`.

### Option B: run from source (Python 3.10+)

```bash
git clone https://github.com/Antonis-Koutsoumpis/qualitative-personality-annotator
cd qualitative-personality-annotator
pip install -r requirements.txt
python tools/make_sample_data.py           # optional: synthetic test items
python -m personality_annotator --config examples/config_video.yaml
```

There are example configs for each modality in `examples/`
(`config_video.yaml`, `config_audio.yaml`, `config_text.yaml`, `config_zenodo.yaml`).

Useful options: `--port 8765`, `--no-browser`, and `--host 0.0.0.0` (lets other
computers on the same network reach the tool, for supervised sessions with several
annotators on one machine's database).

Pages:

| Page | Address | For |
|---|---|---|
| Annotator login | `http://127.0.0.1:8765/` | annotators |
| Admin | `http://127.0.0.1:8765/admin` | the researcher |

To password-protect the admin page, set the environment variable
`ANNOTATOR_ADMIN_PASSWORD` before starting the tool.

## Configuration

Everything is set in `config.yaml` (fully commented; see
[`personality_annotator/config.example.yaml`](personality_annotator/config.example.yaml)).
Restart the tool after editing it.

| Setting | Meaning |
|---|---|
| `modality` | `video`, `audio` or `text`; one modality per project |
| `sources` | where items come from (see below); several sources may be combined |
| `annotators_per_item` | how many different annotators should rate each item |
| `scale` | one rating scale for all variables (default 1–5) with labels |
| `variables` | what can be rated: `id`, `label`, `group` (for menus) and a `definition` shown to annotators |
| `completion` | share of a video/audio that must actually be played before submitting (default 95 %); for text, scrolling to the end and an optional minimum reading time |
| `coverage_bin_s` | time-bin size for counting overlap in video/audio (default 1 s) |
| `reservation_timeout_h` | an item opened but not finished is released for others after this time |
| `demographics` | age range and gender options asked on first login |
| `instructions` | text shown on the annotators' *Instructions* panel |
| `data_dir` | where the database, caches and temporary files are kept |

### Item sources

```yaml
sources:
  - type: local                 # a folder on this computer
    path: ./my_videos
    recursive: false            # also scan sub-folders
    pattern: "*.mp4"            # optional filter

  - type: zenodo                # a public Zenodo record
    record: https://zenodo.org/records/1234567   # link, DOI or record number
    pattern: "*.mp4"

  - type: text_csv              # text only: one row per text
    path: ./transcripts.csv     # or url: https://...
    id_column: item_id
    text_column: text
```

Supported files: video `.mp4 .webm .m4v .mov .ogv`; audio
`.mp3 .wav .m4a .ogg .flac .aac .opus`; text `.txt .md`. Use MP4 (H.264/AAC) for video;
it plays in every browser.

The item id is the file name without extension. Items are scanned at start-up and with
**Re-scan sources** on the admin page. Files that disappear are hidden, never deleted,
and their annotations are kept.

Zenodo files are streamed through the tool, so seeking works. Set `cache_media: true`
to download each file once into `data_dir/media_cache` instead. Note that a Zenodo
record holds at most 100 files, so larger collections need several records.

## Annotating

1. The annotator enters a **participant code** (no names or e-mail addresses). On the
   first login they give their age and gender. With the same code they can stop and
   continue later.
2. The tool presents items **one at a time, in random order**. No annotator sees the
   same item twice. Items with the fewest annotations are offered first, so
   annotations stay evenly spread until every item has reached
   `annotators_per_item`.
3. To mark a segment:
   - **Video/audio**: press `[` at the start and `]` at the end (or type the times, or
     use the *Now* buttons; for audio you can also drag across the waveform). Check it
     with *Preview loop* (`P`).
   - **Text**: select words with the mouse; the selection snaps to whole words.
4. Rate the segment on one or more variables: choose the variable, a score, and explain
   why (required). Notes are optional. Segments may overlap.
5. *Submit* becomes available when the item has been watched/listened to (or read to
   the end) **and** there is at least one segment, or the box *No personality
   information in this item* is ticked with an explanation.

Keyboard: `Space` play/pause · `←`/`→` 1 s · `Shift`+`←`/`→` 0.1 s · `[` start ·
`]` end · `P` preview.

Annotators only ever see their own annotations. Work is saved on the computer as they
go: segments immediately, and the half-filled segment form as a draft in the browser.

![Annotating text](docs/text_annotate.png)

## Results, exports and clips

The **admin page** has three tabs:

- **Overview**: progress per item (completed vs. target) and per annotator, the
  sources and any scan errors, and the explanations given for "no personality
  information".
- **Results**: per item, a heatmap of how many raters marked each moment (or each word)
  for each variable. You can colour it by the number of raters or by the mean score
  (low ↔ high), filter by rater, and hover for scores and explanations. Clicking the
  heatmap jumps the player to that moment. There is a table of **peak segments**
  (continuous stretches marked by at least *n* raters) and of all individual
  segments; each can be downloaded as a clip.
- **Export & import**: CSV downloads, extraction of all peaks, and full backups.

![Results view](docs/video_admin_results.png)

### Exported tables (long format, UTF-8 CSV)

| File | One row per | Key columns |
|---|---|---|
| `annotations_long.csv` | rating | `annotator_code`, `item_id`, `segment_id`, `start_s`, `end_s` (video/audio) or `char_start`, `char_end`, `span_text` (text), `variable`, `score`, `explanation`, `notes`, timestamps, `instance_id` |
| `coverage_long.csv` | item × variable × time bin (or word) | `n_annotators`, `mean_score`, `sd_score`, `n_low`/`n_mid`/`n_high` (relative to the scale midpoint), `annotators` |
| `peak_segments.csv` | stretch with ≥ *n* raters | `start_s`/`end_s` (or `start_char`/`end_char` + `span_text`), `max_n_annotators`, `mean_score`, `n_ratings`, `annotators` |
| `assignments.csv` | annotator × item | status, watched proportion / reading time, no-information flag and explanation |
| `annotators.csv` | annotator | code, age, gender |

By default only **submitted** items are included; tick *include unsubmitted work* to
see work in progress. In `coverage_long.csv`, bins or words nobody marked are left out
unless you tick *include empty bins / words*.

Every row carries the `annotator_code`, so raters are always distinguishable. The
`instance_id` additionally identifies the computer (database) a rating was made on.

### Clips

Downloads are zip files. Each segment comes as a clip (`.mp4` for video, the original
audio format for audio, `.txt` for text) plus a `.json` and a `.csv` with its ratings,
and an `index.csv` lists everything. Video is re-encoded for frame-accurate cuts.

For large batches, or with the material on another computer, use the command-line
script on exported CSVs:

```bash
# one clip per annotator segment
python tools/extract_segments.py --csv annotations_long.csv --media-dir ./videos --out ./clips
# one clip per peak, with the overlapping ratings
python tools/extract_segments.py --csv peak_segments.csv --annotations annotations_long.csv \
    --media-dir ./videos --out ./peak_clips --min-raters 2
```

## Several raters on different computers

In this version each computer has its own database, so live assignment across laptops
isn't possible. To combine raters:

1. Each rater (or you, on their computer) opens the admin page → *Export & import* →
   **Download full export** (`full_export.json`).
2. On your computer, **Import a full export…** for each file.

All ids are unique across computers, so nothing collides and importing the same file
twice adds nothing. The results view and all exports then include every rater.
Make sure raters use **different participant codes**; the import warns if the same
code rated the same item on two computers. For text, the import also checks that both
computers used identical text, so character positions match.

On separate computers, each rater works through every item their computer's sources
contain (`annotators_per_item` only applies within one database). To split the work,
point each computer at a different folder or Zenodo `pattern`; to have everyone rate
everything, give them all the same sources.

Several annotators can also share **one** database during a supervised session: start
the tool with `--host 0.0.0.0` and let them open `http://<that computer's IP>:8765`.
Then the assignment rules (no repeats, even spread, `annotators_per_item`) apply
across all of them.

## How annotations are counted

- Video/audio time is divided into bins (default 1 s). A rating covers every bin its
  segment overlaps.
- Text is divided into words; a rating covers every word its span overlaps.
- Per bin/word and variable, each annotator counts **once**. If one annotator has
  several overlapping segments rated on the same variable, their scores are averaged
  for that bin.
- `n_low`, `n_mid` and `n_high` count annotators whose (averaged) score is below, at or
  above the scale midpoint, so "often marked as high" and "often marked as low" can be
  separated.
- Peak segments merge consecutive bins/words that reach the threshold. For video/audio,
  their edges are tightened to where the overlapping annotations actually start and
  end.

## Limitations of this version

- Local only: no central server, no live synchronisation between computers (use full
  export/import).
- Sources: local folders and public Zenodo records. OSF, Yoda (WebDAV) and other
  private storage are planned; the source code is structured so they can be added in
  `personality_annotator/sources.py`.
- One shared rating scale for all variables.
- The admin page is open to anyone who can reach the tool unless
  `ANNOTATOR_ADMIN_PASSWORD` is set. Annotator logins are by code only. This is
  fine on one computer but not meant for public internet hosting.
- The downloadable programs are not code-signed (see the warnings above).

## For developers

```
personality_annotator/
  __main__.py      start-up (python -m personality_annotator)
  app.py           FastAPI app: annotator API, admin API, media streaming
  config.py        config.yaml loading and validation
  db.py            SQLite schema (UUID ids, audit log of every change)
  sources.py       local / Zenodo / text_csv sources, syncing items
  assignment.py    which item an annotator gets next
  analysis.py      coverage and peak computation (pure Python)
  exports.py       CSV/JSON exports, importing another computer's export
  extract.py       ffmpeg cutting and zip bundling
  static/          HTML/CSS/JS front-end (wavesurfer.js vendored, BSD-3)
tools/
  make_sample_data.py   synthetic videos/audio/texts for testing
  extract_segments.py   command-line extraction from exported CSVs
tests/                  pytest suite (assignment rules, coverage, exports, merging)
```

```bash
pip install -r requirements-dev.txt
python tools/make_sample_data.py
pytest -q
```

GitHub Actions (`.github/workflows/build.yml`) runs the tests and builds the Windows,
macOS and Linux programs with PyInstaller on every push to `main`. Push a tag such as
`v0.1.0` to publish them as a release.

## Licence

MIT. wavesurfer.js is included under its BSD-3-Clause licence
(`personality_annotator/static/vendor/wavesurfer-LICENSE.txt`).
