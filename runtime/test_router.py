import copy
import json
from pathlib import Path
import tempfile
import unittest

from router import Router, RoutingError, aiohttp, web, zstandard


def body(kind="turn", subagent=False):
    metadata = {"x-codex-turn-metadata": json.dumps({"request_kind": kind})}
    if subagent:
        metadata["x-openai-subagent"] = "spawn_agent"
    return {"model": "gpt-6-astra", "reasoning": {"effort": "low", "summary": "auto"},
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Keep every byte of this context value."}]}],
            "tools": [{"type": "function", "name": "test", "parameters": {}}],
            "client_metadata": metadata, "stream": True, "store": False}


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = self.home / "compaction-routing.json"
        self.config.write_text(json.dumps({"model": "gpt-5.6-terra", "reasoning_effort": "high"}))
        models = [{"slug": name, "comp_hash": "test-compatible", "context_window": 272000,
                   "input_modalities": ["text", "image"], "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}
                  for name in ["gpt-6-astra", "gpt-5.6-terra"]]
        (self.home / "models_cache.json").write_text(json.dumps({"models": models}))
        self.captured = []
        async def mock(request):
            if request.headers.get("Upgrade", "").lower() == "websocket":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for message in ws:
                    self.captured.append(message.data.encode())
                    await ws.send_str(message.data)
                return ws
            data = await request.read()
            self.captured.append(data)
            return web.Response(body=data, headers={"x-codex-turn-state": "state-token"})
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", mock)
        self.server = web.AppRunner(app, access_log=None)
        await self.server.setup()
        site = web.TCPSite(self.server, "127.0.0.1", 0)
        await site.start()
        upstream = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.router = Router(self.config, self.home, upstream)
        self.base = await self.router.start()
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.router.close()
        await self.server.cleanup()
        self.temp.cleanup()

    async def test_coding_http_is_byte_identical_and_preserves_headers(self):
        raw = json.dumps(body(), indent=3).encode()
        async with self.client.post(self.base + "/responses", data=raw) as result:
            self.assertEqual(await result.read(), raw)
            self.assertEqual(result.headers["x-codex-turn-state"], "state-token")
        self.assertEqual(self.captured, [raw])

    async def test_all_compaction_paths_only_change_model_and_effort(self):
        for path, kind in [("/responses", "compaction"), ("/responses/compact", "turn")]:
            original = body(kind, subagent=True)
            expected = copy.deepcopy(original)
            expected["model"] = "gpt-5.6-terra"
            expected["reasoning"]["effort"] = "high"
            async with self.client.post(self.base + path, json=original) as result:
                self.assertEqual(await result.json(content_type=None), expected)
        self.assertTrue(all(e["subagent"] for e in self.router.events))

    async def test_zstd_compaction(self):
        original = body("compaction")
        compressed = zstandard.ZstdCompressor().compress(json.dumps(original).encode())
        async with self.client.post(self.base + "/responses", data=compressed, headers={"Content-Encoding": "zstd"}) as result:
            value = json.loads(await result.read())
            self.assertEqual(value["input"], original["input"])
            self.assertEqual(value["model"], "gpt-5.6-terra")

    async def test_websocket_coding_and_compaction(self):
        async with self.client.ws_connect(self.base + "/responses") as ws:
            original = dict(body(), type="response.create")
            raw = json.dumps(original, indent=4)
            await ws.send_str(raw)
            self.assertEqual((await ws.receive()).data, raw)
            compact = dict(body("compaction", subagent=True), type="response.create")
            await ws.send_json(compact)
            expected = copy.deepcopy(compact)
            expected["model"] = "gpt-5.6-terra"
            expected["reasoning"]["effort"] = "high"
            self.assertEqual(json.loads((await ws.receive()).data), expected)

    async def test_unknown_protocol_stops_before_sending(self):
        original = body()
        del original["client_metadata"]
        async with self.client.post(self.base + "/responses", json=original) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.captured, [])

    async def test_incompatible_model_stops_before_sending(self):
        cache = json.loads((self.home / "models_cache.json").read_text())
        cache["models"][1]["comp_hash"] = "incompatible"
        (self.home / "models_cache.json").write_text(json.dumps(cache))
        async with self.client.post(self.base + "/responses", json=body("compaction")) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.captured, [])

    async def test_configuration_changes_apply_on_next_compaction(self):
        self.config.write_text(json.dumps({"model": "gpt-5.6-terra", "reasoning_effort": "low"}))
        async with self.client.post(self.base + "/responses", json=body("compaction")) as result:
            self.assertEqual(json.loads(await result.read())["reasoning"]["effort"], "low")

    async def test_local_route_requires_random_key_and_rejects_browser_origins(self):
        async with self.client.post(self.base.replace(self.router.key, "wrong") + "/responses", json=body()) as result:
            self.assertEqual(result.status, 404)
        async with self.client.post(self.base + "/responses", json=body(), headers={"Origin": "https://example.com"}) as result:
            self.assertEqual(result.status, 404)
        self.assertEqual(self.captured, [])


if __name__ == "__main__":
    unittest.main()
