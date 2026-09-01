"""YouTube keyword crawler - Stage 1 (single-file engine).

This one file contains everything except configuration (`config.py`) and the
post-hoc analysis (`analysis.py`):

  * YouTubeClient  - thin API wrapper with quota accounting + retry/backoff
  * extraction     - title + tags + first N chars of description -> unigrams
  * Storage        - txt / csv / json / xlsx persistence + dedup ledger
  * Reviewer       - interactive one-word-at-a-time 0/1 labelling
  * Crawler        - the per-keyword paging loop with the saturation rule

Run `python3 crawl.py --help` for the CLI.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import config


# Small helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_duration_to_seconds(iso: str) -> Optional[int]:
    """Convert an ISO-8601 duration (e.g. 'PT3M12S') to whole seconds."""
    if not iso:
        return None
    m = re.match(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        iso,
    )
    if not m:
        return None
    days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _setup_ansi() -> bool:
    """Return True if ANSI escape codes work in this terminal.

    On Windows, attempt to enable VT100/ANSI processing via the Win32 console
    API. Succeeds silently on Windows Terminal, PowerShell, and most modern
    terminals; falls back gracefully on plain cmd.exe where it would render
    raw escape bytes instead of formatting.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        h = ctypes.windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(
                h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
            return True
    except Exception:
        pass
    return False


_ANSI = _setup_ansi()
# Bold the word in terminals that support it; use >>> word <<< elsewhere.
_BOLD = "\033[1m" if _ANSI else ">>> "
_RESET = "\033[0m" if _ANSI else " <<<"


# YouTube API client
class QuotaExceeded(RuntimeError):
    """Raised when a call would push spend past the configured budget."""


class YouTubeClient:
    """Wraps the Data API v3 with quota tracking and simple backoff."""

    def __init__(self, api_key: str, budget: int = config.QUOTA_BUDGET):
        from googleapiclient.discovery import build

        self._yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self.budget = budget
        self.units_spent = 0
        self._category_cache: Optional[Dict[str, str]] = None

    # -- quota -----------------------------------------------------------
    def _charge(self, endpoint: str) -> None:
        cost = config.QUOTA_COST[endpoint]
        if self.units_spent + cost > self.budget:
            raise QuotaExceeded(
                f"Call to {endpoint} (+{cost}) would exceed budget "
                f"{self.budget}; already spent {self.units_spent}."
            )
        self.units_spent += cost

    # -- generic execute with retry -------------------------------------
    def _execute(self, request, endpoint: str, retries: int = 4):
        from googleapiclient.errors import HttpError

        self._charge(endpoint)
        delay = 2.0
        for attempt in range(retries):
            try:
                return request.execute()
            except HttpError as e:
                status = getattr(e.resp, "status", None)
                # Hard quota errors are not worth retrying.
                if status == 403 and b"quotaExceeded" in (e.content or b""):
                    raise QuotaExceeded(str(e)) from e
                # Transient: 500/503, and soft 403 (rateLimit). Back off.
                if status in (500, 503, 403) and attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    # -- search ----------------------------------------------------------
    def search(self, keyword: str, page_token: Optional[str] = None) -> dict:
        params = dict(config.SEARCH_PARAMS)
        params["q"] = keyword
        if page_token:
            params["pageToken"] = page_token
        req = self._yt.search().list(**params)
        return self._execute(req, "search")

    # -- videos.list -----------------------------------------------------
    def videos(self, video_ids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i : i + 50]
            req = self._yt.videos().list(
                part="snippet,contentDetails,statistics", id=",".join(chunk)
            )
            resp = self._execute(req, "videos")
            for item in resp.get("items", []):
                out[item["id"]] = item
        return out

    # -- channels.list ---------------------------------------------------
    def channels(self, channel_ids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        uniq = list(dict.fromkeys(channel_ids))  # preserve order, dedup
        for i in range(0, len(uniq), 50):
            chunk = uniq[i : i + 50]
            req = self._yt.channels().list(part="statistics", id=",".join(chunk))
            resp = self._execute(req, "channels")
            for item in resp.get("items", []):
                out[item["id"]] = item
        return out

    # -- videoCategories.list (cached) ----------------------------------
    def categories(self) -> Dict[str, str]:
        if self._category_cache is None:
            req = self._yt.videoCategories().list(
                part="snippet", regionCode=config.CATEGORY_REGION_CODE
            )
            resp = self._execute(req, "videoCategories")
            self._category_cache = {
                item["id"]: item["snippet"]["title"] for item in resp.get("items", [])
            }
        return self._category_cache


# Metadata assembly
def build_video_row(
    video_id: str,
    search_item: dict,
    detail: Optional[dict],
    channel: Optional[dict],
    categories: Dict[str, str],
    seed_keyword: str,
    search_rank: int,
    search_page: int,
) -> dict:
    """Merge search + videos + channels payloads into one flat metadata row.

    Hidden statistics (likes/comments/subscribers the owner has hidden) are
    left as "" so an empty cell is distinguishable from a genuine zero.
    """
    snippet = (detail or {}).get("snippet", {}) or search_item.get("snippet", {})
    content = (detail or {}).get("contentDetails", {})
    stats = (detail or {}).get("statistics", {})
    ch_stats = (channel or {}).get("statistics", {})

    duration_iso = content.get("duration", "")
    category_id = snippet.get("categoryId", "")

    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "description": snippet.get("description", ""),
        "tags": "|".join(snippet.get("tags", []) or []),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "published_at": snippet.get("publishedAt", ""),
        "duration_iso": duration_iso,
        "duration_seconds": iso_duration_to_seconds(duration_iso),
        "category_id": category_id,
        "category_name": categories.get(category_id, ""),
        "view_count": stats.get("viewCount", ""),
        "like_count": stats.get("likeCount", ""),
        "comment_count": stats.get("commentCount", ""),
        "subscriber_count": ""
        if ch_stats.get("hiddenSubscriberCount")
        else ch_stats.get("subscriberCount", ""),
        "search_rank": search_rank,
        "search_page": search_page,
        "seed_keyword": seed_keyword,
        "discovery_path": seed_keyword,  # Stage 1: path is just the seed
        "discovery_depth": 0,
        "fetched_at": now_iso(),
    }


# Word extraction
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_NON_WORD_RE = re.compile(r"[^a-z0-9']+")


def _load_stopwords() -> frozenset:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

    return ENGLISH_STOP_WORDS


STOPWORDS = _load_stopwords()


def extract_candidates(row: dict) -> List[str]:
    """Return an ordered, de-duplicated list of candidate unigrams.

    Source text = title + tags + first DESC_CHARS chars of description.
    Order of first appearance is preserved so the reviewer sees words in a
    stable, meaningful sequence.
    """
    parts = [
        row.get("title", ""),
        (row.get("tags", "") or "").replace("|", " "),
        (row.get("description", "") or "")[: config.DESC_CHARS],
    ]
    text = " ".join(parts).lower()
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"[#@]\w+", " ", text)  # drop #hashtags / @mentions
    text = _NON_WORD_RE.sub(" ", text)

    seen = set()
    ordered: List[str] = []
    for tok in text.split():
        tok = tok.strip("'")
        if len(tok) < config.MIN_TOKEN_LEN:
            continue
        if tok.isdigit():
            continue
        if tok in STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        ordered.append(tok)
    return ordered


