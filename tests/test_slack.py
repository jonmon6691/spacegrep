import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.database import init_db, insert_bin, insert_photo, search_inventory_summary
from backend.slack.bot import start_slack_bot, stop_slack_bot
from backend.slack.client import add_reaction, remove_reaction, transition_reaction
from backend.slack.handlers_query import build_search_url, parse_location_query


def test_parse_location_query():
    # Standard location queries
    assert parse_location_query("Where is the kapton tape?") == "kapton tape"
    assert parse_location_query("Where are 10k resistors?") == "10k resistors"
    assert parse_location_query("where can i find ESP32?") == "ESP32"
    assert parse_location_query("Find multimeter") == "multimeter"
    assert parse_location_query("locate wire strippers.") == "wire strippers"
    assert parse_location_query("search for solder paste") == "solder paste"

    # Mentions
    assert parse_location_query("<@U12345678> where is soldering iron?") == "soldering iron"
    assert parse_location_query("<@U12345678> find cr2032") == "cr2032"

    # Conversational greetings
    assert parse_location_query("Hey where is the heat shrink?") == "heat shrink"
    assert parse_location_query("Hello where are multimeters?") == "multimeters"

    # Non-queries should return None
    assert parse_location_query("Hey everyone, I organized shelf 2!") is None
    assert parse_location_query("Thanks for finding the tools") is None
    assert parse_location_query("") is None
    assert parse_location_query("   ") is None


def test_build_search_url():
    url = build_search_url("kapton tape", base_url="https://spacegrep.hackerspace.org")
    assert url == "https://spacegrep.hackerspace.org/?q=kapton+tape"

    url_with_special = build_search_url("resistors & capacitors", base_url="http://localhost:8000")
    assert url_with_special == "http://localhost:8000/?q=resistors+%26+capacitors"


def test_search_inventory_summary():
    init_db()
    photo = insert_photo(
        photo_id="test-p-slack",
        filename="test_p.jpg",
        original_name="Component Rack",
        width=1000,
        height=1000,
    )
    insert_bin(
        bin_id="bin-slack-1",
        photo_id="test-p-slack",
        label="Kapton Tape 1/2 inch",
        semantic_tags=["heat resistant tape", "polyimide"],
        bbox=[0.1, 0.1, 0.2, 0.2],
    )
    insert_bin(
        bin_id="bin-slack-2",
        photo_id="test-p-slack",
        label="Multimeter Probes",
        semantic_tags=["test leads", "measurement"],
        bbox=[0.3, 0.3, 0.2, 0.2],
    )

    # Search by label
    res1 = search_inventory_summary("kapton")
    assert res1["total_matches"] == 1
    assert "Component Rack" in res1["shelves"]
    assert "Kapton Tape 1/2 inch" in res1["sample_labels"]

    # Search by semantic tag
    res2 = search_inventory_summary("heat resistant")
    assert res2["total_matches"] == 1
    assert "Kapton Tape 1/2 inch" in res2["sample_labels"]

    # Search with no match
    res3 = search_inventory_summary("oscilloscope")
    assert res3["total_matches"] == 0
    assert len(res3["shelves"]) == 0


def test_slack_reaction_lifecycle():
    async def run_test():
        client = AsyncMock()
        client.reactions_add = AsyncMock()
        client.reactions_remove = AsyncMock()

        # Add reaction
        added = await add_reaction(client, channel="C123", timestamp="1234.56", name="eyes")
        assert added is True
        client.reactions_add.assert_called_with(channel="C123", timestamp="1234.56", name="eyes")

        # Transition reaction (:eyes: -> :white_check_mark:)
        await transition_reaction(
            client,
            channel="C123",
            timestamp="1234.56",
            old_name="eyes",
            new_name="white_check_mark",
        )
        client.reactions_remove.assert_called_with(channel="C123", timestamp="1234.56", name="eyes")
        client.reactions_add.assert_called_with(channel="C123", timestamp="1234.56", name="white_check_mark")

    import asyncio
    asyncio.run(run_test())


def test_start_slack_bot_unconfigured():
    async def run_test():
        # When tokens are not configured in environment, start_slack_bot gracefully returns False
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "", "SLACK_APP_TOKEN": ""}):
            started = await start_slack_bot()
            assert started is False
            await stop_slack_bot()

    import asyncio
    asyncio.run(run_test())


