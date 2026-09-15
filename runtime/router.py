"""Route native Codex compaction requests; never construct or retain conversation context."""
import asyncio
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
import aiohttp
from aiohttp import web
import zstandard

MAX_BODY = 64 * 1024 * 1024
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
       "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer"}
WS_HOP = HOP | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                "sec-websocket-protocol", "sec-websocket-accept"}


class RoutingError(ValueError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def classify(body, headers, path):
    if path.rstrip("/").endswith("/responses/compact"):
        return "compaction"
    metadata = body.get("client_metadata") or {}
    raw = metadata.get("x-codex-turn-metadata") or headers.get("x-codex-turn-metadata")
    try:
        details = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError as exc:
        raise RoutingError("Codex sent invalid request metadata; compaction routing stopped.") from exc
    kind = details.get("request_kind") or metadata.get("request_kind")
    trigger = any(isinstance(item, dict) and item.get("type") == "compaction_trigger"
                  for item in body.get("input", []) if isinstance(body.get("input"), list))
    if trigger:
        if kind and kind != "compaction":
            raise RoutingError("Conflicting compaction markers; routing stopped.")
        return "compaction"
    if kind in {"compaction", "turn", "prewarm", "memory"}:
        return kind
    raise RoutingError("This Codex request has an unrecognized purpose. Update Compaction Router; no model request was sent.")


def validate_selection(selection, coding_model, models):
    if set(selection) != {"model", "reasoning_effort"}:
        raise RoutingError("compaction-routing.json requires exactly model and reasoning_effort.")
    selected = models.get(selection["model"])
    coding = models.get(coding_model)
    if not selected or not coding:
        raise RoutingError("The coding or compaction model is absent from Codex's model catalog.")
    if selection["reasoning_effort"] not in {v["effort"] for v in selected.get("supported_reasoning_levels", [])}:
        raise RoutingError("The selected compaction model does not support this reasoning level.")
    if not selected.get("comp_hash") or selected["comp_hash"] != coding.get("comp_hash"):
        raise RoutingError("The coding and compaction models use incompatible checkpoint formats.")
    if (selected.get("context_window") or 0) < (coding.get("context_window") or 0):
        raise RoutingError("The compaction model has a smaller context window than the coding model.")
    if set(selected.get("input_modalities", [])) != set(coding.get("input_modalities", [])):
        raise RoutingError("The compaction model has different supported input types.")


class Router:
    def __init__(self, config_path, codex_home, upstream="https://chatgpt.com/backend-api/codex", audit=None):
        self.config_path = Path(config_path)
        self.codex_home = Path(codex_home)
        self.upstream = upstream.rstrip("/")
        self.audit = audit
        self.key = secrets.token_urlsafe(32)
        self.events = []

    def event(self, **fields):
        fields["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.events.append(fields)
        self.events = self.events[-100:]
        if self.audit:
            self.audit.parent.mkdir(parents=True, exist_ok=True)
            if self.audit.exists() and self.audit.stat().st_size > 1024 * 1024:
                self.audit.replace(self.audit.with_suffix(".previous.jsonl"))
            with self.audit.open("a", encoding="utf-8") as out:
                out.write(json.dumps(fields) + "\n")

    def rewrite(self, data, headers, path):
        body = json.loads(data)
        if not isinstance(body, dict) or not body.get("model"):
            raise RoutingError("Unexpected Codex model request structure.")
        kind = classify(body, headers, path)
        source = body["model"]
        if kind == "compaction":
            selection = read_json(self.config_path)
            cache = read_json(self.codex_home / "models_cache.json")
            models = {model["slug"]: model for model in cache["models"]}
            validate_selection(selection, source, models)
            body["model"] = selection["model"]
            body["reasoning"] = dict(body.get("reasoning") or {}, effort=selection["reasoning_effort"])
            self.event(kind=kind, coding_model=source, model=body["model"],
                       reasoning_effort=selection["reasoning_effort"],
                       subagent=bool((body.get("client_metadata") or {}).get("x-openai-subagent")
                                     or headers.get("x-openai-subagent")
                                     or (body.get("client_metadata") or {}).get("x-codex-parent-thread-id")))
            return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(), True
        self.event(kind=kind, model=source, reasoning_effort=(body.get("reasoning") or {}).get("effort"))
        return data, False

    async def start(self):
        self.client = aiohttp.ClientSession(auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None),
            cookie_jar=aiohttp.DummyCookieJar(), skip_auto_headers={"User-Agent", "Content-Type", "Accept-Encoding"})
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None, auto_decompress=False)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/{self.key}/backend-api/codex"
        return self.base_url

    async def close(self):
        await self.runner.cleanup()
        await self.client.close()

    async def handle(self, request):
        prefix = f"/{self.key}/backend-api/codex"
        if not request.path.startswith(prefix + "/") or request.headers.get("Origin"):
            raise web.HTTPNotFound()
        suffix = request.raw_path[len(prefix):]
        url = self.upstream + suffix
        try:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self.websocket(request, url)
            data = await request.read()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
            if request.method == "POST" and "/responses" in request.path:
                encoding = headers.get("Content-Encoding", "").lower()
                if encoding == "zstd":
                    decoded = zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_BODY)
                elif encoding == "gzip":
                    decoded = gzip.decompress(data)
                elif encoding == "deflate":
                    decoded = zlib.decompress(data)
                elif not encoding or encoding == "identity":
                    decoded = data
                else:
                    raise RoutingError("Unsupported request compression; routing stopped.")
                rewritten, changed = self.rewrite(decoded, request.headers, request.path)
                if changed:
                    data = rewritten
                    headers = {k: v for k, v in headers.items() if k.lower() != "content-encoding"}
            async with self.client.request(request.method, url, headers=headers, data=data,
                                           allow_redirects=False) as response:
                downstream = web.StreamResponse(status=response.status, headers={
                    k: v for k, v in response.headers.items() if k.lower() not in HOP})
                await downstream.prepare(request)
                async for chunk in response.content.iter_any():
                    await downstream.write(chunk)
                await downstream.write_eof()
                return downstream
        except (RoutingError, ValueError, KeyError, OSError) as exc:
            message = str(exc) if isinstance(exc, RoutingError) else "Compaction Router configuration or catalog could not be read."
            self.event(kind="error", message=message)
            return web.json_response({"error": {"message": message, "type": "compaction_router_error"}}, status=400)
        except aiohttp.ClientError:
            self.event(kind="error", message="Could not connect to the official Codex service.")
            return web.json_response({"error": {"message": "Compaction Router could not reach OpenAI."}}, status=502)

    async def websocket(self, request, url):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in WS_HOP}
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        async with self.client.ws_connect(url, headers=headers, protocols=protocols,
                                         max_msg_size=MAX_BODY, autoping=True) as upstream:
            response_headers = {k: v for k, v in upstream._response.headers.items() if k.lower() not in WS_HOP}
            downstream = web.WebSocketResponse(max_msg_size=MAX_BODY, protocols=protocols)
            downstream.headers.update(response_headers)
            await downstream.prepare(request)

            async def upload():
                async for message in downstream:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        data = message.data.encode()
                        value = json.loads(data)
                        try:
                            if value.get("type") == "response.create":
                                data, _ = self.rewrite(data, {}, request.path)
                            await upstream.send_str(data.decode())
                        except RoutingError as exc:
                            self.event(kind="error", message=str(exc))
                            await downstream.send_json({"type": "error", "error": {"message": str(exc), "type": "compaction_router_error"}})
                            await downstream.close(code=1008, message=b"Compaction routing rejected request")
                            break
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        raise RoutingError("Unexpected binary Codex request; routing stopped.")

            async def download():
                async for message in upstream:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream


