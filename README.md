# MCP-ChatBridge

`MCP-ChatBridge` (formerly MCP-LinkGPT) is a small local MCP server that lets Codex explicitly consult
three browser-based AI advisors through deterministic `browser-use`/CDP control:

- ChatGPT Web
- Gemini Web
- Claude Web

Codex remains responsible for gathering evidence, deciding what to change,
implementing changes, and verifying them. The browser bridge is intentionally
not an autonomous browser agent.

```text
                         ┌─ ask_chatgpt ─> providers/chatgpt.py ─┐
Codex -> MCP-ChatBridge ----├─ ask_gemini  ─> providers/gemini.py  ├─> common bridge/session lifecycle -> browser-use/CDP
                         └─ ask_claude  ─> providers/claude.py  ─┘
```

The shared lifecycle owns profile locking, browser startup/shutdown,
cancellation behavior, CDP target binding, page identity, response ownership,
correlation, timeout handling, and fail-closed cleanup. Provider adapters own
only the parts that differ by site: approved domains, home/login routes, DOM
selectors, composer/send controls, streaming signals, and conversation routes.

## MCP tool surface

The MCP server exposes exactly three top-level tools:

- `ask_chatgpt(...)`
- `ask_gemini(...)`
- `ask_claude(...)`

Each tool uses the same interface and supports these `action` values:

- `ask` — send one prompt and wait for a confirmed final response.
- `status` — open/check the dedicated provider profile without sending a prompt.
- `last_response` — bounded recovery after an explicit timeout; never resends.
- `new_chat` — open a fresh conversation.
- `close` — close only that provider's browser session.

This keeps the MCP surface at three tools instead of multiplying provider
lifecycle operations into 15 top-level tools.

### Image generation (`ask_gemini` and `ask_chatgpt`)

`ask_gemini` and `ask_chatgpt` additionally accept `action="generate_image"`. It is
not a new tool; Claude Web rejects the action. The examples below use Gemini;
ChatGPT specifics are in "ChatGPT image generation and attachments".

```text
ask_gemini(
  action="generate_image",
  prompt="Generate an image: <explicit image request>",
  save_dir="",        # default ~/Pictures/MCP-ChatBridge/gemini ; absolute or ~/..., outside this repo
  max_images=4,       # 1-8
  timeout_seconds=600
)
```

The prompt goes through the same fail-closed `ask` lifecycle (serialized
operation, pinned page identity, observable submit acceptance, no automatic
resend). After the owned response is stable, the bridge runs a collection step
inside the same locked operation:

1. Enumerate **every** `<img>` in the owned assistant turn (no Gemini-specific
   image selector), then filter in `image_store.py`: visible, at least 128 px on
   the short side, allowed host (`*.googleusercontent.com`, `*.gstatic.com`,
   `*.google.com`, or page-local `blob:`/`data:image`), de-duplicated. When the
   UI changes, adjust that one policy instead of a selector chain.
2. Download, in order of preference:
   - **Full size:** click the page's own download control (found by its `download`
     icon name, scoped to the one image it belongs to) while Chrome downloads are
     redirected by CDP `Browser.setDownloadBehavior` into a private temp dir, then
     read the file and delete the dir (`source="full_download"`,
     `method="download_button"`).
   - Fallback: in-page `fetch` (original-size `=s0` URL, then the displayed URL),
     then a canvas re-encode of the displayed image (displayed size only).

   Cookies are never read or exported.
3. Validate by magic bytes (PNG/JPEG/WEBP only, 16 MB cap) and save as
   `gemini-<UTC time>-<sha256 prefix>.<ext>` with file mode `0600`.

Results (image bytes are never returned, only metadata):

- `ok=true`, `status="completed"`, `images=[{path, mime, bytes, sha256, source, method, ...}]`,
  `image_count`, `save_dir`, plus any text in `response` (may be empty for an
  image-only turn). `image_errors` lists images that failed while others saved.
- `ok=false`, `status="no_image"`, `error_type="NoImageGeneratedError"` — Gemini
  answered without a usable image (refusal, quota, wording). The text is in
  `response`; nothing is resent.
- `ok=false`, `status="completed_collection_failed"` — the response completed but
  no image could be saved. Use `last_response`; do not resend the prompt.

Phrase the prompt as an explicit image request; the bridge does not rewrite it.