def test_handle_query_message_flow():
    from backend.slack.handlers_query import handle_query_message

    async def run_test():
        client = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "100.201"})

        await handle_query_message(
            client=client,
            channel="C12345",
            message_ts="100.200",
            query="kapton tape",
        )
        assert client.chat_postMessage.called
        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C12345"
        assert call_kwargs["thread_ts"] == "100.200"
        assert "/?q=kapton+tape" in call_kwargs["text"]

    import asyncio
    asyncio.run(run_test())


def test_handle_photo_upload_invalid_image():
    from backend.slack.handlers_photo import handle_photo_upload

    async def run_test():
        client = AsyncMock()
        client.reactions_add = AsyncMock(return_value={"ok": True})
        client.reactions_remove = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "100.301"})
        client.token = "xoxb-test"

        file_info = {
            "name": "bad.jpg",
            "mimetype": "image/jpeg",
            "url_private_download": "https://slack.com/files/bad.jpg",
        }

        with patch("backend.slack.handlers_photo.download_slack_file", AsyncMock(return_value=b"not an image")):
            await handle_photo_upload(
                client=client,
                channel="C12345",
                message_ts="100.300",
                file_info=file_info,
            )

        # :eyes: added first
        client.reactions_add.assert_any_call(channel="C12345", timestamp="100.300", name="eyes")
        # :no_entry: added after failure
        client.reactions_add.assert_any_call(channel="C12345", timestamp="100.300", name="no_entry")
        # Thread reply explaining invalid image
        assert client.chat_postMessage.called
        call_kwargs = client.chat_postMessage.call_args[1]
        assert "Invalid or corrupt image file" in call_kwargs["text"]

    import asyncio
    asyncio.run(run_test())


def test_parse_location_query_at_mention_prefixless():
    # Direct mention with keywords without 'where is' prefix
    assert parse_location_query("<@U12345678> aaa batteries") == "aaa batteries"
    assert parse_location_query("<@U12345678> aaa batteries?") == "aaa batteries"
    assert parse_location_query("hey <@U12345678> aaa batteries") == "aaa batteries"
    assert parse_location_query("<@U12345678> 3v coin cell") == "3v coin cell"
    assert parse_location_query("<@U12345678> can you find 10k resistors") == "10k resistors"
    # Bare mention returns None (signals to look at parent message in threads)
    assert parse_location_query("<@U12345678>") is None
    assert parse_location_query("<@U12345678>   ") is None


def test_extract_query_from_parent_message():
    from backend.slack.handlers_query import extract_query_from_parent_message
    assert extract_query_from_parent_message("Where are the aaa batteries?") == "aaa batteries"
    assert extract_query_from_parent_message("Anyone know where aaa batteries are?") == "aaa batteries"
    assert extract_query_from_parent_message("where is the kapton tape?") == "kapton tape"
    assert extract_query_from_parent_message("Do we have any solder paste?") == "solder paste"
    assert extract_query_from_parent_message("aaa batteries") == "aaa batteries"
    assert extract_query_from_parent_message("kapton tape") == "kapton tape"
    # Long conversational messages should not match
    long_msg = "Hey team, remember that we have a workshop meeting tomorrow at 6pm to discuss our new layout."
    assert extract_query_from_parent_message(long_msg) is None


def test_process_bot_event_ignores_unmentioned_message():
    from backend.slack.bot import process_bot_event

    async def run_test():
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "UBOT123"})
        client.chat_postMessage = AsyncMock()

        # Regular message where bot is NOT mentioned: must be ignored!
        event = {
            "channel": "C123",
            "ts": "100.01",
            "text": "where is the kapton tape?",
        }
        with patch("backend.slack.bot.handle_query_message", AsyncMock()) as mock_hq:
            await process_bot_event(client, event, from_app_mention=False)
            mock_hq.assert_not_called()
            client.chat_postMessage.assert_not_called()

        # Image upload where bot is NOT mentioned: must be ignored!
        photo_event = {
            "channel": "C123",
            "ts": "100.02",
            "text": "here is a cool electronics project I made",
            "files": [{"mimetype": "image/jpeg", "name": "my_project.jpg"}],
        }
        with patch("backend.slack.bot.handle_photo_upload", AsyncMock()) as mock_hp:
            await process_bot_event(client, photo_event, from_app_mention=False)
            mock_hp.assert_not_called()

    import asyncio
    asyncio.run(run_test())


