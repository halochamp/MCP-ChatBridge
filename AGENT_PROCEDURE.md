# MCP-ChatBridge — Agent Procedure

This document is the **complete repository-agent operating procedure** for working directly inside the public `MCP-ChatBridge` project (formerly `MCP-LinkGPT`).

It is intentionally self-contained. Do not assume the agent can read another private repository, a parent workspace, or external project rules.

Document roles:

- [`CLAUDE.md`](CLAUDE.md) — hard project constraints that must always hold.
- [`AGENT.md`](AGENT.md) — short overview and decision map.
- **This file** — full execution procedure for an agent working in this repository.
- [`README.md`](README.md) — product behavior, MCP tool contract, browser lifecycle, setup, examples, and user documentation.
- [`GEMINI_IMAGE_PROMPT_GUIDE_TH.md`](GEMINI_IMAGE_PROMPT_GUIDE_TH.md) — how to prompt Gemini for images (always detailed).

When instructions conflict, follow the most restrictive applicable rule. Never weaken the safety or ownership guarantees described in `CLAUDE.md` or `README.md`.

## 1. Purpose and boundaries

MCP-ChatBridge is a deterministic bridge from a calling agent to **ChatGPT Web, Gemini Web, and Claude Web** through `browser-use`/CDP. Each provider owns its own visible Chrome profile (default `~/.codex-<provider>/browser-profile`, outside the repository).

- The web chat is a **read-only advisor**. It is not the implementation worker and never replaces local verification.
- The calling agent owns local evidence, edits, commands, tests, integration, and the final answer.
- Browser actions are deterministic. There is no autonomous browser agent and no second LLM controlling the browser.

## 2. Hard constraints

- Never read, print, export, or commit browser cookies, passwords, or storage.
- Prompts and responses may be processed transiently for the intended request only; never persist them to application logs, commit them, or send them to unrelated destinations.
- Keep provider browser profiles outside the repository and isolated from each other.
- Restrict automatic navigation and page mutation to each provider's approved domains. Sign-in hosts (Google, Apple, Microsoft, provider auth hosts) are **authentication-only**: a page may open there, the bridge reports a login gate, but it never types, clicks, or mutates them.
- Stop on CAPTCHA, login, or human-verification gates. The user completes them manually in the opened window.
- Keep fail-closed response ownership: serialized operation, pinned page identity, observable submit acceptance, stable completion. Never resend automatically after an ambiguous post-submit failure.
- Keep transcript mode (section 7). Do not make per-turn DOM counters or exact selectors a correctness dependency.

## 3. User-triggered rule

Call these tools only when the user explicitly asks for a web-chat opinion, review, audit, or image, or names a provider (ChatGPT, Gemini, or Claude).

- Do not call them merely because a task is complex or another opinion might help.
- A generic request for an "advisor" or "second opinion" is not a request for this bridge unless the user says so.
- If the user names a different advisor or provider, follow that request instead of silently substituting.
- Once the user has requested a provider for an issue, follow-up and closure calls for the same issue are part of that authorized workflow.
- Never switch provider silently after a failure. Report the reason and ask.

## 4. Choosing a provider

- Use the provider the user named. If the user said only "via the bridge" for an opinion, ask which one, or use Gemini when the user has said Gemini is the intended default.
- Prefer Gemini for image generation. ChatGPT can generate images, but free accounts allow only a few per day, so use it only when asked and never for tests.
- Claude Web has no image generation in this bridge.

## 5. Evidence packet

The web chat cannot read local files. Before a call, gather and compress local evidence and send it **inline**:

- the exact question and what decision it supports;
- the relevant code or diff, test output, and constraints;
- paths, line numbers, and commit IDs as provenance labels only.

Never ask the chat to fetch omitted local files, use another connector, or infer changes from paths. Never include secrets, credentials, or private data: the content goes to the provider's web service.

## 6. Tool lifecycle

```text
ask_<provider>(action="status")
ask_<provider>(action="ask", prompt="...", new_chat=true|false, timeout_seconds=600)
ask_<provider>(action="last_response")   # explicit-timeout recovery only
ask_<provider>(action="new_chat")
ask_<provider>(action="close")
```

