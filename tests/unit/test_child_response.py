"""HTTP diagnostics cross the child protocol without response secrets."""

from __future__ import annotations

import httpx
import pytest

from aggregato.sync import child
from aggregato.sync.protocol import ProtocolViolation, ResponseMessage, decode, encode


def test_response_message_discards_credentials_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = (
        "url-user",
        "url-password",
        "query-token",
        "fragment-token",
        "header-token",
        "cookie-token",
        "body-access-token",
        "body-refresh-token",
    )
    request = httpx.Request(
        "POST",
        "https://url-user:url-password@example.test/oauth/token"
        "?access_token=query-token#fragment-token",
    )
    response = httpx.Response(
        200,
        request=request,
        headers={
            "Authorization": "Bearer header-token",
            "Set-Cookie": "session=cookie-token",
        },
        json={"access_token": "body-access-token", "refresh_token": "body-refresh-token"},
    )
    messages: list[ResponseMessage] = []
    monkeypatch.setattr(child, "emit", messages.append)

    child._response(response)

    assert len(messages) == 1
    message = messages[0]
    assert message.model_dump(exclude={"type"}) == {
        "method": "POST",
        "url": "https://example.test/oauth/token",
        "status": 200,
    }
    encoded = encode(message)
    assert all(secret not in encoded for secret in secrets)
    assert isinstance(decode(encoded), ResponseMessage)
    decoded = decode(
        '{"type":"response","method":"GET",'
        '"url":"https://user:password@example.test/api?token=protocol-token#fragment",'
        '"status":200}'
    )
    assert isinstance(decoded, ResponseMessage)
    assert decoded.url == "https://example.test/api"
    with pytest.raises(ProtocolViolation):
        decode(
            '{"type":"response","method":"POST","url":"https://example.test/oauth",'
            '"status":200,"headers":{"Authorization":"header-token"},'
            '"body":"body-access-token"}'
        )
