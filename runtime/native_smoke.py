"""Exercise official app-server through the router, using an isolated temporary task."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import sys

from router import Router, find_codex, read_json, web, aiohttp


class Rpc:
    def __init__(self, process):
        self.process = process
        self.number = 0
        self.notifications = []
        self.messages = []

    async def receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 120)
        if not line:
            raise RuntimeError("Native app-server exited unexpectedly")
        value = json.loads(line)
        if value.get("method") == "item/completed":
            item = value.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage":
                self.messages.append(item.get("text", ""))
        if value.get("method") == "error":
            raise RuntimeError(str(value["params"]))
        return value

    async def request(self, method, params):
        self.number += 1
        self.process.stdin.write((json.dumps({"id": self.number, "method": method, "params": params}) + "\n").encode())
        await self.process.stdin.drain()
        while True:
            value = await self.receive()
            if value.get("id") == self.number:
                if "error" in value:
                    raise RuntimeError(str(value["error"]))
                return value["result"]
            self.notifications.append(value)

    async def done(self, thread=None):
        while True:
            value = self.notifications.pop(0) if self.notifications else await self.receive()
            if value.get("method") == "turn/completed":
                if thread and value["params"].get("threadId") != thread:
                    continue
                if value["params"]["turn"].get("status") == "failed":
                    raise RuntimeError(str(value["params"]["turn"]))
                return value


def events(compact):
    item = {"type": "compaction", "encrypted_content": "TEST_CHECKPOINT"} if compact else {
        "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "MAPLE-7429", "annotations": []}]}
    return [{"type": "response.created", "response": {"id": "resp_test"}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"id": "resp_test", "status": "completed",
             "output": [item], "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}]


async def smoke(live=False, binary=None, automatic=False, quiet=False, subagents=False):
    original_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    with tempfile.TemporaryDirectory(prefix="compaction-test-") as directory:
        home = Path(directory)
        shutil.copy2(original_home / "models_cache.json", home / "models_cache.json")
        (home / "compaction-routing.json").write_text(json.dumps({"model": "gpt-5.6-terra", "reasoning_effort": "high"}))
        if live:
            shutil.copy2(original_home / "auth.json", home / "auth.json")
        else:
            # A dummy token is accepted by the local mock; it is never sent to OpenAI.
            (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "test-key"}))
        auto_config = 'model_auto_compact_token_limit = 500\n' if automatic or subagents else ''
        (home / "config.toml").write_text(auto_config + 'model = "gpt-6-astra"\nmodel_reasoning_effort = "low"\nweb_search = "disabled"\n[features]\nremote_compaction_v2 = true\nmulti_agent_v2 = true\n', encoding="utf-8")
        captured = []
        mock_runner = None
        spawned = False
        child_called = False
        def mock_events(body):
            nonlocal spawned, child_called
            compact = any(i.get("type") == "compaction_trigger" for i in body.get("input", []))
            result = events(compact)
            metadata = body.get("client_metadata") or {}
            purpose = json.loads(metadata.get("x-codex-turn-metadata", "{}"))
            child = bool(metadata.get("x-openai-subagent") or metadata.get("x-codex-parent-thread-id"))
            call = None
            if subagents and purpose.get("request_kind") == "turn" and not compact:
                if not child and not spawned:
                    spawned = True
                    call = ("spawn_agent", {"message": "Return MAPLE-7429.", "task_name": "routing_test", "fork_turns": "none"})
                elif child and not child_called:
                    child_called = True
                    call = ("list_agents", {})
            if call:
                item = {"type": "function_call", "call_id": "call_" + call[0], "name": call[0],
                        "namespace": "collaboration", "arguments": json.dumps(call[1])}
                result[1]["item"] = item
                result[-1]["response"]["output"] = [item]
            if (automatic and not compact) or (subagents and child and call):
                result[-1]["response"]["usage"] = {"input_tokens": 1000, "output_tokens": 5, "total_tokens": 1005}
            return result
        async def mock(request):
            if request.headers.get("Upgrade", "").lower() == "websocket":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        body = json.loads(message.data)
                        captured.append(body)
                        for event in mock_events(body):
                            await ws.send_json(event)
                return ws
            if request.path.endswith("/responses"):
                body = await request.json()
                captured.append(body)
                payload = "".join("data: " + json.dumps(item) + "\n\n" for item in mock_events(body))
                return web.Response(text=payload, content_type="text/event-stream")
            return web.json_response({})
        upstream = "https://chatgpt.com/backend-api/codex"
        if not live:
            app = web.Application(client_max_size=64*1024*1024)
            app.router.add_route("*", "/{tail:.*}", mock)
            mock_runner = web.AppRunner(app, access_log=None)
            await mock_runner.setup()
            site = web.TCPSite(mock_runner, "127.0.0.1", 0)
            await site.start()
            upstream = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        router = Router(home / "compaction-routing.json", home, upstream)
        base = await router.start()
        env = dict(os.environ, CODEX_HOME=str(home))
        log = (home / "stderr.log").open("wb")
        process = await asyncio.create_subprocess_exec(str(binary or find_codex()), "-c", f"openai_base_url={json.dumps(base)}",
            "app-server", env=env, cwd=directory, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=log)
        try:
            rpc = Rpc(process)
            await rpc.request("initialize", {"clientInfo": {"name": "compaction_router_test", "version": "1.0"}, "capabilities": {"experimentalApi": True}})
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            start = await rpc.request("thread/start", {"model": "gpt-6-astra", "cwd": directory, "ephemeral": True,
                "approvalPolicy": "never", "sandbox": "read-only"})
            thread = start["thread"]["id"]
            await rpc.request("turn/start", {"threadId": thread, "effort": "low", "input": [
                {"type": "text", "text": "Remember the reference code MAPLE-7429. Reply with the code only. Do not use any tools."}]})
            await rpc.done(thread)
            if subagents:
                for _ in range(200):
                    if any(e.get("subagent") and e["kind"] == "compaction" for e in router.events):
                        break
                    await asyncio.sleep(0.05)
                assert child_called, "Native subagent did not run"
                assert any(e.get("subagent") and e["model"] == "gpt-5.6-terra" and e["reasoning_effort"] == "high" for e in router.events), router.events
                if not quiet:
                    print(json.dumps({"passed": True, "native_subagent_auto_compaction": True, "requests": router.events}, indent=2), flush=True)
                return router.events
            if not automatic:
                await rpc.request("thread/compact/start", {"threadId": thread})
                await rpc.done()
            await rpc.request("turn/start", {"threadId": thread, "effort": "low", "input": [
                {"type": "text", "text": "What was the reference code? Reply with the code only. Do not use tools."}]})
            await rpc.done()
            compactions = [e for e in router.events if e["kind"] == "compaction"]
            turns = [e for e in router.events if e["kind"] == "turn"]
            assert compactions and all(e["model"] == "gpt-5.6-terra" and e["reasoning_effort"] == "high" for e in compactions), router.events
            assert len(turns) >= 2 and all(e["model"] == "gpt-6-astra" for e in turns), router.events
            assert rpc.messages and "MAPLE-7429" in rpc.messages[-1], rpc.messages
            if not live:
                assert any(i.get("type") == "compaction" for i in captured[-1].get("input", [])), "Native checkpoint missing from continuation"
            if not quiet:
                print(json.dumps({"passed": True, "live": live, "automatic": automatic, "reference_preserved": True, "requests": router.events}, indent=2), flush=True)
            return router.events
        except Exception:
            log.flush()
            print("Router events:", router.events, file=sys.stderr)
            print((home / "stderr.log").read_text(errors="replace")[-5000:], file=sys.stderr)
            raise
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.terminate()
                await process.wait()
            log.close()
            await router.close()
            if mock_runner:
                await mock_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(smoke(live="--live" in sys.argv, automatic="--automatic" in sys.argv, subagents="--subagents" in sys.argv))