- Call `status` once before the first use in a session and continue only at `ready`.
- Calls are serialized per provider. Never call another browser action on the same provider while `ask` is running.
- `timeout_seconds` is 10-900. The submit step waits up to 120 s for an observable acceptance signal, polling the page every 0.15 s and returning as soon as one appears; it presses or clicks send once and never resends.
- Accept only `ok=true` with `status="completed"` as an answer. Progress tails are context only.
- `close` closes only that provider's browser session. Close before using the same profile in a plain browser, and close that plain browser afterwards.

## 7. Reading results (transcript mode)

Page DOMs drift constantly. All providers therefore run in transcript mode: the bridge reads the **whole conversation text** and watches generic signals (page-text growth and quiet time, a stop control when present, images that were not on the page before submit).

- `response` is the whole conversation text with `transcript: true`. It includes sidebar and footer noise. **Read it yourself** and extract the answer.
- With `new_chat=true` the conversation is only your exchange. With `new_chat=false` it includes earlier turns.
- `last_response` returns the current page's conversation text.
- A login path (`/login`, `/signin`, `/sign-in`, `/auth/login`) is reported as a login gate whatever the DOM looks like.

## 8. Failure and recovery

| Situation | Action |
|---|---|
| `status="timed_out"` | Do not resend. Use `last_response` once to recover; accept it only if it is complete. |
| Ambiguous submit failure ("no observable lifecycle acceptance signal") | Do not resend automatically. Report it and wait for the user's direction. |
| Login required | Ask the user to sign in manually in the opened window, then retry. Do not read credentials. |
| CAPTCHA / human verification | Ask the user to complete it manually. |
| `status="no_image"` | No usable image came back (refusal, wording, or a free daily limit). Show the text, do not resend. |
| `status="completed_collection_failed"` | The reply finished but no image could be saved. Use `last_response`; do not resend. |
| Provider unavailable | Report the reason; do not switch provider silently. |

## 9. Images, generation and attachments

- `action="generate_image"` on `ask_gemini` or `ask_chatgpt` sends the prompt and saves images to `save_dir` (default `~/Pictures/MCP-ChatBridge/<provider>`, must be outside the repository). `ok=true` returns `images` metadata (path, mime, bytes, sha256), never image bytes.
- **Always send a detailed prompt**: format, scene, camera/composition, materials and lighting, hex palette per part, and strict negatives. Start with `Generate an image:`. See the prompt guide.
- Open the saved image and check it against the prompt before reporting success.
- `attach_images` (with `ask` or `generate_image`) attaches up to 4 local PNG/JPEG/WEBP files (absolute or `~/` paths, 16 MB each). The files are uploaded to the provider: attach only images the user asked you to send. A bad path or failed upload stops the call before anything is submitted. The user's own attached pictures are skipped in the results.
- Generated images are never committed: `.gitignore` covers the bridge's file names.

## 10. Working on MCP-ChatBridge itself

- Keep session ownership, locking, cancellation, cleanup, and correlation in the shared layer (`common_bridge.py`, `chatgpt_web.py`); keep URL, login, and challenge details in `providers/`.
- Keep the MCP surface small: one `ask_<provider>` tool per provider, lifecycle behind `action`.
- After every code change run the full suite:

  ```bash
  BROWSER_USE_CONFIG_DIR=/tmp/mcp-chatbridge-test-browseruse-config \
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
  ```

- Unit tests cannot prove that a third-party web UI has not changed. After a change that affects a live path, restart or reconnect the registered server and run one deliberate, bounded live check per affected provider after manual sign-in. Do not burn free image quotas on tests.
- Prefer evidence from the real page (read-only probes of attributes and structure, never chat content) over guessed selectors.

## 11. Verification and closure

Before saying work is done:

1. the full test suite passes;
2. any live check you claim was actually run, with its observed result;
3. nothing private (local paths, accounts, cookies, generated images) is committed;
4. documentation matches the behavior, including this file and `README.md`.

If a live check could not be run (login required, free limit reached), say so explicitly instead of claiming full completion.

## 12. Quick decision table

| Situation | Action |
|---|---|
| User did not ask for a web-chat opinion or image | Work locally; do not call the bridge |
| User asks a provider for an opinion/review | `status`, then `ask` with an evidence packet |
| User asks for an image | `ask_gemini` `generate_image` with a detailed prompt |
| Timeout | No resend; bounded `last_response` recovery |
| Login or CAPTCHA | Ask the user to complete it manually |
| Code changed | Full tests, restart/reconnect, one bounded live check |
