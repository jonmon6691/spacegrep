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
        r"^(?:hey\s+|hi\s+|hello\s+)?(?:<@[A-Z0-9]+>\s+)?(?:find|locate|search(?:\s+for)?)\s+(?:the\s+|a\s+|some\s+)?(.+?)\s*[\?\.]*$",
        re.IGNORECASE,
    ),
]


def parse_location_query(text: str) -> Optional[str]:
    """
    Parses a Slack message text to identify natural language location questions.
    Returns the clean search query keyword if matched, otherwise None.
    """
    if not text:
        return None

    clean_text = text.strip()

    # Strip leading @bot mention if present: e.g. "<@U12345678> ..."
    text_no_mention = re.sub(r"^<@[A-Z0-9]+>\s*", "", clean_text, flags=re.IGNORECASE).strip()

    # Check query patterns on both mention-stripped text and original text
    for target in (text_no_mention, clean_text):
        for pattern in QUERY_PATTERNS:
            match = pattern.match(target)
            if match:
                candidate = match.group(1).strip()
                candidate = re.sub(r"[\?\.\!]+$", "", candidate).strip()
                if candidate:
                    return candidate

    # If the user directly @mentioned the bot with search keywords, e.g. "<@U12345678> cr2032"
    if clean_text != text_no_mention and text_no_mention:
        clean_direct = re.sub(
            r"^(?:where\s+(?:is|are|can\s+i\s+find)|find|locate|search(?:\s+for)?)\s+",
            "",
            text_no_mention,
            flags=re.IGNORECASE,
        )
        clean_direct = re.sub(
            r"^(?:the\s+|a\s+|an\s+|some\s+)",
            "",
            clean_direct,
            flags=re.IGNORECASE,
        ).strip()
        clean_direct = re.sub(r"[\?\.\!]+$", "", clean_direct).strip()
        if clean_direct and len(clean_direct.split()) <= 6:
            return clean_direct

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
) -> None:
    """Handles an extracted search query: scans inventory and replies with a pre-loaded link."""
    logger.info("Handling where-is query for '%s' in channel %s", query, channel)
    summary = search_inventory_summary(query)
    target_url = build_search_url(query)

    total = summary["total_matches"]
    shelves = summary["shelves"]
    sample_labels = summary["sample_labels"]

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

    await post_thread_reply(client, channel=channel, thread_ts=message_ts, text=reply)