def find_codex():
    location = ROOT / "official-extension.json"
    extensions = Path(read_json(location)["extensions_dir"]) if location.exists() else Path.home() / ".vscode" / "extensions"
    installed = read_json(extensions / "extensions.json")
    matches = [item for item in installed if item["identifier"]["id"].lower() == "openai.chatgpt"]
    if len(matches) != 1:
        raise RoutingError("Cannot identify the active official Codex extension.")
    binary = extensions / matches[0]["relativeLocation"] / "bin" / "windows-x86_64" / "codex.exe"
    if not binary.is_file():
        raise RoutingError("The updated Codex extension has changed its executable location.")
    return binary


async def launch(args):
    binary = find_codex()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if "app-server" in args and "generate-json-schema" not in args and "generate-ts" not in args:
        fingerprint = hashlib.sha256()
        for path in [binary, ROOT / "router.py", ROOT / "native_smoke.py"]:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    fingerprint.update(chunk)
        expected = fingerprint.hexdigest()
        verification = ROOT / "verified.json"
        previous = read_json(verification) if verification.exists() else {}
        if previous.get("fingerprint") != expected:
            print("Compaction Router: checking the official Codex version locally (no model tokens used).", file=sys.stderr)
            from native_smoke import smoke
            await smoke(binary=binary, quiet=True)
            await smoke(binary=binary, automatic=True, quiet=True)
            await smoke(binary=binary, subagents=True, quiet=True)
            temporary = ROOT / f"verified-{os.getpid()}.tmp"
            temporary.write_text(json.dumps({"fingerprint": expected, "binary": str(binary),
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "checks": ["native manual compaction", "native automatic compaction", "native subagent compaction", "native checkpoint continuation"]}), encoding="utf-8")
            temporary.replace(verification)
    router = Router(codex_home / "compaction-routing.json", codex_home, audit=ROOT / "routing.jsonl")
    base = await router.start()
    try:
        process = await asyncio.create_subprocess_exec(str(binary), "-c", f"openai_base_url={json.dumps(base)}", *args)
        return await process.wait()
    finally:
        await router.close()


if __name__ == "__main__":
    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)
    try:
        sys.exit(asyncio.run(launch(sys.argv[1:])))
    except (RoutingError, OSError, ValueError, RuntimeError, AssertionError) as exc:
        print(f"Compaction Router: {exc}", file=sys.stderr)
        sys.exit(1)
