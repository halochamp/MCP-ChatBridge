# MCP-ChatBridge project rules

- This project is a deterministic MCP bridge from Codex to ChatGPT Web, Gemini Web, and Claude Web through `browser-use`/CDP.
- Keep it independent from `Endeavor_Hands`; do not import or edit that project.
- Keep the MCP surface small: one `ask_<provider>` tool per provider. Provider lifecycle operations belong behind that tool's `action` argument rather than multiplying top-level tools.
- There is no default provider for advisor/review requests: use these tools only when the user asks for them or names a provider in that session. A generic request for an "advisor" or "second opinion" is not a request for this bridge. Prefer `ask_gemini` with `action="generate_image"` for image generation; `ask_chatgpt` can also generate images but only when the user names ChatGPT (free accounts allow just a few images per day, so never use it for tests). Never switch providers silently after a failure.
- Keep browser/session ownership, locking, cancellation, cleanup, target binding, and correlation logic in the shared bridge layer. Keep URL/DOM/login/challenge details in provider adapters.
- Do not add an autonomous browser agent or another browser-control LLM. Browser actions must remain deterministic.
- Never read, print, export, or commit browser cookies, passwords, prompts, or responses.
- Keep each provider's browser profile outside the repository by default and isolated from the other providers.
- Restrict automatic navigation and DOM mutation to each adapter's approved domains and stop on CAPTCHA, login, or human-verification gates.
- For Quill/ProseMirror-style rich editors, prefer native CDP keyboard/input events over DOM `execCommand`; visible text is not sufficient proof that the editor's internal state is synchronized.
- All three providers run in transcript mode: read the whole conversation text and watch generic signals (text growth/quiet, stop control, new images), never per-turn DOM counters or exact selectors, because the page DOM drifts. A login path is a login gate. Keep it that way when adding providers or features.
- Preserve fail-closed response ownership from lifecycle signals: serialized operation, pinned page identity, observable submit acceptance, turn-count monotonicity when available, and stable assistant completion. Do not use fuzzy or exact rendered-DOM text equality as an ownership gate, and do not automatically resend after an ambiguous post-submit failure.
- Run `BROWSER_USE_CONFIG_DIR=/tmp/mcp-chatbridge-test-browseruse-config PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` (with the pinned dependencies installed) after code changes.