**Always use a detailed prompt** (format, scene, camera, materials/lighting, hex palette per
part, strict negatives). Short prompts gave small centered compositions with empty side panels;
the detailed prompt gave a full-bleed render. See
[`GEMINI_IMAGE_PROMPT_GUIDE_TH.md`](GEMINI_IMAGE_PROMPT_GUIDE_TH.md) for the template, a worked
example and fixes for common defects.

#### Attaching images (`attach_images`)

`ask_gemini(action="ask" | "generate_image", attach_images=["/abs/path.png", ...])`
attaches up to 4 local PNG/JPEG/WEBP files (absolute or `~/` paths, 16 MB each)
so Gemini can describe or edit them (image-to-image).

- Paths are validated before any browser work: regular file, image magic bytes,
  size cap, de-duplicated. A bad path stops the call with nothing sent.
- Inside the locked operation, before the prompt is typed: open the attach menu
  (control found by its `plus` icon name, nearest to the composer), set the files
  on the page's `input[type=file]` (prefers the `image/*` one) with CDP
  `DOM.setFileInputFiles`, then wait until the previews appear and the upload
  spinner is gone. If that fails the call stops before submitting.
- The files are uploaded to Google. Attach only images the user asked to send.

#### Language independence

Send and stop buttons, the attach trigger, and the download control are matched by
icon name (`arrow_upward`, `stop`, `plus`, `download`), not by localized
`aria-label`, so a non-English Gemini UI keeps working. The old English labels
stay as extra selectors.

#### Live verification (2026-10-31)

- `generate_image` saved a full-size JPEG (2752x1536) through the download control,
  versus the 1024x572 displayed size; the temp download dir was removed.
- `ask` with one attached PNG: Gemini described the attached image correctly.
- `generate_image` with one attached PNG: produced a 2750x1536 redraw of it.
- Icon-based selectors matched the live send button (typed text) and stop button
  (while responding).
- Gemini's fresh-chat route appears ~9-11s after Enter, past the old fixed 8s wait.
  `submit_timeout_seconds` (how long `_submit` waits for an observable acceptance
  signal) is now 120 for every provider, a user decision because the services are
  sometimes slow. It only delays a failing submit; the lifecycle still never
  resends.

### Transcript mode (ChatGPT, Gemini, Claude)

Page DOMs drift constantly. Live on 2026-10-31 ChatGPT dropped `<article>` and
`data-message-author-role`, so the per-turn counters were always 0 and every `ask`
failed with "no observable lifecycle acceptance signal" although the message had
been sent and answered. All three providers therefore run in transcript mode
(`ProviderConfig.transcript_mode`): the bridge reads the **whole conversation
text** and watches only generic signals, and the caller reads the text (noise
included, such as sidebar and footer text).

- `_page_state` adds `page_text_len`, `page_text_tail` and `page_images` for the
  `main` element (falling back to `body`), with the composer form excluded so
  typing and submitting do not move the numbers.
- Submit acceptance also accepts page-text growth (besides the old counters and
  the stop control). `_submit` polls the page every 0.15 s and returns the moment a
  signal appears; `submit_timeout_seconds` (120) is only a ceiling. It presses or
  clicks send once and never resends.
- The response is finished when the text and images stop changing: 3 s after a
  stop control disappears when one was ever seen, otherwise
  `transcript_stable_seconds` (8 s). An image request also needs a **new** image
  beyond the ones the user attached; with no image it reports `no_image` 3 s after
  the stop control (45 s when none was ever seen).
- `response` is the whole conversation text and `transcript` is `true`. A fresh
  chat contains only our exchange; `new_chat=false` includes earlier turns.
  `last_response` returns the current page's conversation text.
- Response ownership is unchanged: serialized operation, pinned page identity
  (the provisional route may move to the final route once), observable submit
  acceptance, no automatic resend.
- A page whose path starts with `/login`, `/signin`, `/sign-in` or `/auth/login` is
  reported as a login gate whatever its DOM looks like.
- Images: every `<img>` on the page outside the composer form that was not there
  before submit is a candidate; the first N (the images you attached, which render
  before any generated image) are skipped and the last `max_images` are kept.

### ChatGPT image generation and attachments

`ask_chatgpt(action="generate_image", ...)` and `attach_images` work like the Gemini
versions, with these ChatGPT specifics (verified live on 2026-10-31 with one
attach + generate run that saved a 1678x937 PNG):

