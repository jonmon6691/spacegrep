import asyncio
import io
import logging
import os
from typing import Any, Dict

import httpx
from PIL import Image
from slack_sdk.web.async_client import AsyncWebClient

from backend.ingest_manager import ingest_manager
from backend.moderation import inspect_photo_policy
from backend.shelf_matcher import match_shelf_action
from backend.slack.client import (
    add_reaction,
    post_thread_reply,
    transition_reaction,
)

logger = logging.getLogger(__name__)

SUPPORTED_MIMETYPES = {"image/jpeg", "image/png", "image/webp"}


async def download_slack_file(url: str, token: str) -> bytes:
    """Downloads a private Slack file attachment using the Bot token."""
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        headers = {"Authorization": f"Bearer {token}"}
        resp = await http_client.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


async def handle_photo_upload(
    client: AsyncWebClient,
    channel: str,
    message_ts: str,
    file_info: Dict[str, Any],
    caption: str = "",
) -> None:
    """
    Handles a photo uploaded to the monitored channel:
    1. Reacts with :eyes: to signal receipt.
    2. Downloads and validates image bytes.
    3. Runs community policy inspection (visible faces, blur, memes).
    4. Determines if photo replaces an existing shelf or is a new shelf.
    5. Submits to IngestManager and monitors progress.
    6. On completion: transitions reaction to :white_check_mark: and replies in thread with bin count & policy alerts.
    7. On fatal error: transitions reaction to :no_entry: and replies with error reason.
    """
    file_name = file_info.get("name") or "uploaded_shelf.jpg"
    mimetype = file_info.get("mimetype", "")
    download_url = file_info.get("url_private_download") or file_info.get("url_private")

    if mimetype not in SUPPORTED_MIMETYPES or not download_url:
        logger.info("Ignoring file %s with unsupported mimetype '%s'", file_name, mimetype)
        return

    # 1. Immediate visual feedback: :eyes:
    logger.info("Photo detected in channel %s (file: %s). Adding :eyes: reaction...", channel, file_name)
    await add_reaction(client, channel=channel, timestamp=message_ts, name="eyes")

    # 2. Download and validate file
    try:
        bot_token = client.token or os.getenv("SLACK_BOT_TOKEN", "")
        file_bytes = await download_slack_file(download_url, bot_token)
    except Exception as e:
        logger.exception("Failed to download file from Slack: %s", e)
        await transition_reaction(client, channel=channel, timestamp=message_ts, old_name="eyes", new_name="no_entry")
        await post_thread_reply(
            client,
            channel=channel,
            thread_ts=message_ts,
            text=f"⛔ **Ingestion Failed:** Could not download attachment from Slack: {e}",
        )
        return

    # Validate image format via Pillow
    try:
        with Image.open(io.BytesIO(file_bytes)) as test_img:
            test_img.verify()
    except Exception as e:
        logger.warning("Corrupt image received from Slack: %s", e)
        await transition_reaction(client, channel=channel, timestamp=message_ts, old_name="eyes", new_name="no_entry")
        await post_thread_reply(
            client,
            channel=channel,
            thread_ts=message_ts,
            text="⛔ **Ingestion Failed:** Invalid or corrupt image file. Please upload a clear JPG, PNG, or WebP photo.",
        )
        return

    # 3. Community policy inspection (runs asynchronously, never blocks)
    try:
        policy_result = await asyncio.to_thread(inspect_photo_policy, file_bytes)
    except Exception as e:
        logger.warning("Policy inspection encountered an error (continuing anyway): %s", e)
        policy_result = None

    # 4. Shelf matching: replacement vs new
    try:
        match_result = await asyncio.to_thread(
            match_shelf_action,
            new_image_bytes=file_bytes,
            caption=caption,
        )
        is_replace = match_result.is_replacement
        target_photo_id = match_result.matched_photo_id
        target_name = match_result.matched_photo_name or file_name
    except Exception as e:
        logger.warning("Shelf matching error: %s (defaulting to new shelf)", e)
        is_replace = False
        target_photo_id = None
        target_name = file_name

    # 5. Enqueue job into IngestManager
    try:
        job = ingest_manager.create_job(
            original_name=target_name,
            file_bytes=file_bytes,
            photo_id=target_photo_id,
            is_replace=is_replace,
        )
    except Exception as e:
        logger.exception("Failed to create ingest job: %s", e)
        await transition_reaction(client, channel=channel, timestamp=message_ts, old_name="eyes", new_name="no_entry")
        await post_thread_reply(
            client,
            channel=channel,
            thread_ts=message_ts,
            text=f"⛔ **Ingestion Failed:** Could not enqueue job: {e}",
        )
        return

    # 6. Await job completion via subscriber queue
    q = ingest_manager.subscribe_job(job.job_id)
    final_status = "failed"
    bins_count = 0
    error_msg = None

    try:
        while True:
            # Poll with timeout to keep task resilient
            data = await q.get()
            status = data.get("status")
            if status == "completed":
                final_status = "completed"
                bins_count = data.get("bins_count", 0)
                break
            elif status == "failed":
                final_status = "failed"
                error_msg = data.get("error", "Vision ingestion failed")
                break
    except Exception as e:
        logger.exception("Error while awaiting ingest job %s: %s", job.job_id, e)
        final_status = "failed"
        error_msg = str(e)
    finally:
        ingest_manager.unsubscribe_job(job.job_id, q)

    base_url = (
        os.getenv("SPACEGREP_BASE_URL", "").strip()
        or os.getenv("APP_BASE_URL", "").strip()
        or "http://localhost:8000"
    ).rstrip("/")

    # 7. Post outcome to Slack
    if final_status == "completed":
        await transition_reaction(
            client,
            channel=channel,
            timestamp=message_ts,
            old_name="eyes",
            new_name="white_check_mark",
        )

        shelf_type = (
            f"*{target_name}* (Replacement of existing shelf)"
            if is_replace
            else f"New Shelf: *{target_name}*"
        )
        reply_lines = [
            "✅ **Accepted and Ingested!**",
            f"Found and indexed **{bins_count} {'bin' if bins_count == 1 else 'bins'}** on {shelf_type}.",
            f"👉 <{base_url}|Open in Spacegrep>",
        ]

        if policy_result and policy_result.policy_warnings:
            reply_lines.append("")
            reply_lines.append("⚠️ **Community Policy Notice:**")
            for w in policy_result.policy_warnings:
                reply_lines.append(f"• {w}")

        await post_thread_reply(
            client,
            channel=channel,
            thread_ts=message_ts,
            text="\n".join(reply_lines),
        )
    else:
        await transition_reaction(
            client,
            channel=channel,
            timestamp=message_ts,
            old_name="eyes",
            new_name="no_entry",
        )
        await post_thread_reply(
            client,
            channel=channel,
            thread_ts=message_ts,
            text=f"⛔ **Ingestion Failed:** {error_msg or 'An unexpected error occurred during processing.'}",
        )
