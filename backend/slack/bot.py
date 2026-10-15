import asyncio
import collections
import logging
import os
import re
from typing import Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from backend.slack.client import fetch_parent_message, post_thread_reply
from backend.slack.handlers_photo import handle_photo_upload
from backend.slack.handlers_query import (
    extract_query_from_parent_message,
    handle_query_message,
    parse_location_query,
    strip_bot_mentions,
)

logger = logging.getLogger(__name__)

_slack_app: Optional[AsyncApp] = None
_socket_handler: Optional[AsyncSocketModeHandler] = None
_runner_task: Optional[asyncio.Task] = None

_bot_user_id: Optional[str] = None
_processed_events: collections.deque = collections.deque(maxlen=1000)
_processed_lock = asyncio.Lock()


async def get_bot_user_id(client: AsyncWebClient) -> Optional[str]:
    """Retrieves and caches the bot's user ID via auth.test."""
    global _bot_user_id
    if _bot_user_id:
        return _bot_user_id
    try:
        auth_res = await client.auth_test()
        _bot_user_id = auth_res.get("user_id")
        return _bot_user_id
    except Exception as e:
        logger.warning("Could not fetch bot user_id via auth.test: %s", e)
        return None


def is_bot_at_mentioned(text: str, bot_user_id: Optional[str]) -> bool:
    """Checks whether text contains an @-mention targeting the bot."""
    if not text:
        return False
    if bot_user_id:
        return f"<@{bot_user_id}>" in text
    return bool(re.search(r"<@[A-Z0-9]+>", text))


async def should_process_event(channel: str, message_ts: str) -> bool:
    """Deduplicates events across message and app_mention handlers."""
    if not channel or not message_ts:
        return False
    key = f"{channel}:{message_ts}"
    async with _processed_lock:
        if key in _processed_events:
            return False
        _processed_events.append(key)
        return True


async def process_bot_event(
    client: AsyncWebClient,
    event: dict,
    from_app_mention: bool = False,
) -> None:
    """
    Central dispatcher for Slack events where the bot is at'ed.
    Only acts when the bot is explicitly @-mentioned, allowing the bot to live
    in any channel without interfering in unrelated conversation.
    """
    channel = event.get("channel")
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")
    text = event.get("text", "")
    files = event.get("files", [])

    if not channel or not message_ts:
        return

    # Check deduplication between message and app_mention events
    if not await should_process_event(channel, message_ts):
        return

    bot_user_id = await get_bot_user_id(client)

    # For general message events, ensure the bot was @-mentioned
    if not from_app_mention and not is_bot_at_mentioned(text, bot_user_id):
        return

    target_thread_ts = thread_ts or message_ts
    is_thread_reply = bool(thread_ts and thread_ts != message_ts)

    # 1. Check for photo uploads in current message
    image_files = [f for f in files if f.get("mimetype", "").startswith("image/")]
    if image_files:
        clean_caption = strip_bot_mentions(text)
        for f in image_files:
            asyncio.create_task(
                handle_photo_upload(
                    client=client,
                    channel=channel,
                    message_ts=message_ts,
                    thread_ts=target_thread_ts,
                    file_info=f,
                    caption=clean_caption,
                )
            )
        return

    # 2. Check for queries directly in the message text (e.g. '@spacegrep aaa batteries')
    query = parse_location_query(text)
    if query:
        asyncio.create_task(
            handle_query_message(
                client=client,
                channel=channel,
                message_ts=message_ts,
                thread_ts=target_thread_ts,
                query=query,
            )
        )
        return

    # 3. If in a reply thread and the reply had no query (e.g. veteran replies '@spacegrep'):
    # Look at the root/parent message of the thread
    if is_thread_reply and thread_ts:
        parent = await fetch_parent_message(client, channel=channel, thread_ts=thread_ts)
        if parent:
            # Check if parent message has photos to index
            parent_images = [
                f for f in parent.get("files", []) if f.get("mimetype", "").startswith("image/")
            ]
            if parent_images:
                parent_caption = strip_bot_mentions(parent.get("text", ""))
                for f in parent_images:
                    asyncio.create_task(
                        handle_photo_upload(
                            client=client,
                            channel=channel,
                            message_ts=parent.get("ts", message_ts),
                            thread_ts=thread_ts,
                            file_info=f,
                            caption=parent_caption,
                        )
                    )
                return

            # Check if parent message has a query
            parent_query = extract_query_from_parent_message(parent.get("text", ""))
            if parent_query:
                asyncio.create_task(
                    handle_query_message(
                        client=client,
                        channel=channel,
                        message_ts=message_ts,
                        thread_ts=thread_ts,
                        query=parent_query,
                    )
                )
                return

    # 4. If mentioned without a specific query or asking for help, reply with quick usage guide
    clean_text = strip_bot_mentions(text).strip()
    if not clean_text or clean_text.lower() in ("help", "?", "hello", "hi"):
        base_url = (
            os.getenv("SPACEGREP_BASE_URL", "").strip()
            or os.getenv("APP_BASE_URL", "").strip()
            or "http://localhost:8000"
        ).rstrip("/")
        help_msg = (
            "👋 Hi! I'm *Spacegrep*, your workshop inventory locator.\n"
            "• Ask where something is: `@spacegrep where is kapton tape` or `@spacegrep aaa batteries`\n"
            "• Reply `@spacegrep` in any thread where someone asked for an item to find it\n"
            "• Upload a shelf photo with `@spacegrep` to index storage bins\n"
            f"• View the visual workshop map: <{base_url}|Open Spacegrep>"
        )
        await post_thread_reply(client, channel=channel, thread_ts=target_thread_ts, text=help_msg)


def create_slack_app() -> Optional[AsyncApp]:
    """Creates and configures the AsyncApp instance with event routing."""
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.info("SLACK_BOT_TOKEN not configured. Slack integration will remain inactive.")
        return None

    app = AsyncApp(token=bot_token)

    @app.event("message")
    async def on_message(body, client):
        event = body.get("event", {})
        if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_deleted"):
            return
        await process_bot_event(client=client, event=event, from_app_mention=False)

    @app.event("app_mention")
    async def on_app_mention(body, client):
        event = body.get("event", {})
        if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_deleted"):
            return
        await process_bot_event(client=client, event=event, from_app_mention=True)

    return app


async def start_slack_bot() -> bool:
    """Initializes and connects the Slack SocketModeHandler in the background."""
    global _slack_app, _socket_handler, _runner_task

    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()

    if not bot_token or not app_token:
        logger.info(
            "Slack tokens not provided (SLACK_BOT_TOKEN / SLACK_APP_TOKEN). Running web UI only."
        )
        return False

    try:
        _slack_app = create_slack_app()
        if not _slack_app:
            return False

        _socket_handler = AsyncSocketModeHandler(_slack_app, app_token)
        _runner_task = asyncio.create_task(_socket_handler.start_async())
        logger.info("Slack Socket Mode started.")
        return True
    except Exception as e:
        logger.exception("Failed to start Slack Socket Mode handler: %s", e)
        return False


async def stop_slack_bot() -> None:
    """Gracefully terminates the Slack Socket Mode connection."""
    global _socket_handler, _runner_task
    if _socket_handler:
        try:
            logger.info("Closing Slack Socket Mode handler...")
            await _socket_handler.close_async()
        except Exception as e:
            logger.warning("Error closing Slack Socket Mode handler: %s", e)
        _socket_handler = None

    if _runner_task:
        _runner_task.cancel()
        try:
            await _runner_task
        except asyncio.CancelledError:
            pass
        _runner_task = None
    logger.info("Slack bot stopped.")
