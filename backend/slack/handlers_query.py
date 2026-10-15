import logging
import os
import re
import urllib.parse
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

from backend.database import search_inventory_summary
from backend.slack.client import post_thread_reply

logger = logging.getLogger(__name__)

# Regex patterns for matching where-is questions and search intents
QUERY_PATTERNS = [
    re.compile(
        r"^(?:hey\s+|hi\s+|hello\s+)?(?:<@[A-Z0-9]+>\s+)?where\s+(?:is|are|can\s+i\s+find)\s+(?:the\s+|a\s+|some\s+)?(.+?)\s*[\?\.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:hey\s+|hi\s+|hello\s+)?(?:<@[A-Z0-9]+>\s+)?where\s+(?:the\s+|a\s+|some\s+)?(.+?)\s+(?:is|are|can\s+be\s+found|at)\s*[\?\.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:hey\s+|hi\s+|hello\s+)?(?:<@[A-Z0-9]+>\s+)?(?:find|locate|search(?:\s+for)?)\s+(?:the\s+|a\s+|some\s+)?(.+?)\s*[\?\.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:anyone|anybody)\s+(?:know\s+)?where\s+(?:the\s+|a\s+|some\s+)?(.+?)\s+(?:is|are|can\s+be\s+found|at)\s*[\?\.]*$",
        re.IGNORECASE,
    ),
]


def strip_bot_mentions(text: str) -> str:
    """Strips Slack user/bot mentions such as <@U12345678> from text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text, flags=re.IGNORECASE).strip()


def parse_location_query(text: str) -> Optional[str]:
    """
    Parses a Slack message text to identify natural language location questions.
    Returns the clean search query keyword if matched, otherwise None.
    Handles standard 'where is' queries as well as direct mention queries like
    '<@spacegrep> aaa batteries' without any prefix.
    """
    if not text:
        return None

    clean_text = text.strip()
    text_no_mention = strip_bot_mentions(clean_text)
    had_mention = clean_text != text_no_mention

    # Check query patterns on both mention-stripped text and original text
    for target in (text_no_mention, clean_text):
        for pattern in QUERY_PATTERNS:
            match = pattern.match(target)
            if match:
                candidate = match.group(1).strip()
                candidate = re.sub(r"[\?\.\!]+$", "", candidate).strip()
                if candidate:
                    return candidate

    # If the user directly @mentioned the bot with search keywords, e.g. "<@U12345678> aaa batteries"
    if had_mention and text_no_mention:
        clean_direct = re.sub(
            r"^(?:hey\s+|hi\s+|hello\s+|please\s+|can\s+you\s+(?:find\s+)?|could\s+you\s+(?:find\s+)?)",
            "",
            text_no_mention,
            flags=re.IGNORECASE,
        ).strip()
        clean_direct = re.sub(
            r"^(?:where\s+(?:is|are|can\s+i\s+find)|find|locate|search(?:\s+for)?)\s+",
            "",
            clean_direct,
            flags=re.IGNORECASE,
        )
        clean_direct = re.sub(
            r"^(?:the\s+|a\s+|an\s+|some\s+)",
            "",
            clean_direct,
            flags=re.IGNORECASE,
        ).strip()
        clean_direct = re.sub(r"[\?\.\!]+$", "", clean_direct).strip()
        if clean_direct and len(clean_direct.split()) <= 8:
            return clean_direct

    return None


def extract_query_from_parent_message(parent_text: str) -> Optional[str]:
    """
    Extracts a query from a parent thread message when a user invokes the bot in a reply thread.
    Tries standard query parsing first, then short item inquiries.
    """
    if not parent_text:
        return None
    q = parse_location_query(parent_text)
    if q:
        return q
    clean = strip_bot_mentions(parent_text)
    clean = re.sub(
        r"^(?:hey|hi|hello|anyone|anybody)\s+(?:know\s+|seen\s+)?",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(
        r"^(?:do\s+we\s+have|have\s+we\s+got|is\s+there|seen)\s+(?:any\s+|the\s+|a\s+)?",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r"^(?:the\s+|a\s+|an\s+|some\s+)", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"[\?\.\!]+$", "", clean).strip()
    words = clean.split()
    if 1 <= len(words) <= 8:
        return clean
    return None


def build_search_url(query: str, base_url: Optional[str] = None) -> str:
    """Builds a direct link to Spacegrep with the search query pre-loaded."""
    root = (
        base_url
        or os.getenv("SPACEGREP_BASE_URL", "").strip()
        or os.getenv("APP_BASE_URL", "").strip()
        or "http://localhost:8000"
    ).rstrip("/")
    encoded_q = urllib.parse.quote_plus(query.strip())
    return f"{root}/?q={encoded_q}"


async def handle_query_message(
    client: AsyncWebClient,
    channel: str,
    message_ts: str,
    query: str,
    thread_ts: Optional[str] = None,
) -> None:
    """Handles an extracted search query: scans inventory and replies with a pre-loaded link."""
    logger.info("Handling where-is query for '%s' in channel %s", query, channel)
    summary = search_inventory_summary(query)
    target_url = build_search_url(query)

    total = summary["total_matches"]

    if total > 0:
        reply = (
            f"Found {total} matching {'bin' if total == 1 else 'bins'} for \"{query}\".\n"
            f"<{target_url}|Open in Spacegrep>"
        )
    else:
        reply = (
            f"No bins currently indexed directly matching \"{query}\".\n"
            f"<{target_url}|Open in Spacegrep>"
        )

    target_thread_ts = thread_ts or message_ts
    await post_thread_reply(client, channel=channel, thread_ts=target_thread_ts, text=reply)