# Storage
class Storage:
    """Owns every output file plus the in-memory dedup/labelling state."""

    def __init__(self, output_dir: Path):
        self.dir = output_dir
        self.dir.mkdir(parents=True, exist_ok=True)

        # video_id -> full metadata row (dedup ledger + master sheet source)
        self.videos: Dict[str, dict] = {}
        # keyword -> label record (label 0/1, counts, first-seen metadata...)
        self.keywords: Dict[str, dict] = {}
        # discovery edges: seed_keyword -> video_id -> word
        self.edges: List[dict] = []

        self.seed_set = {k.lower() for k in config.SEED_KEYWORDS}

    # -- paths -----------------------------------------------------------
    def _p(self, name: str) -> Path:
        return self.dir / name

    # -- known-word test -------------------------------------------------
    def is_known(self, word: str) -> bool:
        return word in self.seed_set or word in self.keywords

    # -- keyword bookkeeping --------------------------------------------
    def bump(self, word: str) -> None:
        """A word we've already judged shows up again: raise its counter."""
        if word in self.keywords:
            self.keywords[word]["occurrence_count"] += 1

    def record_keyword(
        self,
        word: str,
        label: int,
        video_row: dict,
        discovered_from: str,
    ) -> None:
        """First-time judgement of a candidate word."""
        rec = {
            "keyword": word,
            "label": label,
            "occurrence_count": 1,
            "first_seen_video_id": video_row["video_id"],
            "source_video_ids": [video_row["video_id"]],
            "discovered_from_keyword": discovered_from,
            "discovery_path": video_row.get("discovery_path", discovered_from),
            "discovery_depth": video_row.get("discovery_depth", 0),
            "judged_at": now_iso(),
            # embed the first-seen video metadata (prefixed to avoid clashes)
            **{f"video_{k}": v for k, v in video_row.items()},
        }
        self.keywords[word] = rec
        self._rewrite_txts()

    def revert_keyword(self, word: str) -> None:
        """Undo a first-time judgement so the word can be re-marked.

        Clean by construction: the record is dropped from the in-memory store
        (the source of truth for both txt and csv), its discovery edge is
        removed, and the txt files are rewritten from scratch. No line-level
        deletion or spreadsheet surgery is ever needed.
        """
        self.keywords.pop(word, None)
        for i in range(len(self.edges) - 1, -1, -1):
            if self.edges[i]["word"] == word:
                del self.edges[i]
                break
        self._rewrite_txts()

    def add_source(self, word: str, video_id: str) -> None:
        rec = self.keywords.get(word)
        if rec and video_id not in rec["source_video_ids"]:
            rec["source_video_ids"].append(video_id)

    def _rewrite_txts(self) -> None:
        """Rewrite both keyword txt files from the in-memory store.

        Full rewrite (rather than append) so that reverting a decision is
        trivially reflected. Files are small, so the cost is negligible next
        to human labelling speed.
        """
        rel = [w for w, r in self.keywords.items() if r["label"] == 1]
        irr = [w for w, r in self.keywords.items() if r["label"] == 0]
        with open(self._p(config.RELEVANT_TXT), "w", encoding="utf-8") as f:
            f.write("\n".join(rel) + ("\n" if rel else ""))
        with open(self._p(config.IRRELEVANT_TXT), "w", encoding="utf-8") as f:
            f.write("\n".join(irr) + ("\n" if irr else ""))

    # -- video ledger ----------------------------------------------------
    def has_video(self, video_id: str) -> bool:
        return video_id in self.videos

    def add_video(self, row: dict) -> None:
        self.videos[row["video_id"]] = row

    def add_edge(self, seed_keyword: str, video_id: str, word: str, label: int) -> None:
        self.edges.append(
            {
                "seed_keyword": seed_keyword,
                "video_id": video_id,
                "word": word,
                "label": label,
                "created_at": now_iso(),
            }
        )

    # -- CSV writers -----------------------------------------------------
    def flush_csvs(self) -> None:
        self._write_videos_master()
        self._write_keyword_csv(label=1, path=config.RELEVANT_CSV)
        self._write_keyword_csv(label=0, path=config.IRRELEVANT_CSV)
        self._write_edges()

    def _write_videos_master(self) -> None:
        rows = list(self.videos.values())
        self._write_csv(config.VIDEOS_MASTER_CSV, config.VIDEO_COLUMNS, rows)

    def _keyword_columns(self) -> List[str]:
        base = [
            "keyword",
            "label",
            "occurrence_count",
            "first_seen_video_id",
            "source_video_ids",
            "discovered_from_keyword",
            "discovery_path",
            "discovery_depth",
            "judged_at",
        ]
        video_cols = [f"video_{c}" for c in config.VIDEO_COLUMNS]
        return base + video_cols

    def _write_keyword_csv(self, label: int, path: str) -> None:
        cols = self._keyword_columns()
        rows = []
        for rec in self.keywords.values():
            if rec["label"] != label:
                continue
            r = dict(rec)
            r["source_video_ids"] = "|".join(r["source_video_ids"])
            rows.append(r)
        self._write_csv(path, cols, rows)

    def _write_edges(self) -> None:
        cols = ["seed_keyword", "video_id", "word", "label", "created_at"]
        self._write_csv(config.DISCOVERY_EDGES_CSV, cols, self.edges)

    def _write_csv(self, name: str, cols: List[str], rows: List[dict]) -> None:
        with open(self._p(name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    # -- checkpoint ------------------------------------------------------
    def save_checkpoint(self, progress: dict) -> None:
        """Atomic-ish JSON dump of the full crawl state after each label."""
        state = {
            "saved_at": now_iso(),
            "progress": progress,
            "videos": self.videos,
            "keywords": self.keywords,
            "edges": self.edges,
        }
        tmp = self._p(config.CHECKPOINT_JSON + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self._p(config.CHECKPOINT_JSON))

    def load_checkpoint(self) -> Optional[dict]:
        p = self._p(config.CHECKPOINT_JSON)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            state = json.load(f)
        self.videos = state.get("videos", {})
        self.keywords = state.get("keywords", {})
        self.edges = state.get("edges", [])
        return state.get("progress", {})

    # -- final workbook --------------------------------------------------
    def write_workbook(self) -> None:
        import pandas as pd

        self.flush_csvs()
        path = self._p(config.RESULTS_XLSX)
        with pd.ExcelWriter(path, engine="openpyxl") as xl:
            self._sheet(xl, config.VIDEOS_MASTER_CSV, "videos_master")
            self._sheet(xl, config.RELEVANT_CSV, "relevant_keywords")
            self._sheet(xl, config.IRRELEVANT_CSV, "irrelevant_keywords")
            self._sheet(xl, config.DISCOVERY_EDGES_CSV, "discovery_edges")

    def _sheet(self, xl, csv_name: str, sheet: str) -> None:
        import pandas as pd

        p = self._p(csv_name)
        df = pd.read_csv(p) if p.exists() else pd.DataFrame()
        # Excel caps sheet names at 31 chars.
        df.to_excel(xl, sheet_name=sheet[:31], index=False)


# Interactive reviewer
class Reviewer:
    """Prompts the user 0/1 for one candidate word at a time.

    auto_label:
      None   -> interactive (real run)
      "all"  -> auto-answer 1 for everything (smoke tests)
      "none" -> auto-answer 0 for everything (smoke tests)
    """

    QUIT = "__QUIT__"
    SKIP = "__SKIP__"
    BACK = "__BACK__"

    def __init__(self, auto_label: Optional[str] = None):
        self.auto_label = auto_label

    def ask(self, word: str, context: dict, can_go_back: bool = False, first_word: bool = True) -> object:
        if self.auto_label == "all":
            return 1
        if self.auto_label == "none":
            return 0

        back = " / b=back" if can_go_back else ""
        # Show seed + title only on the first word of each video; for
        # subsequent words of the same video it adds noise without value.
        header = ""
        if first_word:
            title = context.get("title", "")
            kw = context.get("seed_keyword", "")
            header = f"\n  seed='{kw}'  video='{title[:70]}'\n"
        prompt = (
            f"{header}"
            f"  Relevant? word = {_BOLD}{word}{_RESET}  "
            f"[1=yes / 0=no / s=skip{back} / q=save+quit]: "
        )
        while True:
            try:
                ans = input(prompt).strip().lower()
            except EOFError:
                return self.QUIT
            if ans == "1":
                return 1
            if ans == "0":
                return 0
            if ans == "s":
                return self.SKIP
            if ans == "b" and can_go_back:
                return self.BACK
            if ans == "q":
                return self.QUIT
            allowed = "1, 0, s" + (", b" if can_go_back else "") + ", or q"
            print(f"    Please enter {allowed}.")


# Crawler
class Crawler:
    def __init__(
        self,
        client: YouTubeClient,
        storage: Storage,
        reviewer: Reviewer,
        keywords: List[str],
        max_pages: Optional[int] = None,
    ):
        self.client = client
        self.storage = storage
        self.reviewer = reviewer
        self.keywords = keywords
        self.max_pages = max_pages or config.MAX_PAGES_PER_KEYWORD

        # Progress that survives across resume. Keyed for the saturation plot.
        self.progress = {
            "completed_keywords": [],
            "videos_processed": 0,
            "series": [],  # per-video saturation samples
            "stop_reasons": {},
            "shorts_skipped": 0,
        }

    # -- resume ----------------------------------------------------------
    def restore(self, progress: Optional[dict]) -> None:
        if progress:
            self.progress.update(progress)

    # -- per-keyword loop ------------------------------------------------
    def run(self) -> None:
        done = set(self.progress["completed_keywords"])
        for keyword in self.keywords:
            if keyword in done:
                print(f"[skip] '{keyword}' already completed.")
                continue
            try:
                self._crawl_keyword(keyword)
            except QuotaExceeded as e:
                print(f"\n[quota] {e}\n[quota] Saving and exiting; resume later.")
                self.storage.save_checkpoint(self.progress)
                self.storage.flush_csvs()
                return
            except _UserQuit:
                print("\n[quit] Saving and exiting; resume later.")
                self.storage.save_checkpoint(self.progress)
                self.storage.flush_csvs()
                return
            self.progress["completed_keywords"].append(keyword)
            self.storage.save_checkpoint(self.progress)
            self.storage.flush_csvs()

        print(
            f"\n[done] All keywords processed. "
            f"Quota spent: {self.client.units_spent} units."
        )
        self.storage.write_workbook()

    def _crawl_keyword(self, keyword: str) -> None:
        print(f"\n=== Keyword: '{keyword}' ===")
        dry_streak = 0
        page_token = None
        page_num = 0
        global_rank = 0

        while True:
            if page_num >= self.max_pages:
                self._record_stop(keyword, "max_pages")
                break

            resp = self.client.search(keyword, page_token)
            page_num += 1
            items = resp.get("items", [])
            if not items:
                self._record_stop(keyword, "no_items")
                break

            enriched = self._enrich(items)

            for item in items:
                vid = item.get("id", {}).get("videoId")
                if not vid:
                    continue
                global_rank += 1

                if self.storage.has_video(vid):
                    # Already processed under an earlier keyword: still a
                    # "dry" video for this keyword's saturation counter.
                    dry_streak += 1
                    if self._mark_dry(keyword, vid, dry_streak):
                        return
                    continue

                detail, channel = enriched.get(vid, (None, None))

                if config.EXCLUDE_SHORTS:
                    duration_seconds = iso_duration_to_seconds(
                        (detail or {}).get("contentDetails", {}).get("duration", "")
                    )
                    if (
                        duration_seconds is not None
                        and duration_seconds <= config.SHORTS_MAX_DURATION_SECONDS
                    ):
                        # A Short: no metadata stored, no words extracted. It
                        # still can't contribute a new keyword, so it counts
                        # towards the dry-video saturation streak.
                        self.progress["shorts_skipped"] = (
                            self.progress.get("shorts_skipped", 0) + 1
                        )
                        dry_streak += 1
                        if self._mark_dry(keyword, vid, dry_streak):
                            return
                        continue

                row = build_video_row(
                    video_id=vid,
                    search_item=item,
                    detail=detail,
                    channel=channel,
                    categories=self.client.categories(),
                    seed_keyword=keyword,
                    search_rank=global_rank,
                    search_page=page_num,
                )
                self.storage.add_video(row)
                self.progress["videos_processed"] += 1

                produced_relevant = self._review_video(keyword, row)

                if produced_relevant:
                    dry_streak = 0
                else:
                    dry_streak += 1
                self._sample(keyword, vid, produced_relevant, dry_streak)

                if dry_streak >= config.SATURATION_LIMIT:
                    self._record_stop(keyword, "saturation")
                    return

            page_token = resp.get("nextPageToken")
            if not page_token:
                self._record_stop(keyword, "no_next_page")
                break

    # -- enrichment ------------------------------------------------------
    def _enrich(self, items: List[dict]):
        vids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        details = self.client.videos(vids)
        channel_ids = [
            d.get("snippet", {}).get("channelId") for d in details.values()
        ]
        channels = self.client.channels([c for c in channel_ids if c])
        out = {}
        for vid in vids:
            d = details.get(vid)
            ch = None
            if d:
                ch = channels.get(d.get("snippet", {}).get("channelId"))
            out[vid] = (d, ch)
        return out

    # -- per-video review ------------------------------------------------
    def _review_video(self, keyword: str, row: dict) -> bool:
        """Return True if this video produced at least one NEW relevant word.

        The user can step backwards through this video's undecided words with
        'b' (repeatedly, not just once) to re-mark a mistake. Reverting is a
        clean delete + rewrite because the in-memory keyword store is the sole
        source of truth. Back is scoped to the current video: saturation is
        only evaluated after every word here has been decided, so crossing a
        video boundary would corrupt the dry-streak counter.
        """
        vid = row["video_id"]

        # First pass: already-judged words (seed set or earlier videos) just
        # get their occurrence counter bumped. They never need a decision, so
        # they stay out of the back-navigable list below.
        to_decide: List[str] = []
        for word in extract_candidates(row):
            if self.storage.is_known(word):
                self.storage.bump(word)
                self.storage.add_source(word, vid)
            else:
                to_decide.append(word)

        committed: Dict[int, int] = {}  # index in to_decide -> label
        i = 0
        while i < len(to_decide):
            word = to_decide[i]
            ans = self.reviewer.ask(word, row, can_go_back=(i > 0), first_word=(i == 0))

            if ans == Reviewer.QUIT:
                raise _UserQuit()
            if ans == Reviewer.SKIP:
                i += 1
                continue
            if ans == Reviewer.BACK:
                i -= 1
                prev = to_decide[i]
                if i in committed:
                    # Undo the previous decision so it can be re-marked.
                    self.storage.revert_keyword(prev)
                    del committed[i]
                    self.storage.save_checkpoint(self.progress)
                continue

            label = int(ans)
            self.storage.record_keyword(word, label, row, discovered_from=keyword)
            self.storage.add_edge(keyword, vid, word, label)
            committed[i] = label
            # Checkpoint after every single labelled word (crash safety).
            self.storage.save_checkpoint(self.progress)
            i += 1

        return any(label == 1 for label in committed.values())

    # -- shared dry-video bookkeeping -------------------------------------
    def _mark_dry(self, keyword: str, video_id: str, dry_streak: int) -> bool:
        """Record a video (duplicate or Short) that can't yield a new keyword.

        Returns True if this pushed the keyword past the saturation limit
        (caller should stop the keyword).
        """
        self._sample(keyword, video_id, produced_relevant=False, dry=dry_streak)
        if dry_streak >= config.SATURATION_LIMIT:
            self._record_stop(keyword, "saturation")
            return True
        return False

    # -- saturation samples ----------------------------------------------
    def _sample(self, keyword: str, video_id: str, produced_relevant: bool, dry: int):
        self.progress["series"].append(
            {
                "keyword": keyword,
                "video_id": video_id,
                "cumulative_videos": self.progress["videos_processed"],
                "relevant_total": sum(
                    1 for r in self.storage.keywords.values() if r["label"] == 1
                ),
                "produced_relevant": int(produced_relevant),
                "dry_streak": dry,
            }
        )

    def _record_stop(self, keyword: str, reason: str) -> None:
        self.progress["stop_reasons"][keyword] = reason
        print(f"  [stop] '{keyword}' -> {reason}")


class _UserQuit(Exception):
    pass


# CLI
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="YouTube keyword crawler - Stage 1")
    p.add_argument(
        "--keywords",
        nargs="*",
        default=None,
        help="Override the seed keywords (space-separated). Quote multi-word ones.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Cap pages per keyword (default from config, max ~10).",
    )
    p.add_argument(
        "--auto-label",
        choices=["all", "none"],
        default=None,
        help="Skip prompts and auto-answer (for smoke tests only).",
    )
    p.add_argument(
        "--budget",
        type=int,
        default=config.QUOTA_BUDGET,
        help="Max quota units to spend this run.",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from an existing checkpoint if present.",
    )
    p.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore any existing checkpoint and start clean.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    api_key = config.get_api_key()
    client = YouTubeClient(api_key, budget=args.budget)
    storage = Storage(config.OUTPUT_DIR)
    reviewer = Reviewer(auto_label=args.auto_label)

    keywords = args.keywords if args.keywords else list(config.SEED_KEYWORDS)

    crawler = Crawler(
        client=client,
        storage=storage,
        reviewer=reviewer,
        keywords=keywords,
        max_pages=args.max_pages,
    )

    if args.resume and not args.fresh:
        progress = storage.load_checkpoint()
        if progress:
            crawler.restore(progress)
            print(f"[resume] Restored checkpoint from {config.CHECKPOINT_JSON}.")

    try:
        crawler.run()
    except KeyboardInterrupt:
        print("\n[interrupt] Saving checkpoint before exit.")
        storage.save_checkpoint(crawler.progress)
        storage.flush_csvs()
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
