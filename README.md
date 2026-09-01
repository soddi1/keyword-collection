# YouTube Keyword Crawler - Stage 1

Interactive keyword-discovery crawler. For each seed keyword it searches
YouTube, collects full metadata for every result, extracts content words from
the title + tags + first 500 chars of the description, and asks you to label
each previously-unseen word as relevant (`1`) or irrelevant (`0`). It stops a
keyword once 200 consecutive videos produce no new relevant keyword (or the
API runs out of pages), then moves to the next seed.

This is the first stage of a larger iterative crawling pipeline; the discovery
graph it records (which keyword found which word, via which video) is what the
later additive stages build on.

## Files

| File | Purpose |
|------|---------|
| `config.py` | All tunables: seed keywords, API params, thresholds, paths |
| `crawl.py` | The engine: API client, extraction, storage, review loop |
| `analysis.py` | Saturation plot + decision flowchart from the checkpoint |
| `requirements.txt` | Python dependencies |

## Setup

```bash
python3 -m pip install -r requirements.txt
```

Put your API key in `.env` (already present here):

```
YouTube_API_KEY=<your key>
```

## Run

Smoke test first (no prompts, one page, cheap):

```bash
python3 crawl.py --keywords keyboard --max-pages 1 --auto-label none
```

Real interactive run over all seed keywords:

```bash
python3 crawl.py
```

Useful flags:

- `--keywords "mechanical keyboard" typing` - override the seed set
- `--max-pages N` - cap pages per keyword (API max ~10)
- `--auto-label {all,none}` - auto-answer every word (testing only)
- `--budget N` - refuse to spend past N quota units this run
- `--resume` - continue from the last checkpoint
- `--fresh` - ignore any checkpoint and start clean

## Shorts are excluded

The Data API has no official "is this a Short" flag, so `config.py` uses
YouTube's current Shorts duration cap as a proxy: any video with
`duration_seconds <= SHORTS_MAX_DURATION_SECONDS` (default 180, i.e. 3
minutes) is dropped before any metadata is stored or words are extracted. It
still counts as a dry result towards the 200-video saturation streak, since it
could never have contributed a new keyword. Toggle with `EXCLUDE_SHORTS =
False` or adjust `SHORTS_MAX_DURATION_SECONDS` in `config.py` if this proxy is
too aggressive or too lenient for your niche.

During labelling: `1` = relevant, `0` = irrelevant, `s` = skip this word
without judging it, `b` = back (re-mark the previous word), `q` = save and
quit (resume later with `--resume`).

The `b` option appears only when there is a previous word to return to. Press
it repeatedly to step further back. Undoing a decision is clean: the word is
removed from the in-memory store and the txt files are rewritten from it, so
nothing needs manual deletion. Back is scoped to the current video, because
the 200-dry-video saturation counter is only evaluated once every word in a
video has been decided.

## Analysis

After a run (or any smoke test), generate the plots and flowchart:

```bash
python3 analysis.py
```

## Outputs (in `output/`)

| File | Contents |
|------|----------|
| `relevant_keywords.txt` / `.csv` | Words you marked relevant (+ first-seen metadata) |
| `irrelevant_keywords.txt` / `.csv` | Words you marked irrelevant |
| `videos_master.csv` | One deduped row per unique video; the dedup ledger |
| `discovery_edges.csv` | Graph edges: seed_keyword -> video_id -> word |
| `crawl_state.json` | Full checkpoint (written after every labelled word) |
| `stage1_results.xlsx` | All sheets combined into one workbook |
| `saturation.png` / `saturation_data.csv` | Saturation evidence + raw series |
| `flowchart.md` / `flowchart.png` | Decision flowchart with real run counts |

## Quota

`search.list` costs 100 units; the other calls cost 1 each. The default daily
quota is 10,000 units, so a full 8-keyword run (~8,000+ units) is about one run
per day. The client tracks spend and refuses a call that would exceed
`--budget`; if you hit the ceiling it checkpoints so you can resume tomorrow
with `--resume`.
