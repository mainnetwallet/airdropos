"""Photos attached to a chat message: payload shape and request validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.api.v1.endpoints.system import ChatRequest
from backend.app.llm.client import Message

PNG = "data:image/png;base64,iVBORw0KGgo="


def test_message_without_images_is_plain_text() -> None:
    assert Message("user", "hi").to_payload() == {"role": "user", "content": "hi"}


def test_user_message_with_image_uses_content_parts() -> None:
    payload = Message("user", "what is this?", images=[PNG]).to_payload()
    assert payload["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": PNG}},
    ]


def test_images_are_never_attached_to_other_roles() -> None:
    assert Message("assistant", "x", images=[PNG]).to_payload()["content"] == "x"


def test_request_accepts_valid_images() -> None:
    assert ChatRequest(message="hi", images=[PNG]).images == [PNG]


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.com/a.png",
        "data:text/html;base64,PGgxPg==",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,@@@",
    ],
)
def test_request_rejects_non_image_or_remote_urls(bad: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", images=[bad])


def test_request_rejects_too_many_or_oversized_images() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", images=[PNG] * 5)
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", images=["data:image/png;base64," + "A" * 8_100_000])
