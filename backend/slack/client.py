import logging
from typing import Optional
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


async def add_reaction(
    client: AsyncWebClient,
    channel: str,
    timestamp: str,
    name: str,
) -> bool:
    """Adds an emoji reaction to a message, catching already_reacted gracefully."""
    try:
        await client.reactions_add(
            channel=channel,
            timestamp=timestamp,
            name=name,
        )
        return True
    except SlackApiError as e:
        if e.response.get("error") == "already_reacted":
            return True
        logger.warning("Slack reactions_add error ('%s'): %s", name, e)
        return False
    except Exception as e:
        logger.warning("Failed to add Slack reaction '%s': %s", name, e)
        return False


async def remove_reaction(
    client: AsyncWebClient,
    channel: str,
    timestamp: str,
    name: str,
) -> bool:
    """Removes an emoji reaction from a message, catching no_reaction gracefully."""
    try:
        await client.reactions_remove(
            channel=channel,
            timestamp=timestamp,
            name=name,
        )
        return True
    except SlackApiError as e:
        if e.response.get("error") in ("no_reaction", "message_not_found"):
            return True
        logger.warning("Slack reactions_remove error ('%s'): %s", name, e)
        return False
    except Exception as e:
        logger.warning("Failed to remove Slack reaction '%s': %s", name, e)
        return False


async def transition_reaction(
    client: AsyncWebClient,
    channel: str,
    timestamp: str,
    old_name: str,
    new_name: str,
) -> None:
    """Transitions from an old reaction to a new reaction (e.g. :eyes: -> :white_check_mark:)."""
    await remove_reaction(client, channel, timestamp, old_name)
    await add_reaction(client, channel, timestamp, new_name)


async def post_thread_reply(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    text: str,
    reply_broadcast: bool = False,
) -> Optional[str]:
    """Posts a thread reply message to the specified thread_ts, optionally broadcasting to the channel."""
    try:
        res = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            reply_broadcast=reply_broadcast,
            unfurl_links=False,
            unfurl_media=False,
        )
        return res.get("ts")
    except Exception as e:
        logger.error("Failed to post Slack thread reply: %s", e)
        return None


async def fetch_parent_message(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
) -> Optional[dict]:
    """Fetches the root message of a thread from Slack using conversations.replies."""
    try:
        resp = await client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=1,
            inclusive=True,
        )
        messages = resp.get("messages", [])
        if messages:
            return messages[0]
    except Exception as e:
        logger.warning(
            "Failed to fetch parent message for thread %s in channel %s: %s",
            thread_ts,
            channel,
            e,
        )
    return None