- Free accounts can create only a few images (and uploads) per day. Do not use this
  for casual tests; when the limit is reached the reply is text and the result is
  `status="no_image"`, never a resend. Prefer `ask_gemini` for images.
- Default folder `~/Pictures/MCP-ChatBridge/chatgpt` (Gemini keeps `.../gemini`).
- ChatGPT has no inline download button (only "Generated image N", "Edit ...",
  "Share ..."), so the image bytes are fetched from the page; they are the
  full-size file, not a thumbnail.
- Attaching: the page keeps four file inputs from load and only the one **without**
  an `accept` attribute attaches. A file set immediately after the page loads was
  silently ignored twice, while one set after a 3 s settle attached in about 2 s,
  so `attach_settle_seconds` is 3 and the files are re-issued (up to 3 tries, 6 s
  apart) if nothing changes. Nothing is sent if the upload never shows.
- A fresh chat moves `/` to `/c/local-chatgpt%3A<uuid>` to `/c/<uuid>`; the
  provisional prefix `/c/local-chatgpt` is configured, otherwise the submit check
  saw an unknown route.
- Claude Web: needs a manual sign-in in its profile window. Claude's transcript-mode
  `ask` was verified live after that sign-in.

### Sign-in hosts (all providers)

A future sign-in must be able to finish inside the bridge-controlled window. Before
this was added, Claude's window blocked a Google sign-in and kept reopening (seen
live on 2026-10-31). Each provider now lists its sign-in hosts as **authentication
hosts only**: the page may open there and the bridge reports a login gate, but they
are never `interactive_roots`, so the bridge never types, clicks or mutates them.

| Provider | Extra sign-in hosts |
|---|---|
| ChatGPT | `auth.openai.com`, Google (`accounts.google.com`, `accounts.youtube.com`), Apple (`appleid.apple.com`), Microsoft (`login.microsoftonline.com`, `login.live.com`) |
| Gemini | Google (`accounts.google.com`, `accounts.youtube.com`) |
| Claude | `auth.anthropic.com`, Google (`accounts.google.com`, `accounts.youtube.com`) |

`accounts.youtube.com` is part of Google's sign-in redirect chain; that is general
knowledge and has not been exercised live here. Complete CAPTCHA or verification
prompts manually; the bridge still stops on them.

### Provider selection

No provider is the default for advisor, review, or second-opinion requests, and a
plain `advisor` request is not a ChatBridge request (see the repository `CLAUDE.md`
routing rule). Use these tools only when the user says to use them via ChatBridge
or names a provider in that session: plain "via ChatBridge" means `ask_gemini`;
`ask_chatgpt` and `ask_claude` only when the user names them. Claude's image
generation always uses `ask_gemini` with `action="generate_image"`. If the
requested provider is unavailable, report the reason and obtain explicit
direction before switching providers; never switch silently.

Example:

```text
ask_gemini(action="status")

ask_gemini(
  action="ask",
  prompt="<complete focused request>",
  new_chat=true,
  timeout_seconds=600
)

ask_gemini(action="last_response")
ask_gemini(action="close")
```

A successful advisory answer must have:

```text
ok = true
status = "completed"
```

Progress notifications and visible response tails are context only. They are
not final advice.

## Migration from the previous ChatGPT-only surface

The Agentic version now uses the three-tool surface above. Previous top-level
names such as:

```text
chatgpt_status
chatgpt_ask
chatgpt_last_response
chatgpt_new_chat
chatgpt_close
```

are no longer MCP tools. Their behavior moved behind `ask_chatgpt(action=...)`.
For example:

```text
chatgpt_status()              -> ask_chatgpt(action="status")
chatgpt_ask(prompt="...")     -> ask_chatgpt(action="ask", prompt="...")
chatgpt_last_response()       -> ask_chatgpt(action="last_response")
chatgpt_new_chat()            -> ask_chatgpt(action="new_chat")
chatgpt_close()               -> ask_chatgpt(action="close")
```

## Provider profiles

Each provider owns a separate persistent Chrome profile:

```text
ChatGPT  ~/.codex-chatgpt/browser-profile
Gemini   ~/.codex-gemini/browser-profile
Claude   ~/.codex-claude/browser-profile
```

