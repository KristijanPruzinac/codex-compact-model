# Codex Compact Model

Choose a separate **model and reasoning effort for compaction** while keeping the official OpenAI Codex extension in charge of coding, tools, conversation history, and subagents.

Initial release: **Windows x64, local VS Code, ChatGPT sign-in**. This is an independent community extension, not an OpenAI product.

## Use

1. Install the official **Codex** extension (`openai.chatgpt`), sign in, and start a task so its model catalog is available.
2. Install [Codex Compact Model from the VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=Kristijan.codex-compact-model), or install the Windows VSIX from [GitHub Releases](https://github.com/KristijanPruzinac/codex-compact-model/releases).
3. Run **Codex Compact Model: Enable** from the Command Palette, then reload VS Code.
4. Click **Compact** in the status bar, or run **Codex Compact Model: Configure Model and Reasoning**.

The default is **GPT-5.6 Terra / high**. A changed selection applies to the next compaction request, including requests from existing subagents.

The coding model remains the one you choose in Codex. Normal turns, manual compaction, automatic compaction, and native subagent compaction are covered. The extension keeps the native compaction prompt, input, tools, checkpoint format, and continuation behavior.

## How it works

```text
Official Codex extension → small local launcher → current official codex.exe
                                                  ↓
                               loopback request router → OpenAI
```

The launcher uses Codex's `openai_base_url` configuration for a local HTTP/WebSocket router. It recognizes native compaction metadata and changes only `model` and `reasoning.effort` in compaction requests. Normal model requests are forwarded without rewriting their body. It neither generates its own summaries nor constructs a separate agent context.

The router listens only on `127.0.0.1`, with a random URL token for each launch. It connects to the official Codex service using HTTPS with certificate verification. Existing authentication headers pass through in memory. Logs contain request purpose, model, reasoning, and timestamps; they do not record prompts, responses, or authentication headers. No external analytics are added.

Python and its required libraries are bundled. Users do not need to install Python, a compiler, another agent framework, or a VM.

## Updates and compatibility

The official Codex installation is not patched or replaced. The launcher resolves the currently installed Codex extension from VS Code's extension registry. When the official executable or router changes, it runs local tests for manual compaction, automatic compaction, subagent compaction, and checkpoint continuation before launching the real backend. These tests use a local mock and consume no model tokens.

Unknown request metadata and incompatible model checkpoint formats stop with an error instead of silently using a different model. Future compatibility cannot be guaranteed: if OpenAI changes the private protocol, an update to this extension may be required. **Codex Compact Model: Show Status** displays the last successful check.

This integration uses `chatgpt.cliExecutable`, a development setting of the official extension. It does not apply to Codex Desktop, remote Codex hosts, WSL, VS Code Web, or API-key/custom-provider sessions in this release. It supports the normal current Codex configuration; a custom provider or explicit per-task endpoint override can bypass the local router.

Selected models must be present in the local Codex model catalog, advertise the reasoning level, and have compatible checkpoint formats, context sizes, and input types. Using a cheaper model may reduce compaction cost, but reasoning-token usage and subscription accounting vary; savings and summary quality are not guaranteed.

## Settings and recovery

Selection is stored in `$CODEX_HOME/compaction-routing.json` (normally `%USERPROFILE%\.codex\compaction-routing.json`):

```json
{"model": "gpt-5.6-terra", "reasoning_effort": "high"}
```

Use **Codex Compact Model: Show Routing Log** to verify each request's selection. Logs rotate at roughly 1 MB.

To restore the standard integration, run **Codex Compact Model: Disable**, then reload VS Code. If the extension cannot start or has already been uninstalled, remove `chatgpt.cliExecutable` from your user settings and reload VS Code. Conversation files remain managed by Codex.

## Develop

On Windows x64, with Node.js, Python 3.13, and the standard .NET Framework compiler:

```powershell
npm ci
npm run build
npm run check
npm run package
```

`scripts/build.ps1` downloads the official embeddable Python distribution, verifies its published SHA-256 checksum, installs pinned libraries, compiles the small launcher, and runs the router tests. Native compatibility tests can also run locally after signing into the official Codex extension:

```powershell
runtime\python\python.exe runtime\native_smoke.py
runtime\python\python.exe runtime\native_smoke.py --automatic
runtime\python\python.exe runtime\native_smoke.py --subagents
```

`--live` performs a small real model request sequence using your existing sign-in and consumes account usage. Routine tests do not.

## License

MIT. Bundled Python and libraries retain their own licenses, shipped alongside them. See [THIRD_PARTY.md](THIRD_PARTY.md).
