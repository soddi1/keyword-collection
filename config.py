"""Configuration for the YouTube keyword crawler (Stage 1).

Everything that you might reasonably want to tweak lives here so the main
`crawl.py` logic stays clean. Nothing in this file talks to the network.
"""

from __future__ import annotations

import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
ENV_PATH = BASE_DIR / ".env"

API_KEY_ENV_NAME = "YouTube_API_KEY"

# Seed keywords (Stage 1 starting set)
SEED_KEYWORDS = [
    "keyboard",
    "typing",
    "keystrokes",
    "keyboarding",
    "typing on keyboard",
    "using keyboard",
    "mechanical keyboard",
    "new keyboard",
]

# YouTube search.list parameters
# NOTE: `type="video"` is added on top of the values you specified. The
# search endpoint otherwise mixes in channels/playlists that have no
# id.videoId, which would leave holes in the metadata and the rank numbering.
SEARCH_PARAMS = {
    "part": "id,snippet",
    "maxResults": 50,
    "order": "relevance",
    "relevanceLanguage": "en",
    "type": "video",
}
CATEGORY_REGION_CODE = "US"

# Crawl thresholds
# keyword ("dry" videos). 200 videos == 4 pages of 50.
SATURATION_LIMIT = 200

# How many characters of the description feed the word extractor.
DESC_CHARS = 1000

# Candidate words shorter than this are dropped (after stopword removal).
MIN_TOKEN_LEN = 3

# The YouTube Data API refuses to paginate past ~500 search results, so a
# single keyword can never exceed this many pages regardless of thresholds.
MAX_PAGES_PER_KEYWORD = 10

# Shorts exclusion
EXCLUDE_SHORTS = True
SHORTS_MAX_DURATION_SECONDS = 180

# Quota accounting
# Default daily quota is 10,000 units. search.list = 100 units; videos.list,
# channels.list and videoCategories.list = 1 unit each. The client refuses a
# call that would push cumulative spend past QUOTA_BUDGET.
QUOTA_COST = {
    "search": 100,
    "videos": 1,
    "channels": 1,
    "videoCategories": 1,
}
QUOTA_BUDGET = 10_000

# Output file names (created inside OUTPUT_DIR)
RELEVANT_TXT = "relevant_keywords.txt"
IRRELEVANT_TXT = "irrelevant_keywords.txt"
RELEVANT_CSV = "relevant_keywords.csv"
IRRELEVANT_CSV = "irrelevant_keywords.csv"
VIDEOS_MASTER_CSV = "videos_master.csv"
DISCOVERY_EDGES_CSV = "discovery_edges.csv"
SATURATION_DATA_CSV = "saturation_data.csv"
CHECKPOINT_JSON = "crawl_state.json"
RESULTS_XLSX = "stage1_results.xlsx"

# Analysis artefacts
SATURATION_PNG = "saturation.png"
FLOWCHART_MD = "flowchart.md"
FLOWCHART_PNG = "flowchart.png"

# Column order for the per-video metadata block. Reused by videos_master and
# embedded (prefixed) inside the keyword sheets as "first-seen" metadata.
VIDEO_COLUMNS = [
    "video_id",
    "title",
    "url",
    "description",
    "tags",
    "channel_id",
    "channel_title",
    "published_at",
    "duration_iso",
    "duration_seconds",
    "category_id",
    "category_name",
    "view_count",
    "like_count",
    "comment_count",
    "subscriber_count",
    "search_rank",
    "search_page",
    "seed_keyword",
    "discovery_path",
    "discovery_depth",
    "fetched_at",
]


def get_api_key() -> str:
    """Load the API key from .env (falling back to the process env)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH)
    except Exception:
        pass
    key = os.environ.get(API_KEY_ENV_NAME)
    if not key:
        raise RuntimeError(
            f"{API_KEY_ENV_NAME} not found. Add it to {ENV_PATH} as "
            f"'{API_KEY_ENV_NAME}=<your key>'."
        )
    return key