Optional overrides:

```bash
export CODEX_CHATGPT_PROFILE_DIR="$HOME/.codex-chatgpt/browser-profile"
export CODEX_GEMINI_PROFILE_DIR="$HOME/.codex-gemini/browser-profile"
export CODEX_CLAUDE_PROFILE_DIR="$HOME/.codex-claude/browser-profile"
```

Profiles must remain outside the repository. They contain authenticated browser
state and must never be committed or shared.

Only one MCP process can own a given provider profile at a time. The providers
do not share locks or authenticated state with one another.

## Deterministic security model

MCP-ChatBridge intentionally keeps browser control narrow.

For every provider:

1. Automatic navigation is restricted to the provider adapter's approved
   interactive/login domains.
2. Every DOM mutation rechecks HTTPS, port, credentials-in-URL, hostname, and
   the bound page identity before changing the composer or submitting a prompt.
3. CDP work is pinned to one browser target for the duration of an operation.
4. CAPTCHA, login, and human-verification gates stop the bridge for manual user
   action; they are never bypassed.
5. Browser cookies, passwords, prompts, and responses are not written to
   application logs or exported through storage APIs.
6. If startup/shutdown/cancellation leaves browser ownership uncertain, the
   profile stays blocked rather than being silently reused. A persisted marker
   older than the recovery grace may self-clear only while the cross-process
   profile lock is held, process inspection succeeds, and no process names that
   exact `--user-data-dir`; any live owner or uncertain scan remains blocked.
7. A cancellation after prompt submission is not treated as a rollback.

The shared lifecycle continues to disable browser-use's storage-state cookie
watchdog for persistent profiles. Chrome itself owns the profile state.

Gemini's Quill editor and Claude's ProseMirror editor are driven through native
CDP input events (`Select All`/Backspace, `Input.insertText`, then Enter) rather
than DOM `execCommand` mutation. This keeps the visible composer and the site's
internal rich-editor state synchronized for long and multiline prompts. Enter is
the single native submit action; if lifecycle acceptance is delayed or ambiguous,
the bridge waits through the submit deadline and fails safely instead of issuing a
second DOM-button click that could duplicate the turn.

## Response ownership and correlation

The bridge does not accept "the latest text on the page" as sufficient proof
that a response belongs to the prompt it just sent.

Before submission it verifies only structural readiness: the guarded composer
is still present, visible, and non-empty. It deliberately does not compare the
rendered DOM text with the source prompt because Quill, ProseMirror, and other
rich editors may rewrite whitespace, Markdown blocks, or accessibility text.
After submission ownership is established from lifecycle signals:

- the bound browser target and serialized operation;
- the pinned page/conversation identity;
- an observable submit-acceptance signal (new user turn, new assistant turn, or
  streaming state); for a fresh-chat route, the bridge first baselines the new
  conversation and requires a later lifecycle mutation on that exact route before
  binding it to the operation;
- user-turn count monotonicity when that semantic node is available; and
- a new assistant/model turn that becomes non-streaming and stable.

Rendered user text is presentation data, not an ownership oracle. General fuzzy
string similarity and exact rendered-text gates are both intentionally avoided.

## Timeout and retry rules

A timeout after prompt submission is ambiguous: the provider may still finish
the same request later. Therefore do not automatically resend.
If the bridge has already bound a new assistant turn to this request, the
terminal tool result has `ok=false`, `status="timed_out"`, and the cumulative
response generated so far (up to 100,000 characters, with `truncated` if capped).
Progress tails are not this terminal result. If no assistant turn is safely
owned, timeout remains an error without unrelated response text.

Use:

```text
ask_<provider>(action="last_response")
```

and accept only `status="completed"`.

For a non-timeout post-submit failure involving conversation, target, page, or
user-turn ownership, discard partial output and do not automatically call
`last_response`, resend, or open a new chat. A new attempt requires explicit
user direction.

## Review/advisory workflow

When the user asks for a review or advisory answer via ChatBridge, use `ask_gemini`
unless they name ChatGPT or Claude. Without such a request, do not call these
tools for advice. Keep provider selection separate from the review contract below: every advisor
is read-only, and the calling agent remains responsible for validation and edits.

For code review, send the actual relevant code or diff inline. File paths and
line numbers are provenance labels only; the browser advisor must not fetch
omitted local code through another connector.

