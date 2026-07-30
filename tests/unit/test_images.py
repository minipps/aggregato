"""Image routes preserve local URLs even when image retrieval is disabled (T078)."""

from __future__ import annotations

from types import SimpleNamespace

from aggregato.api.routes.images import _PLACEHOLDER, image


async def test_image_route_returns_placeholder_without_touching_cache() -> None:
    """A disabled cache is an intentional offline mode, not a remote redirect or error."""
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(image_cache_enabled=False),
                engine=object(),
            )
        )
    )

    response = await image(request, "a" * 64)

    assert response.media_type == "image/gif"
    assert response.body == _PLACEHOLDER
    assert response.headers["cache-control"] == "no-store"
