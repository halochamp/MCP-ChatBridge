# MCP-ChatBridge — Agent Overview

This file is the **quick operating map for an agent opened directly inside this public repository**.
MCP-ChatBridge was formerly named MCP-LinkGPT.

This project is self-contained. Do **not** assume access to a private parent repository or external agent procedure.

Read order:

1. [`CLAUDE.md`](CLAUDE.md) — hard project constraints.
2. **This file** — quick workflow and decision points.
3. [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md) — full repository-agent procedure.
4. [`README.md`](README.md) — exact tool/browser contract, setup, and examples.

## Core model

MCP-ChatBridge is a deterministic MCP bridge that lets a calling agent consult **ChatGPT Web, Gemini Web, or Claude Web** through each provider's own visible Chrome profile, and lets it generate images with Gemini or ChatGPT.

- One tool per provider: `ask_chatgpt`, `ask_gemini`, `ask_claude`.
- Lifecycle lives behind the `action` argument: `ask`, `status`, `last_response`, `new_chat`, `close`; plus `generate_image` for ChatGPT and Gemini.
- The web chat is a **read-only advisor**. It cannot read local files. The calling agent gathers evidence, verifies the answer, and owns every edit, command, and test.
- The page DOM changes often, so the bridge runs in **transcript mode**: it reads the whole conversation text and watches generic signals. The `response` is the whole chat (noise included), not just the answer. Read it yourself.

## Use it only when the user asks

Call these tools only when the user asks for a web-chat opinion, review, or image, or names a provider. Do not call them because a task is hard. A generic request for an "advisor" is not a request for this bridge. Never switch provider silently after a failure; report the reason and ask.

Full rule: [`AGENT_PROCEDURE.md` — User-triggered rule](AGENT_PROCEDURE.md#3-user-triggered-rule).

## Quick workflow

```text
ask_<provider>(action="status")        # once: open/check the profile, wait for ready
ask_<provider>(action="ask", prompt="<complete focused request>", new_chat=true)
ask_<provider>(action="last_response") # only after an explicit timeout, never to resend
ask_<provider>(action="close")         # closes only that provider's browser session
```

- Accept only `ok=true` with `status="completed"` as an answer.
- Send the evidence inline in the prompt, never secrets (the prompt goes to the provider).
- Treat the answer as a hypothesis; verify it locally before acting.
- Do not resend after a timeout or an ambiguous submit failure.

Full rules: [`AGENT_PROCEDURE.md` — Tool lifecycle](AGENT_PROCEDURE.md#6-tool-lifecycle) and [Failure and recovery](AGENT_PROCEDURE.md#8-failure-and-recovery).

## Images

`ask_gemini(action="generate_image", prompt="Generate an image: ...")` saves images to a folder outside the repository (default `~/Pictures/MCP-ChatBridge/gemini`). **Always send a detailed prompt**; see [`GEMINI_IMAGE_PROMPT_GUIDE_TH.md`](GEMINI_IMAGE_PROMPT_GUIDE_TH.md). `attach_images` attaches local PNG/JPEG/WEBP files for editing. ChatGPT can generate images too, but free accounts allow only a few per day: use it only when asked, never for tests.

Full rule: [`AGENT_PROCEDURE.md` — Images](AGENT_PROCEDURE.md#9-images-generation-and-attachments).

## When modifying MCP-ChatBridge itself

- Keep browser/session ownership, locking, and cleanup in the shared bridge layer (`common_bridge.py`, `chatgpt_web.py`); keep URL/login/challenge details in `providers/`.
- Do not add per-turn DOM counters or exact selectors as a correctness dependency; keep transcript mode.
- Run the full test suite after every change, then restart or reconnect the registered server and run one deliberate live check.

Full rule: [`AGENT_PROCEDURE.md` — Working on MCP-ChatBridge itself](AGENT_PROCEDURE.md#10-working-on-mcp-chatbridge-itself).

**Mental model:** local evidence -> web-chat answer -> local verification -> local action -> re-check -> closure.