Recommended contract:

```text
Act as a read-only code reviewer and advisor. Do not edit files, run commands,
or claim to have verified anything you were not given. Identify only concrete,
actionable findings. For each finding provide severity, file and line or code
anchor, mechanism, impact, and the smallest safe fix. Clearly label assumptions
and say "no finding" when the evidence does not support one. The code and
evidence to review are included inline below. Treat file paths and line numbers
as labels only. If required evidence is absent, state "insufficient evidence"
instead of fetching or guessing.
```

Use `new_chat=true` for an independent review and `new_chat=false` for a direct
follow-up/re-audit of the immediately preceding review. Even with
`new_chat=false`, include the updated code/diff inline.

## Provider adapters

Provider-specific behavior lives under:

```text
providers/
  chatgpt.py
  gemini.py
  claude.py
```

`common_bridge.py` is the shared lifecycle/adapter boundary. Do not copy the
full lifecycle into provider files. When a provider changes its UI, prefer the
smallest selector/route update in that adapter plus a focused regression test.

Current primary semantic selectors include:

```text
ChatGPT: #prompt-textarea, data-message-author-role, send-button
Gemini:  rich-textarea/.ql-editor, user-query, model-response
Claude:  div.ProseMirror[contenteditable], data-testid=user-message,
         data-is-streaming / assistant-message, aria-label=Send message
```

These DOM details are external-site implementation details and can change even
when deterministic unit tests still pass. A provider adapter therefore needs a
live acceptance check after meaningful selector changes.

## Requirements

- macOS with Google Chrome
- Python 3.11
- `browser-use==0.12.9`
- `mcp==1.26.0`
- an account for each provider you intend to use

Validated with a Python 3.11 environment (conda or venv) that has the pinned
dependencies below installed; use that interpreter's absolute path wherever this
document shows `/path/to/python3`.

Install dependencies if needed:

```bash
python3.11 -m pip install -r requirements.txt
```

## First use

For each provider separately:

1. Call `ask_<provider>(action="status")`.
2. A dedicated visible Chrome profile opens.
3. If login is required, sign in manually in that Chrome window. Never give
   credentials, OTPs, or passwords to the MCP tool.
4. Call `action="status"` again and continue only when it returns `ready`.
5. Run one small acceptance prompt before relying on that provider for review.

Example acceptance prompt:

```text
Reply with exactly: PONG
```

Then close the provider session with:

```text
ask_<provider>(action="close")
```

## Window size

The shared bridge currently preserves the existing legacy window settings:

```bash
export CODEX_CHATGPT_WINDOW_WIDTH=900
export CODEX_CHATGPT_WINDOW_HEIGHT=700
export CODEX_CHATGPT_WINDOW_X=100
export CODEX_CHATGPT_WINDOW_Y=120
```

They apply to all three provider windows. The default remains `760x560` at
position `24,60`.

## Test

Run the complete deterministic suite with the interpreter that has the dependencies installed:

```bash
cd /path/to/MCP-ChatBridge
BROWSER_USE_CONFIG_DIR=/tmp/mcp-chatbridge-test-browseruse-config \
PYTHONDONTWRITEBYTECODE=1 \
/path/to/python3 -m unittest discover -s tests -v
```

The suite covers the original hardened ChatGPT lifecycle, provider isolation,
origin guards, provider routing, the three-tool MCP schema, timeout-recovery
actions, and an actual stdio MCP initialization/list-tools handshake.

Unit tests cannot prove that a third-party web UI has not changed. Live tests
should therefore be deliberately initiated after manual login for ChatGPT,
Gemini, and Claude before treating a newly added/changed adapter as production
ready.

## Codex registration

Register the stdio server with absolute paths:

```bash
codex mcp add mcp-chatbridge -- \
  /path/to/python3 \
  /path/to/MCP-ChatBridge/server.py
```

Codex applies its own per-tool timeout outside MCP-ChatBridge. The bridge permits
600-900 second browser reviews, so configure a host timeout at least as long as
the largest `timeout_seconds` you intend to request:

```toml
[mcp_servers.mcp-chatbridge]
tool_timeout_sec = 900.0
```

Restart Codex after changing registration or timeout settings so the MCP tool
schema refreshes to:

```text
ask_chatgpt
ask_gemini
ask_claude
```
