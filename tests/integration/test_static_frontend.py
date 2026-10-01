from __future__ import annotations

from pathlib import Path

import httpx

from aggregato.config import load_config
from aggregato.main import create_app

TOKEN = "static-test-token"


async def test_spa_serves_assets_but_cannot_escape_its_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    static_dir = tmp_path / "static"
    assets = static_dir / "assets"
    assets.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main>SPA</main>", encoding="utf-8")
    (assets / "app.js").write_text("asset", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    (static_dir / "escape.txt").symlink_to(outside)
    (assets / "escape.js").symlink_to(outside)

    config = load_config({"AGGREGATO_TOKEN": TOKEN, "AGGREGATO_DATA": str(data_dir)}).model_copy(
        update={"static_dir": static_dir}
    )
    app = create_app(config, run_migrations=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        asset = await client.get("/assets/app.js")
        spa_route = await client.get("/works/example")
        traversal = await client.get("/%2e%2e/outside.txt")
        symlink = await client.get("/escape.txt")
        asset_symlink = await client.get("/assets/escape.js")
        missing_api = await client.get("/api/v1/missing")

    assert asset.status_code == 200 and asset.text == "asset"
    assert spa_route.status_code == 200 and "<main>SPA</main>" in spa_route.text
    assert traversal.status_code == 404
    assert symlink.status_code == 404
    assert asset_symlink.status_code == 404
    assert missing_api.status_code == 404
    assert all("outside secret" not in response.text for response in (traversal, symlink))
