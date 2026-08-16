"""Asset cache-busting tests.

Run with: python tests/test_web_assets.py

Guards a failure mode that is indistinguishable from "the bug was never fixed":
the add-on is updated, the server has new JavaScript, but the browser keeps
running the old copy. Version-stamped URLs plus revalidation headers make that
impossible.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "health_sentinel", "rootfs", "opt", "sentinel")
sys.path.insert(0, SRC)

os.environ["SENTINEL_VERSION"] = "9.9.9"

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import web  # noqa: E402


class FakeSentinel:
    """Only what the routes touched here actually use."""

    live = {}
    capabilities = {}
    boot_verdict = {}

    class _Storage:
        def open_incidents(self):
            return []

        async def aquery(self, *_args, **_kwargs):
            return []

    class _Detector:
        def status(self):
            return {}

    class _Notifier:
        def describe(self):
            return {}

    class _Stream:
        connected = True

    storage = _Storage()
    detector = _Detector()
    notifier = _Notifier()
    journal = _Stream()
    recorder = _Stream()
    started_ts = 0

    def uptime(self):
        return "1m"


results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


async def main() -> int:
    app = web.create_app(FakeSentinel())
    async with TestClient(TestServer(app)) as client:
        index = await client.get("/")
        html = await index.text()

        check("index served", index.status == 200)
        check("chart.js is version-stamped", 'static/chart.js?v=9.9.9' in html,
              "cache-bust")
        check("app.js is version-stamped", 'static/app.js?v=9.9.9' in html)
        check("style.css is version-stamped", 'static/style.css?v=9.9.9' in html)
        check("no unstamped asset refs remain",
              'src="static/app.js"' not in html and
              'href="static/style.css"' not in html)
        check("index must revalidate",
              "no-cache" in (index.headers.get("Cache-Control") or ""),
              index.headers.get("Cache-Control"))

        asset = await client.get("/static/chart.js?v=9.9.9")
        check("versioned asset serves", asset.status == 200)
        check("asset must revalidate",
              "no-cache" in (asset.headers.get("Cache-Control") or ""),
              asset.headers.get("Cache-Control"))

        body = await asset.text()
        check("served chart.js contains the sizing fix",
              "dataset.cssHeight" in body)

        status = await client.get("/api/status")
        payload = await status.json()
        check("status reports version", payload.get("version") == "9.9.9",
              payload.get("version"))

        health = await client.get("/health")
        check("health endpoint still works", health.status == 200)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
