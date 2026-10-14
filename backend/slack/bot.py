import asyncio
import logging
import os
from typing import Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler

from backend.slack.handlers_photo import handle_photo_upload
from backend.slack.handlers_query import handle_query_message, parse_location_query

logger = logging.getLogger(__name__)

_slack_app: Optional[AsyncApp] = None
_socket_handler: Optional[AsyncSocketModeHandler] = None
_runner_task: Optional[asyncio.Task] = None


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
        channel = event.get("channel")
        monitored_channel = os.getenv("SLACK_CHANNEL_ID", "").strip()

        # Channel isolation: ignore messages outside the designated channel
        if monitored_channel and channel != monitored_channel:
            return

        # Avoid processing bot's own messages or sub-events
        if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_deleted"):
            return

        message_ts = event.get("ts")
        text = event.get("text", "")
        files = event.get("files", [])

        # 1. Check for photo uploads
        image_files = [f for f in files if f.get("mimetype", "").startswith("image/")]
        if image_files:
            for f in image_files:
                asyncio.create_task(
                    handle_photo_upload(
                        client=client,
                        channel=channel,
                        message_ts=message_ts,
                        file_info=f,
                        caption=text,
                    )
                )
            return

        # 2. Check for where-is queries
        query = parse_location_query(text)
        if query:
            asyncio.create_task(
                handle_query_message(
                    client=client,
                    channel=channel,
                    message_ts=message_ts,
                    query=query,
                )
            )

    @app.event("app_mention")
    async def on_app_mention(body, client):
        event = body.get("event", {})
        channel = event.get("channel")
        monitored_channel = os.getenv("SLACK_CHANNEL_ID", "").strip()

        if monitored_channel and channel != monitored_channel:
            return

        message_ts = event.get("ts")
        text = event.get("text", "")
        query = parse_location_query(text)
        if query:
            asyncio.create_task(
                handle_query_message(
                    client=client,
                    channel=channel,
                    message_ts=message_ts,
                    query=query,
                )
            )

    return app


async def start_slack_bot() -> bool:
    """Initializes and connects the Slack SocketModeHandler in the background."""
    global _slack_app, _socket_handler, _runner_task

    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    monitored_channel = os.getenv("SLACK_CHANNEL_ID", "").strip()

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
        logger.info(
            "Slack Socket Mode started. Monitoring channel ID: '%s'",
            monitored_channel or "ALL (unrestricted)",
        )
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