def test_process_bot_event_handles_at_mention_query():
    from backend.slack.bot import process_bot_event

    async def run_test():
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "UBOT123"})

        # Message where bot IS mentioned with prefix-less query
        event = {
            "channel": "C123",
            "ts": "200.01",
            "text": "<@UBOT123> aaa batteries",
        }
        with patch("backend.slack.bot.handle_query_message", AsyncMock()) as mock_hq:
            await process_bot_event(client, event, from_app_mention=False)
            mock_hq.assert_called_once()
            call_kwargs = mock_hq.call_args[1]
            assert call_kwargs["query"] == "aaa batteries"
            assert call_kwargs["channel"] == "C123"
            assert call_kwargs["thread_ts"] == "200.01"

    import asyncio
    asyncio.run(run_test())


def test_process_bot_event_veteran_thread_reply():
    from backend.slack.bot import process_bot_event

    async def run_test():
        client = AsyncMock()
        client.auth_test = AsyncMock(return_value={"user_id": "UBOT123"})
        # Mock parent message retrieval
        client.conversations_replies = AsyncMock(return_value={
            "ok": True,
            "messages": [
                {
                    "ts": "300.00",
                    "text": "Where are the aaa batteries?",
                }
            ],
        })

        # Veteran replies in thread with just "@spacegrep"
        thread_reply_event = {
            "channel": "C123",
            "ts": "300.05",
            "thread_ts": "300.00",
            "text": "<@UBOT123>",
        }
        with patch("backend.slack.bot.handle_query_message", AsyncMock()) as mock_hq:
            await process_bot_event(client, thread_reply_event, from_app_mention=False)
            mock_hq.assert_called_once()
            call_kwargs = mock_hq.call_args[1]
            assert call_kwargs["query"] == "aaa batteries"
            assert call_kwargs["channel"] == "C123"
            assert call_kwargs["thread_ts"] == "300.00"

    import asyncio
    asyncio.run(run_test())


def test_handle_photo_upload_broadcasts_policy_guidelines():
    from backend.models import PolicyInspectionResult
    from backend.slack.handlers_photo import handle_photo_upload

    async def run_test():
        client = AsyncMock()
        client.reactions_add = AsyncMock(return_value={"ok": True})
        client.reactions_remove = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "400.02"})
        client.token = "xoxb-test"

        file_info = {
            "name": "workshop_shelf.jpg",
            "mimetype": "image/jpeg",
            "url_private_download": "https://slack.com/files/shelf.jpg",
        }

        # Create valid image bytes
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), color=(10, 20, 30)).save(buf, format="JPEG")
        valid_img_bytes = buf.getvalue()

        # Mock policy inspection returning warnings
        policy_res = PolicyInspectionResult(
            is_appropriate=True,
            policy_warnings=["Image resolution is very low. Please provide higher resolution."],
        )

        # Mock ingest manager job completion
        mock_job = MagicMock()
        mock_job.job_id = "job-123"
        q = asyncio.Queue()
        await q.put({"status": "completed", "bins_count": 5})

        with patch("backend.slack.handlers_photo.download_slack_file", AsyncMock(return_value=valid_img_bytes)), \
             patch("backend.slack.handlers_photo.inspect_photo_policy", return_value=policy_res), \
             patch("backend.slack.handlers_photo.ingest_manager.create_job", return_value=mock_job), \
             patch("backend.slack.handlers_photo.ingest_manager.subscribe_job", return_value=q), \
             patch("backend.slack.handlers_photo.ingest_manager.unsubscribe_job"):

            await handle_photo_upload(
                client=client,
                channel="C123",
                message_ts="400.01",
                file_info=file_info,
                thread_ts="400.01",
            )

        # 2 messages posted:
        # 1. Ingest completion (reply_broadcast=False)
        # 2. Community guideline notice (reply_broadcast=True)
        assert client.chat_postMessage.call_count == 2

        calls = client.chat_postMessage.call_args_list
        completion_call = calls[0][1]
        guideline_call = calls[1][1]

        assert "Found 5 bins" in completion_call["text"]
        assert completion_call["reply_broadcast"] is False

        assert "Community Policy Notice" in guideline_call["text"]
        assert "Image resolution is very low" in guideline_call["text"]
        assert guideline_call["reply_broadcast"] is True

    import asyncio
    asyncio.run(run_test())
