"""Operator-facing validation for ListenBrainz-compatible endpoint URLs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aggregato.providers.listenbrainz import ListenBrainzConfig, listens_url


def test_base_url_is_trimmed_before_building_the_endpoint() -> None:
    config = ListenBrainzConfig(
        username="mini", token="token", base_url=" http://maloja:42010/apis/listenbrainz/ "
    )

    assert config.base_url == "http://maloja:42010/apis/listenbrainz"
    assert listens_url(config) == "http://maloja:42010/apis/listenbrainz/1/user/mini/listens"


def test_base_url_rejects_an_api_version_suffix() -> None:
    with pytest.raises(ValidationError, match="stop before the API version /1"):
        ListenBrainzConfig(
            username="mini", token="token", base_url="http://maloja:42010/apis/listenbrainz/1"
        )
