"""Hard-coded component-test inputs for the daily news pipeline."""

from __future__ import annotations

from datetime import datetime, timezone


NOW_UTC = datetime(2026, 5, 16, 18, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
NOW_LABEL = "May 16, 2026"


TOPICS = [
    {
        "key": "climate_resilience",
        "title": "Climate Resilience",
        "rationale": "Cities are testing flood defenses and heat plans before summer.",
        "keywords": [
            "climate",
            "resilience",
            "flood",
            "levee",
            "heat",
            "city",
            "infrastructure",
            "adaptation",
        ],
        "boost_phrases": [
            "city flood defenses",
            "extreme heat plan",
            "climate resilience funding",
        ],
        "min_score": 2,
        "max_articles_per_source": 2,
        "frame_tags": ["us", "western"],
    },
    {
        "key": "chip_exports",
        "title": "Chip Export Controls",
        "rationale": "Governments are tightening semiconductor export rules.",
        "keywords": [
            "chip",
            "chips",
            "semiconductor",
            "export",
            "controls",
            "licensing",
            "ai",
            "manufacturing",
        ],
        "boost_phrases": [
            "chip export controls",
            "semiconductor licensing rules",
            "advanced ai chips",
        ],
        "min_score": 2,
        "max_articles_per_source": 2,
        "frame_tags": ["us", "western", "non_western"],
    },
]


RSS_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Fixture Wire</title>
    <item>
      <title>City expands flood defenses after river levee warnings</title>
      <link>https://example.com/climate-levee</link>
      <description>Officials approved climate resilience funding for pumps, levee repairs, and neighborhood cooling centers.</description>
      <pubDate>Sat, 16 May 2026 15:30:00 GMT</pubDate>
      <source>Fixture Wire</source>
    </item>
    <item>
      <title>New chip export controls target advanced AI accelerators</title>
      <link>https://example.com/chip-controls</link>
      <description>Licensing rules for semiconductor shipments were tightened after talks with manufacturing allies.</description>
      <pubDate>Sat, 16 May 2026 16:45:00 GMT</pubDate>
      <source>Fixture Wire</source>
    </item>
    <item>
      <title>Sports club announces preseason schedule</title>
      <link>https://example.com/sports</link>
      <description>The club will play three exhibition games next month.</description>
      <pubDate>Sat, 16 May 2026 17:00:00 GMT</pubDate>
      <source>Fixture Wire</source>
    </item>
  </channel>
</rss>
"""


SCRAPED_ARTICLE_TEXT = (
    "City officials said the new climate resilience package will reinforce river levees, "
    "install backup pumps, and open cooling centers before the summer heat season. "
    "The mayor said inspections found several flood barriers needed repairs after heavy spring rain. "
    "Engineers said the work should reduce neighborhood flood risk, but warned that some roads "
    "may still close during extreme storms."
)


CHIP_ARTICLE_TEXT = (
    "The commerce ministry announced tighter chip export controls covering advanced AI accelerators "
    "and several semiconductor manufacturing tools. Companies must apply for licenses before shipping "
    "the covered chips to restricted destinations. Officials said the rules are meant to slow military "
    "use of advanced computing while keeping some commercial trade open."
)


TOP_FUNNEL_STORIES = [
    {
        "title": "Cities race to reinforce flood defenses before summer storm season",
        "description": "Climate adaptation teams are repairing levees, pumps, and heat shelters.",
        "url": "https://example.com/top-climate",
        "domain": "example.com",
        "providers": ["google_news_top", "bbc_world_top"],
        "provider_details": [
            {"key": "google_news_top", "name": "Google News Top Stories", "frame": "us/western"},
            {"key": "bbc_world_top", "name": "BBC World News", "frame": "global/western"},
        ],
        "frames": ["us/western", "global/western"],
        "pub_date": "Sat, 16 May 2026 14:20:00 GMT",
    },
    {
        "title": "New chip export controls widen licensing for AI semiconductor shipments",
        "description": "Officials added advanced chips and manufacturing tools to the export list.",
        "url": "https://example.com/top-chips",
        "domain": "example.com",
        "providers": ["reuters_top", "ap_top"],
        "provider_details": [
            {"key": "reuters_top", "name": "Reuters", "frame": "global/western"},
            {"key": "ap_top", "name": "Associated Press", "frame": "us/western"},
        ],
        "frames": ["global/western", "us/western"],
        "pub_date": "Sat, 16 May 2026 13:00:00 GMT",
    },
    {
        "title": "Heat plan adds cooling centers as cities prepare for dangerous temperatures",
        "description": "Emergency managers linked the plan to climate resilience and infrastructure upgrades.",
        "url": "https://example.com/top-heat",
        "domain": "example.com",
        "providers": ["al_jazeera_top"],
        "provider_details": [
            {"key": "al_jazeera_top", "name": "Al Jazeera", "frame": "global south/non-western"},
        ],
        "frames": ["global south/non-western"],
        "pub_date": "Sat, 16 May 2026 12:30:00 GMT",
    },
]


ARTICLE_TARGETS = [
    {
        "article_id": "Fixture Wire-climate_resilience-1",
        "source": "Fixture Wire",
        "title": "City expands flood defenses after river levee warnings",
        "pub_date": "Sat, 16 May 2026 15:30:00 GMT",
        "url": "https://example.com/climate-levee",
        "description": "Officials approved climate resilience funding for pumps, levee repairs, and cooling centers.",
        "text": SCRAPED_ARTICLE_TEXT,
        "topic_key": "climate_resilience",
        "topic_title": "Climate Resilience",
        "relevance_score": 6,
    },
    {
        "article_id": "Fixture Wire-chip_exports-1",
        "source": "Fixture Wire",
        "title": "New chip export controls target advanced AI accelerators",
        "pub_date": "Sat, 16 May 2026 16:45:00 GMT",
        "url": "https://example.com/chip-controls",
        "description": "Licensing rules for semiconductor shipments were tightened.",
        "text": CHIP_ARTICLE_TEXT,
        "topic_key": "chip_exports",
        "topic_title": "Chip Export Controls",
        "relevance_score": 6,
    },
    {
        "article_id": "Second Source-climate_resilience-1",
        "source": "Second Source",
        "title": "Cooling centers added to climate resilience plan",
        "pub_date": "Sat, 16 May 2026 12:00:00 GMT",
        "url": "https://example.com/cooling-centers",
        "description": "The city will open cooling centers and inspect flood pumps.",
        "text": SCRAPED_ARTICLE_TEXT,
        "topic_key": "climate_resilience",
        "topic_title": "Climate Resilience",
        "relevance_score": 4,
    },
]


ARTICLE_SUMMARIES = [
    (
        "### City expands flood defenses after river levee warnings\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Sat, 16 May 2026 15:30:00 GMT\n"
        "- URL: https://example.com/climate-levee\n"
        "- Article ID: Fixture Wire-climate_resilience-1\n"
        "- Topic: Climate Resilience\n\n"
        "Summary:\n"
        "City officials approved a climate resilience package focused on river levee repairs, "
        "backup pumps, and cooling centers before summer. Engineers said inspections found "
        "weak flood barriers after heavy spring rain, while officials warned some road closures "
        "could still happen in extreme storms."
    ),
    (
        "### New chip export controls target advanced AI accelerators\n"
        "Metadata:\n"
        "- Source: Fixture Wire\n"
        "- Published: Sat, 16 May 2026 16:45:00 GMT\n"
        "- URL: https://example.com/chip-controls\n"
        "- Article ID: Fixture Wire-chip_exports-1\n"
        "- Topic: Chip Export Controls\n\n"
        "Summary:\n"
        "Officials announced tighter chip export controls for advanced AI accelerators and "
        "semiconductor manufacturing tools. Companies must seek licenses for covered shipments, "
        "and the government said the move is intended to limit military use while preserving "
        "some commercial trade."
    ),
    (
        "### Cooling centers added to climate resilience plan\n"
        "Metadata:\n"
        "- Source: Second Source\n"
        "- Published: Sat, 16 May 2026 12:00:00 GMT\n"
        "- URL: https://example.com/cooling-centers\n"
        "- Article ID: Second Source-climate_resilience-1\n"
        "- Topic: Climate Resilience\n\n"
        "Summary:\n"
        "The city will open additional cooling centers and inspect flood pumps as part of its "
        "climate resilience plan. Officials said the centers would be staffed during extreme "
        "heat days and that pump inspections would continue through the summer."
    ),
]

ARTICLE_SUMMARIES_CLIMATE_PAIR = [
    ARTICLE_SUMMARIES[0],
    ARTICLE_SUMMARIES[2],
]


SYNTHESIS_BODY = (
    "## Climate Resilience\n"
    "Cities are moving from planning to construction as officials approve levee repairs, "
    "backup pumps, and cooling centers before the summer storm and heat season. The reported "
    "plans reduce some near-term risk, but officials still expect disruptions during extreme weather.\n\n"
    "## Chip Export Controls\n"
    "New semiconductor export controls are tightening the licensing path for advanced AI chips "
    "and manufacturing tools. Officials framed the rules as a security measure while leaving "
    "some commercial shipments possible."
)


IMAGE_ART = {
    "final_image_path": "/tmp/news_pipeline_fixture_image.png",
    "overlay_headline": "Cities Prepare for Climate Strain",
    "data_uri": "data:image/png;base64,ZmFrZQ==",
}


RECIPIENTS = ["reader@example.com"]
RECIPIENT_NAMES = ["Reader Example"]
