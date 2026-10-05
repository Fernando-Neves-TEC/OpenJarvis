from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from openjarvis.connectors.gmail import GmailConnector


def _connector(tmp_path: Path) -> GmailConnector:
    credentials = tmp_path / "gmail.json"
    credentials.write_text(json.dumps({"access_token": "fake-token"}))
    return GmailConnector(credentials_path=str(credentials))


def _metadata(msg_id: str):
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "labelIds": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
        "snippet": f"snippet-{msg_id}",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "Subject", "value": f"Subject {msg_id}"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
            ]
        },
    }


def test_list_message_stubs_paginates_ids_without_message_fetches(tmp_path: Path):
    connector = _connector(tmp_path)
    pages = [
        {
            "messages": [
                {"id": "m1", "threadId": "t1"},
                {"id": "m2", "threadId": "t2"},
            ],
            "nextPageToken": "next-1",
        },
        {
            "messages": [{"id": "m3", "threadId": "t3"}],
        },
    ]

    with (
        patch(
            "openjarvis.connectors.gmail._gmail_api_list_messages",
            side_effect=pages,
        ) as list_mock,
        patch(
            "openjarvis.connectors.gmail._gmail_api_get_message_metadata",
        ) as metadata_mock,
        patch("openjarvis.connectors.gmail._gmail_api_get_message") as full_mock,
    ):
        items = connector.list_message_stubs(query="in:inbox is:unread")

    assert [item["message_id"] for item in items] == ["m1", "m2", "m3"]
    assert list_mock.call_count == 2
    assert list_mock.call_args_list[0].kwargs["page_token"] is None
    assert list_mock.call_args_list[1].kwargs["page_token"] == "next-1"
    assert list_mock.call_args_list[0].kwargs["max_results"] == 500
    metadata_mock.assert_not_called()
    full_mock.assert_not_called()


def test_list_message_metadata_never_downloads_full_body(tmp_path: Path):
    connector = _connector(tmp_path)

    with (
        patch(
            "openjarvis.connectors.gmail._gmail_api_list_messages",
            return_value={"messages": [{"id": "m1"}, {"id": "m2"}]},
        ) as list_mock,
        patch(
            "openjarvis.connectors.gmail._gmail_api_get_message_metadata",
            side_effect=lambda token, msg_id: _metadata(msg_id),
        ) as metadata_mock,
        patch("openjarvis.connectors.gmail._gmail_api_get_message") as full_mock,
    ):
        items = connector.list_message_metadata(
            query="is:unread",
            max_results=2,
        )

    assert [item["message_id"] for item in items] == ["m1", "m2"]
    assert items[0]["subject"] == "Subject m1"
    assert items[0]["snippet"] == "snippet-m1"
    assert metadata_mock.call_count == 2
    full_mock.assert_not_called()

    _, kwargs = list_mock.call_args
    assert kwargs["query"] == "is:unread"
    assert kwargs["max_results"] == 2


def test_get_message_content_downloads_only_requested_body(tmp_path: Path):
    connector = _connector(tmp_path)
    full = _metadata("m1")
    full["payload"]["mimeType"] = "text/plain"
    full["payload"]["body"] = {"data": "Ym9keS0x"}  # body-1

    with patch(
        "openjarvis.connectors.gmail._gmail_api_get_message",
        return_value=full,
    ) as full_mock:
        item = connector.get_message_content("m1")

    assert item["message_id"] == "m1"
    assert item["body"] == "body-1"
    full_mock.assert_called_once()


def test_get_message_labels_uses_minimal_endpoint(tmp_path: Path):
    connector = _connector(tmp_path)

    with patch(
        "openjarvis.connectors.gmail._gmail_api_get_message_minimal",
        return_value={"id": "m1", "labelIds": ["TRASH"]},
    ) as minimal_mock:
        labels = connector.get_message_labels("m1")

    assert labels == ["TRASH"]
    minimal_mock.assert_called_once()
