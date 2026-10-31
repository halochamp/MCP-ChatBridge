"""Shared deterministic browser/session layer for MCP-ChatBridge providers.

The proven ChatGPT bridge remains the compatibility baseline.  This module
reuses its locking, ownership, cancellation, CDP binding, correlation and
cleanup machinery while moving provider-specific URL/DOM behavior behind a
small adapter configuration.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from chatgpt_web import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    MAX_PROGRESS_TAIL_CHARS,
    MAX_PROMPT_CHARS,
    MAX_RESPONSE_CHARS,
    PROGRESS_REPORT_TIMEOUT_SECONDS,
    REPOSITORY_ROOT,
    ChallengeDetectedError,
    ChatGPTWebBridge,
    ChatGPTWebError,
    CompletionHook,
    CookieSafeBrowserSession,
    LoginRequiredError,
    PromptHook,
    ReadinessTimeoutError,
    _canonical_profile_dir,
)
from image_store import (
    ALLOWED_IMAGE_HOST_ROOTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_LIMIT,
    MIN_IMAGE_EDGE,
    ImageStoreError,
    filter_candidates,
    full_resolution_url,
    resolve_save_dir,
    save_image,
    sniff_image,
    validate_attachments,
)


# Sign-in hosts a provider's login may pass through. They are AUTHENTICATION hosts only: a page may open
# there and the bridge reports a login gate, but they are never interactive roots, so the bridge never
# mutates them. Providers opt in explicitly; none of this widens where a prompt is typed.
GOOGLE_SIGNIN_HOSTS = ("accounts.google.com", "accounts.youtube.com")
APPLE_SIGNIN_HOSTS = ("appleid.apple.com",)
MICROSOFT_SIGNIN_HOSTS = ("login.microsoftonline.com", "login.live.com")

# After a stop control disappears the response is considered finished once nothing changed this long.
TRANSCRIPT_QUIET_AFTER_STOP_SECONDS = 3.0


@dataclass(frozen=True)
class ProviderConfig:
    key: str
    name: str
    home_url: str
    profile_env: str
    default_profile_dir: Path
    allowed_domains: tuple[str, ...]
    interactive_roots: tuple[str, ...]
    auth_roots: tuple[str, ...]
    composer_selectors: tuple[str, ...]
    assistant_selectors: tuple[str, ...]
    user_selectors: tuple[str, ...]
    user_content_selectors: tuple[str, ...]
    send_selectors: tuple[str, ...]
    stop_selectors: tuple[str, ...]
    login_selectors: tuple[str, ...]
    login_labels: tuple[str, ...]
    challenge_selectors: tuple[str, ...]
    challenge_titles: tuple[str, ...]
    conversation_path_prefixes: tuple[str, ...]
    assistant_content_selectors: tuple[str, ...] = ()
    provisional_path_prefixes: tuple[str, ...] = ()
    native_editor_input: bool = False
    submit_with_enter: bool = False
    # How long _submit waits for an observable acceptance signal. Fresh-chat routes can take
    # seconds to appear (Gemini measured ~9-11s) and the services are sometimes slow, so the user
    # set two minutes. A slow success is unaffected; only a failing submit waits this long, and it
    # still never resends.
    submit_timeout_seconds: float = 120.0
    # Icon-name (not localized label) hooks for optional features; empty means unsupported.
    # Empty trigger icons means the file input already exists on the page (no menu to open).
    attach_trigger_icons: tuple[str, ...] = ()
    attach_input_selectors: tuple[str, ...] = ()
    # URL schemes of the preview <img> that prove an attachment landed in the composer.
    attach_preview_src_schemes: tuple[str, ...] = ("blob", "data")
    # Some pages ignore a file set before their handlers hydrate (ChatGPT: 2 of 2 immediate tries
    # were silently dropped, a try after a 3s settle attached in 2s). Wait this long before the
    # first try, and re-issue the files (up to 3 tries) if nothing changed after the retry delay.
    attach_settle_seconds: float = 0.0
    attach_retry_seconds: float = 0.0
    download_button_icons: tuple[str, ...] = ()
    # CSS selectors for providers whose controls have no icon-font names (e.g. SVG buttons).
    download_button_selectors: tuple[str, ...] = ()
    # Ancestor of the assistant message that holds the whole turn (the image may sit outside the
    # text node). Empty means the assistant node itself is the turn.
    assistant_turn_selectors: tuple[str, ...] = ()
    # Hosts the page may be asked to fetch generated images from (plus page-local blob:/data:image).
    image_host_roots: tuple[str, ...] = ALLOWED_IMAGE_HOST_ROOTS
    # Page DOM drifts often (ChatGPT dropped data-message-author-role and <article>), so turn
    # selectors can silently count 0. Transcript mode never relies on them: it reads the whole
    # conversation text (noise included, the caller reads it) and watches generic signals instead:
    # text growth/quiet, a stop control when present, and images that were not there before submit.
    transcript_mode: bool = False
    transcript_root_selectors: tuple[str, ...] = ("main", "body")
    # Quiet time (no change in text/images) required to call a response finished when no stop
    # control was ever seen; with a stop control a short quiet period after it disappears is enough.
    transcript_stable_seconds: float = 8.0
    # An image request with no image and no stop control ever seen is reported as "no image" after
    # this much quiet (generation can be silent for a while, so keep it long).
    transcript_no_image_seconds: float = 45.0
    # A page whose path starts like this is a sign-in page whatever its DOM looks like (the
    # path survives redesigns; label/selector checks do not).
    login_path_prefixes: tuple[str, ...] = ("/login", "/signin", "/sign-in", "/auth/login")

    @property
    def last_response_tool(self) -> str:
        return f"{self.key}_last_response"


class CommonBrowserBridge(ChatGPTWebBridge):
    """Common lifecycle plus provider-specific deterministic DOM adapter.

    The parent class supplies the hardened cross-process profile ownership,
    session lifecycle, cancellation behavior, target binding and response
    correlation state machine.  This layer replaces all site-specific URL and
    DOM operations so ChatGPT, Gemini and Claude can share the same lifecycle.
    """

    CONFIG: ProviderConfig

    def __init__(
        self,
        *,
        profile_dir: Path | None = None,
        session_factory: Any = CookieSafeBrowserSession,
        sleep: Any = asyncio.sleep,
        poll_interval: float = 0.75,
    ) -> None:
        config = self.CONFIG
        configured_profile = os.environ.get(config.profile_env)
        configured_value = configured_profile if configured_profile else (profile_dir or config.default_profile_dir)
        self.profile_dir = _canonical_profile_dir(configured_value)
        self._session_factory = session_factory
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._session = None
        self._lock = asyncio.Lock()
        self._profile_lock_handle = None
        self._ownership_marker_path = None
        self._reconnect_blocked = False
        self._operation_target_id = None
        self._operation_page_identity = None
        self._operation_response_identity = None
        self._operation_user_count_before = None
        self._operation_allow_new_chat_transition = False
        self._operation_new_chat_transition_count = 0
        self._operation_correlation_warning = None
        self._operation_strict_user_turn_text = False
        self._operation_submit_accepted = False
        self._operation_assistant_count_before: int | None = None
        self._operation_submit_identity: tuple[str, str | None] | None = None
        self._operation_baseline_images: set[str] = set()
        self._operation_attached_count = 0

    @property
    def provider_name(self) -> str:
        return self.CONFIG.name

    def _adapt_text(self, value: str) -> str:
        if self.CONFIG.key == "chatgpt":
            return value
        return value.replace("ChatGPT", self.provider_name).replace("chatgpt_last_response", self.CONFIG.last_response_tool)

    @staticmethod
    def _host_matches(hostname: str, roots: tuple[str, ...]) -> bool:
        return any(hostname == root or hostname.endswith(f".{root}") for root in roots)

    @classmethod
    def _is_url_for_roots(cls, url: object, roots: tuple[str, ...]) -> bool:
        if not isinstance(url, str):
            return False
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() == "https"
            and parsed.username is None
            and parsed.password is None
            and port in (None, 443)
            and cls._host_matches(hostname, roots)
        )

    @classmethod
    def _is_interactive_chatgpt_url(cls, url: object) -> bool:
        return cls._is_url_for_roots(url, cls.CONFIG.interactive_roots)

    @classmethod
    def _is_openai_auth_url(cls, url: object) -> bool:
        return cls._is_url_for_roots(url, cls.CONFIG.auth_roots)

    @classmethod
    def _is_login_path_url(cls, url: object) -> bool:
        if not cls._is_interactive_chatgpt_url(url) or not isinstance(url, str):
            return False
        path = urlparse(url).path
        return any(path == prefix or path.startswith(prefix + "/") for prefix in cls.CONFIG.login_path_prefixes)

    @classmethod
    def _is_conversation_url(cls, url: object) -> bool:
        if not cls._is_interactive_chatgpt_url(url) or not isinstance(url, str):
            return False
        path = urlparse(url).path
        return any(path == prefix or path.startswith(prefix) for prefix in cls.CONFIG.conversation_path_prefixes)

    @classmethod
    def _is_provisional_conversation_url(cls, url: object) -> bool:
        if not cls._is_conversation_url(url) or not isinstance(url, str):
            return False
        path = urlparse(url).path
        return any(path.startswith(prefix) for prefix in cls.CONFIG.provisional_path_prefixes)

    @classmethod
    def _origin_guard_js(cls) -> str:
        roots = json.dumps(list(cls.CONFIG.interactive_roots))
        return f"""
const currentUrl = new URL(location.href);
const currentHost = currentUrl.hostname.toLowerCase().replace(/\\.$/, '');
const allowedRoots = {roots};
const approvedHost = allowedRoots.some((root) => currentHost === root || currentHost.endsWith('.' + root));
if (
  currentUrl.protocol !== 'https:' ||
  (currentUrl.port !== '' && currentUrl.port !== '443') ||
  currentUrl.username !== '' ||
  currentUrl.password !== '' ||
  !approvedHost
) return false;
"""

    @classmethod
    def _mutation_guard_js(cls, expected_identity: tuple[str, str | None] | None = None) -> str:
        guard = cls._origin_guard_js()
        if expected_identity is not None:
            expected_url, expected_document_token = expected_identity
            guard += f"if (location.href !== {json.dumps(expected_url)}) return false;\n"
            if expected_document_token is not None:
                guard += (
                    "if (String(performance.timeOrigin || '') !== "
                    f"{json.dumps(expected_document_token)}) return false;\n"
                )
        return guard

    async def _ensure_session(self):
        if self._reconnect_blocked:
            raise ChatGPTWebError(
                f"The previous {self.provider_name} browser session could not be shut down safely. "
                "Restart the MCP server before retrying."
            )
        if self._session is not None:
            return self._session

        window_size, window_position = self._window_options()
        self._acquire_profile_lock()
        try:
            self._assert_no_stale_ownership_marker()
            self._create_ownership_marker()
        except Exception:
            self._release_profile_lock()
            raise

        kwargs: dict[str, Any] = {
            "headless": False,
            "user_data_dir": self.profile_dir,
            "storage_state": None,
            "enable_default_extensions": False,
            "window_size": window_size,
            "window_position": window_position,
            "allowed_domains": list(self.CONFIG.allowed_domains),
            "keep_alive": False,
            "minimum_wait_page_load_time": 0.5,
            "wait_for_network_idle_page_load_time": 1.0,
        }

        partial_session = None
        try:
            partial_session = self._session_factory(**kwargs)
            self._session = partial_session
            self._reconnect_blocked = True
            await partial_session.start()
        except asyncio.CancelledError:
            self._reconnect_blocked = True
            if partial_session is not None:
                self._session = partial_session
            raise
        except Exception as exc:
            if partial_session is None:
                self._finalize_confirmed_shutdown(None)
            else:
                try:
                    await partial_session.kill()
                except asyncio.CancelledError:
                    self._reconnect_blocked = True
                    self._session = partial_session
                    raise
                except Exception:
                    self._reconnect_blocked = True
                    self._session = partial_session
                else:
                    self._finalize_confirmed_shutdown(partial_session)
            raise ChatGPTWebError(
                f"Could not start the dedicated {self.provider_name} browser session ({type(exc).__name__})."
            ) from exc
        self._reconnect_blocked = False
        return partial_session

    async def _navigate(self, url: str | None = None) -> None:
        destination = url or self.CONFIG.home_url
        if not self._is_interactive_chatgpt_url(destination):
            raise ChatGPTWebError(f"Navigation outside the approved {self.provider_name} domain is not allowed.")
        session = await self._ensure_session()
        try:
            cdp_session = await self._get_bound_cdp_session(session)
            if not cdp_session:
                await self._invalidate_session(session)
                raise ChatGPTWebError(f"No active {self.provider_name} browser tab is available.")
            result = await cdp_session.cdp_client.send.Page.navigate(
                params={"url": destination},
                session_id=cdp_session.session_id,
            )
            if not isinstance(result, dict) or result.get("errorText"):
                raise RuntimeError("Page.navigate failed.")
        except asyncio.CancelledError:
            self._reconnect_blocked = True
            raise
        except ChatGPTWebError:
            raise
        except Exception as exc:
            await self._invalidate_session(session)
            raise ChatGPTWebError(f"The browser could not navigate to {self.provider_name}.") from exc
        await self._wait_for_approved_navigation()

    async def _wait_for_approved_navigation(self, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            state = await self._page_state()
            url = state.get("url")
            if self._is_interactive_chatgpt_url(url) or self._is_openai_auth_url(url):
                return
            if time.monotonic() >= deadline:
                raise ChatGPTWebError(f"The browser did not navigate to an approved {self.provider_name} page.")
            await self._sleep(max(self._poll_interval, 0.05))

    @classmethod
    def _raise_for_page_state(cls, state: dict[str, Any]) -> None:
        provider = cls.CONFIG.name
        if state.get("challenge"):
            raise ChallengeDetectedError(
                f"{provider} displayed a human-verification challenge. Complete it manually in the opened browser, then retry."
            )
        url = state.get("url")
        if cls._is_openai_auth_url(url) or cls._is_login_path_url(url):
            raise LoginRequiredError(
                f"{provider} login is required. Sign in manually in the opened dedicated Chrome window, then retry."
            )
        if state.get("login_required") and cls._is_interactive_chatgpt_url(url):
            raise LoginRequiredError(
                f"{provider} login is required. Sign in manually in the opened dedicated Chrome window, then retry."
            )
        if not cls._is_interactive_chatgpt_url(url):
            raise ChatGPTWebError(f"The active browser page is outside the approved {provider} domain.")

    async def _assert_interactive_page(
        self, *, expected_identity: tuple[str, str | None] | None = None
    ) -> dict[str, Any]:
        state = await self._page_state()
        self._raise_for_page_state(state)
        if expected_identity is not None and self._page_identity(state) != expected_identity:
            raise ChatGPTWebError(f"The {self.provider_name} page changed during the operation.")
        return state

    @staticmethod
    def _selector_query_js(selectors: tuple[str, ...]) -> str:
        return json.dumps(list(selectors))

    async def _send_cdp_command(self, method: str, params: dict[str, Any]) -> Any:
        """Send the small allowlisted set of native-input CDP commands we use.

        ``cdp-use`` exposes commands through a typed ``send.<Domain>.<command>``
        library.  ``cdp_client.send`` itself is not callable.  Keep this helper
        explicit so an API-shape mistake cannot be misreported as a transport
        failure and cannot open an arbitrary raw-CDP surface.
        """
        session = await self._ensure_session()
        try:
            cdp_session = await self._get_bound_cdp_session(session)
            if not cdp_session:
                await self._invalidate_session(session)
                raise ChatGPTWebError(f"No active {self.provider_name} browser tab is available.")
            if method == "Input.dispatchKeyEvent":
                return await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                    params=params,
                    session_id=cdp_session.session_id,
                )
            if method == "Input.insertText":
                return await cdp_session.cdp_client.send.Input.insertText(
                    params=params,
                    session_id=cdp_session.session_id,
                )
            if method == "DOM.getDocument":
                return await cdp_session.cdp_client.send.DOM.getDocument(
                    params=params,
                    session_id=cdp_session.session_id,
                )
            if method == "DOM.querySelector":
                return await cdp_session.cdp_client.send.DOM.querySelector(
                    params=params,
                    session_id=cdp_session.session_id,
                )
            if method == "DOM.setFileInputFiles":
                return await cdp_session.cdp_client.send.DOM.setFileInputFiles(
                    params=params,
                    session_id=cdp_session.session_id,
                )
            if method == "Browser.setDownloadBehavior":
                # Browser-level command: it carries no page session id.
                return await cdp_session.cdp_client.send.Browser.setDownloadBehavior(params=params)
            raise ChatGPTWebError(f"Unsupported native CDP command: {method}")
        except asyncio.CancelledError:
            self._reconnect_blocked = True
            raise
        except ChatGPTWebError:
            raise
        except TypeError as exc:
            # A typed-CDP invocation mismatch is a local programming error, not
            # evidence that the browser transport died. Preserve the live
            # session and classify it accurately so diagnostics remain useful.
            raise ChatGPTWebError(
                f"The {self.provider_name} native CDP input command was invoked incorrectly."
            ) from exc
        except Exception as exc:
            await self._invalidate_session(session)
            raise ChatGPTWebError(
                f"The browser connection was lost while controlling {self.provider_name}."
            ) from exc

    async def _dispatch_native_key(
        self,
        *,
        key: str,
        code: str,
        virtual_key: int,
        modifiers: int = 0,
        text: str | None = None,
    ) -> None:
        key_down: dict[str, Any] = {
            "type": "keyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": virtual_key,
            "nativeVirtualKeyCode": virtual_key,
            "modifiers": modifiers,
        }
        if text is not None:
            key_down["text"] = text
            key_down["unmodifiedText"] = text
        await self._send_cdp_command("Input.dispatchKeyEvent", key_down)
        await self._send_cdp_command(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": virtual_key,
                "nativeVirtualKeyCode": virtual_key,
                "modifiers": modifiers,
            },
        )

    async def _page_state(self) -> dict[str, Any]:
        config = self.CONFIG
        state = await self._evaluate(
            f"""
            (() => {{
              const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const first = (selectors, root=document) => {{
                for (const selector of selectors) {{
                  const node = root.querySelector(selector);
                  if (node) return node;
                }}
                return null;
              }};
              const firstAll = (selectors, root=document) => {{
                for (const selector of selectors) {{
                  const nodes = Array.from(root.querySelectorAll(selector));
                  if (nodes.length) return nodes;
                }}
                return [];
              }};
              const uniqueAll = (selectors) => {{
                const seen = new Set();
                const nodes = [];
                for (const selector of selectors) {{
                  for (const node of document.querySelectorAll(selector)) {{
                    if (!seen.has(node)) {{ seen.add(node); nodes.push(node); }}
                  }}
                }}
                return nodes;
              }};

              const composer = first({self._selector_query_js(config.composer_selectors)});
              const assistant = firstAll({self._selector_query_js(config.assistant_selectors)});
              const latestAssistant = assistant.length ? assistant[assistant.length - 1] : null;
              const latestAssistantContent = latestAssistant
                ? first({self._selector_query_js(config.assistant_content_selectors)}, latestAssistant)
                : null;
              const latestAssistantTextNode = latestAssistantContent || latestAssistant;
              const latest = latestAssistantTextNode
                ? (latestAssistantTextNode.innerText || latestAssistantTextNode.textContent || '').trim()
                : '';
              // Count every loaded, content-sized image in the latest assistant turn; the
              // Python side filters further, so UI component renames do not matter here.
              let latestTurn = latestAssistant;
              if (latestAssistant) {{
                for (const turnSelector of {self._selector_query_js(config.assistant_turn_selectors)}) {{
                  const turn = latestAssistant.closest(turnSelector);
                  if (turn) {{ latestTurn = turn; break; }}
                }}
              }}
              const latestImages = latestTurn
                ? Array.from(latestTurn.querySelectorAll('img')).filter((img) =>
                    visible(img) && Math.min(img.naturalWidth || 0, img.naturalHeight || 0) >= {MIN_IMAGE_EDGE})
                : [];
              // Transcript mode: whole-conversation text length/tail (composer excluded, so typing and
              // submitting do not move it) and every big image outside the composer form.
              const transcriptOn = {json.dumps(bool(config.transcript_mode))};
              const transcriptRoot = transcriptOn
                ? (first({self._selector_query_js(config.transcript_root_selectors)}) || document.body)
                : null;
              let pageText = '';
              if (transcriptRoot) {{
                const copy = transcriptRoot.cloneNode(true);
                copy.querySelectorAll('form, textarea, [contenteditable="true"], script, style').forEach((n) => n.remove());
                pageText = (copy.innerText || copy.textContent || '').trim();
              }}
              const pageImages = transcriptRoot
                ? Array.from(transcriptRoot.querySelectorAll('img'))
                    .filter((img) => !img.closest('form') && visible(img) &&
                      Math.min(img.naturalWidth || 0, img.naturalHeight || 0) >= {MIN_IMAGE_EDGE})
                    .slice(0, 40)
                    .map((img) => ({{src: img.currentSrc || img.src || '', w: img.naturalWidth || 0, h: img.naturalHeight || 0}}))
                : [];
              const user = firstAll({self._selector_query_js(config.user_selectors)});
              const latestUserNode = user.length ? user[user.length - 1] : null;
              const latestUserContentNodes = latestUserNode
                ? firstAll({self._selector_query_js(config.user_content_selectors)}, latestUserNode)
                : [];
              const latestUser = latestUserNode
                ? (latestUserContentNodes.length
                    ? latestUserContentNodes.map((node) => (node.innerText || node.textContent || '')).join('\\n')
                    : (latestUserNode.innerText || latestUserNode.textContent || '')).trim()
                : '';
              const stop = uniqueAll({self._selector_query_js(config.stop_selectors)}).some((node) => visible(node));
              const challengeNode = first({self._selector_query_js(config.challenge_selectors)});
              const challengeTitles = {json.dumps([item.lower() for item in config.challenge_titles])};
              const challenge = visible(challengeNode) || (!visible(composer) && challengeTitles.includes(document.title.trim().toLowerCase()));

              const currentUrl = new URL(location.href);
              const currentHost = currentUrl.hostname.toLowerCase().replace(/\\.$/, '');
              const authRoots = {json.dumps(list(config.auth_roots))};
              const isAuthHost = currentUrl.protocol === 'https:' && (currentUrl.port === '' || currentUrl.port === '443') &&
                currentUrl.username === '' && currentUrl.password === '' &&
                authRoots.some((root) => currentHost === root || currentHost.endsWith('.' + root));
              const loginLabels = {json.dumps([item.lower() for item in config.login_labels])};
              const loginControls = uniqueAll({self._selector_query_js(config.login_selectors)}).filter((node) => visible(node));
              const loginLink = isAuthHost || loginControls.some((node) => {{
                const text = (node.innerText || node.textContent || '').trim().toLowerCase();
                const label = (node.getAttribute('aria-label') || '').trim().toLowerCase();
                return loginLabels.includes(text) || loginLabels.includes(label);
              }});

              return {{
                url: location.href,
                title: document.title,
                document_token: String(performance.timeOrigin || ''),
                composer_ready: visible(composer),
                assistant_count: assistant.length,
                latest_text: latest,
                latest_image_count: latestImages.length,
                page_text_len: pageText.length,
                page_text_tail: pageText.slice(-80),
                page_images: pageImages,
                user_count: user.length,
                latest_user_text: latestUser,
                streaming: stop,
                challenge,
                login_required: loginLink
              }};
            }})()
            """
        )
        if not isinstance(state, dict):
            raise ChatGPTWebError(f"Could not inspect the {self.provider_name} page.")
        return state

    @classmethod
    def _readiness_status(cls, state: dict[str, Any]) -> str:
        url = state.get("url")
        if state.get("challenge"):
            return "challenge"
        if (
            cls._is_openai_auth_url(url)
            or cls._is_login_path_url(url)
            or (state.get("login_required") and cls._is_interactive_chatgpt_url(url))
        ):
            return "login_required"
        if not cls._is_interactive_chatgpt_url(url):
            raise ChatGPTWebError(f"The active browser page is outside the approved {cls.CONFIG.name} domain.")
        return "ready" if state.get("composer_ready") else "loading"

    async def _wait_for_readiness(
        self,
        timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        *,
        deadline: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
        last_state: dict[str, Any] = {}
        while True:
            last_state = await self._page_state()
            status = self._readiness_status(last_state)
            if status != "loading":
                return status, last_state
            if time.monotonic() >= deadline:
                raise ReadinessTimeoutError(
                    f"{self.provider_name} did not become ready within {timeout_seconds:g} seconds "
                    f"(page: {last_state.get('title', 'unknown')})."
                )
            await self._sleep(max(self._poll_interval, 0.05))

    async def _wait_for_composer(
        self,
        timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        status, state = await self._wait_for_readiness(timeout_seconds, deadline=deadline)
        if status == "ready":
            return state
        self._raise_for_page_state(state)
        raise ChatGPTWebError(f"{self.provider_name} did not become interactively ready.")

    async def _focus_and_clear_composer(self) -> None:
        expected_identity = self._operation_page_identity
        await self._assert_interactive_page(expected_identity=expected_identity)
        selectors = self._selector_query_js(self.CONFIG.composer_selectors)

        if self.CONFIG.native_editor_input:
            focused = await self._evaluate(
                f"""
                (() => {{
                  {self._mutation_guard_js(expected_identity)}
                  const selectors = {selectors};
                  const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
                  const visible = (node) => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                  if (!visible(el)) return false;
                  el.focus();
                  return document.activeElement === el || el.contains(document.activeElement);
                }})()
                """
            )
            if not focused:
                raise ChatGPTWebError(f"Could not focus the {self.provider_name} prompt composer.")
            # Gemini (Quill) and Claude (ProseMirror) maintain editor state outside
            # the DOM. Mutating their DOM with execCommand can leave visible text
            # out of sync with the state used by Send. Use native browser input
            # events so the editor receives the same beforeinput/input path as a
            # real keyboard action. MCP-ChatBridge currently targets macOS, where
            # Meta+A is the native Select All chord.
            await self._dispatch_native_key(key="a", code="KeyA", virtual_key=65, modifiers=4)
            await self._dispatch_native_key(key="Backspace", code="Backspace", virtual_key=8)
            return

        focused = await self._evaluate(
            f"""
            (() => {{
              {self._mutation_guard_js(expected_identity)}
              const selectors = {selectors};
              const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
              if (!el) return false;
              el.focus();
              if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {{
                el.select();
                const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                if (!setter) return false;
                setter.call(el, '');
                el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'deleteContentBackward'}}));
              }} else {{
                const selection = window.getSelection();
                if (!selection) return false;
                const range = document.createRange();
                range.selectNodeContents(el);
                selection.removeAllRanges();
                selection.addRange(range);
                document.execCommand('delete');
                el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'deleteContentBackward'}}));
              }}
              return true;
            }})()
            """
        )
        if not focused:
            raise ChatGPTWebError(f"Could not focus the {self.provider_name} prompt composer.")

    async def _insert_text(self, text: str) -> None:
        await self._assert_interactive_page(expected_identity=self._operation_page_identity)
        selectors = self._selector_query_js(self.CONFIG.composer_selectors)

        if self.CONFIG.native_editor_input:
            focused = await self._evaluate(
                f"""
                (() => {{
                  {self._mutation_guard_js(self._operation_page_identity)}
                  const selectors = {selectors};
                  const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
                  const visible = (node) => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                  if (!visible(el)) return false;
                  el.focus();
                  return document.activeElement === el || el.contains(document.activeElement);
                }})()
                """
            )
            if not focused:
                raise ChatGPTWebError(f"Could not focus the {self.provider_name} prompt composer.")
            await self._send_cdp_command("Input.insertText", {"text": text})
            return

        inserted = await self._evaluate(
            f"""
            (() => {{
              {self._mutation_guard_js(self._operation_page_identity)}
              const value = {json.dumps(text)};
              const selectors = {selectors};
              const el = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
              const visible = (node) => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
              if (!visible(el)) return false;
              el.focus();
              if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {{
                const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                if (!setter) return false;
                setter.call(el, value);
                el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: value}}));
                return true;
              }}
              const selection = window.getSelection();
              if (!selection) return false;
              const range = document.createRange();
              range.selectNodeContents(el);
              selection.removeAllRanges();
              selection.addRange(range);
              const ok = document.execCommand('insertText', false, value);
              el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: value}}));
              return ok;
            }})()
            """
        )
        if not inserted:
            raise ChatGPTWebError(f"Could not insert text into the {self.provider_name} prompt composer.")

    def _submitted_user_turn_is_visible(self, state: dict[str, Any], prompt: str) -> bool:
        """Correlate response ownership from lifecycle signals, not rendered text.

        Provider UIs are free to reformat Markdown, whitespace, accessibility
        labels, or rich-editor blocks.  The bridge therefore treats rendered user
        text as presentation only.  Ownership comes from the serialized operation,
        pinned page identity, an observed submit-acceptance signal, and turn-count
        monotonicity when the provider exposes a usable user-turn node.
        """
        if not self._operation_submit_accepted:
            return False

        before_count = self._operation_user_count_before
        if before_count is None:
            raise ChatGPTWebError(f"The {self.provider_name} request has no bound user-turn baseline.")
        try:
            current_count = int(state.get("user_count", -1))
        except (TypeError, ValueError) as exc:
            raise ChatGPTWebError(f"The {self.provider_name} page returned an invalid user-turn count.") from exc
        if current_count < 0:
            raise ChatGPTWebError(f"The {self.provider_name} page returned an invalid user-turn count.")

        expected_count = before_count + 1
        if current_count > expected_count:
            raise ChatGPTWebError(
                f"The {self.provider_name} user turn changed during response collection "
                f"(reason=count, expected={expected_count}, observed={current_count})."
            )
        if current_count == expected_count:
            return True

        # If a future UI stops exposing the user-turn node, do not make rendered
        # text selectors a single point of failure.  A response that has started
        # on the same pinned operation is sufficient once submit acceptance was
        # already observed by _submit().
        try:
            assistant_count = int(state.get("assistant_count", -1))
        except (TypeError, ValueError):
            assistant_count = -1
        assistant_before = self._operation_assistant_count_before
        if assistant_before is not None and assistant_count > assistant_before:
            return True
        if bool(state.get("streaming")):
            return True
        return False

    async def _response_page_state(self, prompt: str) -> dict[str, Any] | None:
        # If submit acceptance bound a fresh conversation route, pin response
        # collection to that exact route before delegating to the parent state
        # machine.  Only an explicitly provisional route may transition once more
        # to its provider-defined final conversation URL.
        submit_identity = self._operation_submit_identity
        if (
            submit_identity is not None
            and self._operation_response_identity is not None
            and self._operation_response_identity != submit_identity
        ):
            self._operation_response_identity = submit_identity
            self._operation_new_chat_transition_count = 1
            self._operation_allow_new_chat_transition = self._is_provisional_conversation_url(
                submit_identity[0]
            )
        return await super()._response_page_state(prompt)

    async def _submit(self, prompt: str, timeout_seconds: float | None = None) -> None:
        """Submit from observable lifecycle signals rather than rendered text equality."""
        if timeout_seconds is None:
            timeout_seconds = self.CONFIG.submit_timeout_seconds
        self._operation_submit_accepted = False
        self._operation_assistant_count_before = None
        self._operation_submit_identity = None
        expected_identity = self._operation_page_identity
        deadline = time.monotonic() + timeout_seconds
        composer_selectors = self._selector_query_js(self.CONFIG.composer_selectors)
        send_selectors = self._selector_query_js(self.CONFIG.send_selectors)
        requires_route_bind = bool(
            expected_identity is not None and not self._is_conversation_url(expected_identity[0])
        )
        candidate_identity: tuple[str, str | None] | None = None
        candidate_user_count = -1
        candidate_assistant_count = -1
        candidate_streaming = False
        candidate_latest_text = ""
        candidate_text_len = -1
        transcript = self.CONFIG.transcript_mode

        async def wait_for_acceptance(
            before_user_count: int,
            before_assistant_count: int,
            *,
            max_wait: float,
            before_text_len: int = -1,
        ) -> bool:
            nonlocal candidate_identity, candidate_user_count, candidate_assistant_count
            nonlocal candidate_streaming, candidate_latest_text, candidate_text_len
            acceptance_deadline = min(deadline, time.monotonic() + max_wait)
            while time.monotonic() < acceptance_deadline:
                await self._sleep(0.15)
                state = await self._page_state()
                self._raise_for_page_state(state)
                current_identity = self._page_identity(state)
                if current_identity is None:
                    raise ChatGPTWebError(f"The {self.provider_name} page has no stable identity during submit.")
                try:
                    user_count = int(state.get("user_count", -1))
                    assistant_count = int(state.get("assistant_count", -1))
                except (TypeError, ValueError):
                    user_count = -1
                    assistant_count = -1
                streaming = bool(state.get("streaming"))
                latest_text = str(state.get("latest_text", ""))
                try:
                    text_len = int(state.get("page_text_len", -1)) if transcript else -1
                except (TypeError, ValueError):
                    text_len = -1

                if requires_route_bind:
                    # A fresh-chat route transition is not, by itself, proof that
                    # our submit caused it.  First baseline the new conversation,
                    # then require a later lifecycle mutation on that exact route.
                    if current_identity == expected_identity:
                        continue
                    if not self._is_conversation_url(current_identity[0]):
                        raise ChatGPTWebError(
                            f"The {self.provider_name} conversation changed to an unexpected route during submit."
                        )
                    if candidate_identity is None:
                        candidate_identity = current_identity
                        candidate_user_count = user_count
                        candidate_assistant_count = assistant_count
                        candidate_streaming = streaming
                        candidate_latest_text = latest_text
                        candidate_text_len = text_len
                        continue
                    if current_identity != candidate_identity:
                        if self._is_provisional_conversation_url(candidate_identity[0]):
                            candidate_identity = current_identity
                            candidate_user_count = user_count
                            candidate_assistant_count = assistant_count
                            candidate_streaming = streaming
                            candidate_latest_text = latest_text
                            candidate_text_len = text_len
                            continue
                        raise ChatGPTWebError(
                            f"The {self.provider_name} conversation changed more than once during submit."
                        )
                    lifecycle_changed = (
                        user_count != candidate_user_count
                        or assistant_count != candidate_assistant_count
                        or streaming != candidate_streaming
                        or latest_text != candidate_latest_text
                        or (transcript and text_len != candidate_text_len)
                    )
                    if not lifecycle_changed:
                        continue
                else:
                    if current_identity != expected_identity:
                        raise ChatGPTWebError(
                            f"The {self.provider_name} conversation changed during submit."
                        )
                    lifecycle_changed = (
                        (before_user_count >= 0 and user_count > before_user_count)
                        or (before_assistant_count >= 0 and assistant_count > before_assistant_count)
                        or streaming
                        or (transcript and before_text_len >= 0 and text_len != before_text_len)
                    )
                    if not lifecycle_changed:
                        continue

                self._operation_submit_accepted = True
                self._operation_assistant_count_before = before_assistant_count
                self._operation_submit_identity = current_identity
                return True
            return False

        while time.monotonic() < deadline:
            before = await self._assert_interactive_page(expected_identity=expected_identity)
            try:
                before_user_count = int(before.get("user_count", -1))
                before_assistant_count = int(before.get("assistant_count", -1))
            except (TypeError, ValueError):
                before_user_count = -1
                before_assistant_count = -1
            try:
                before_text_len = int(before.get("page_text_len", -1)) if transcript else -1
            except (TypeError, ValueError):
                before_text_len = -1

            # Keep the pre-submit check deliberately structural. Rich editors are
            # allowed to rewrite whitespace, Markdown blocks, accessibility text,
            # or line structure without making the operation invalid.
            preflight = await self._evaluate(
                f"""
                (() => {{
                  {self._mutation_guard_js(expected_identity)}
                  const composerSelectors = {composer_selectors};
                  const composer = composerSelectors.map((selector) => document.querySelector(selector)).find(Boolean);
                  const visible = (node) => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                  if (!visible(composer)) return {{ok: false, reason: 'composer_missing'}};
                  const raw = composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement
                    ? composer.value
                    : (composer.textContent || composer.innerText || '');
                  return {{ok: String(raw || '').trim().length > 0, reason: 'composer_empty'}};
                }})()
                """
            )
            if not isinstance(preflight, dict) or not preflight.get("ok"):
                await self._sleep(0.2)
                continue

            if self.CONFIG.native_editor_input and self.CONFIG.submit_with_enter:
                await self._dispatch_native_key(
                    key="Enter",
                    code="Enter",
                    virtual_key=13,
                    text="\r",
                )
                # Enter is the single submit action for native rich editors.  A
                # delayed provider acknowledgement is ambiguous: clicking Send
                # after a fixed grace period can duplicate a turn that Enter
                # already accepted.  Wait through the remaining submit deadline
                # and fail safely rather than issuing a second submit action.
                remaining = max(0.1, deadline - time.monotonic())
                if await wait_for_acceptance(
                    before_user_count, before_assistant_count, max_wait=remaining, before_text_len=before_text_len
                ):
                    return
                raise ChatGPTWebError(
                    f"The {self.provider_name} submit action produced no observable lifecycle acceptance signal."
                )

            clicked = await self._evaluate(
                f"""
                (() => {{
                  {self._mutation_guard_js(expected_identity)}
                  const sendSelectors = {send_selectors};
                  const visible = (node) => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                  const button = sendSelectors.map((selector) => document.querySelector(selector)).find(
                    (node) => visible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true'
                  );
                  if (!button) return false;
                  button.click();
                  return true;
                }})()
                """
            )
            if clicked:
                remaining = max(0.1, deadline - time.monotonic())
                if await wait_for_acceptance(
                    before_user_count, before_assistant_count, max_wait=remaining, before_text_len=before_text_len
                ):
                    return
                raise ChatGPTWebError(
                    f"The {self.provider_name} submit action produced no observable lifecycle acceptance signal."
                )
            await self._sleep(0.2)

        raise ChatGPTWebError(f"The {self.provider_name} send control did not become available.")
    async def status(self, timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS) -> dict[str, Any]:
        try:
            result = await super().status(timeout_seconds)
        except ChatGPTWebError as exc:
            raise type(exc)(self._adapt_text(str(exc))) from exc
        result["provider"] = self.CONFIG.key
        return result

    async def new_chat(self) -> dict[str, Any]:
        try:
            result = await super().new_chat()
        except ChatGPTWebError as exc:
            raise type(exc)(self._adapt_text(str(exc))) from exc
        result["provider"] = self.CONFIG.key
        return result

    async def last_response(self) -> dict[str, Any]:
        try:
            if self.CONFIG.transcript_mode:
                result = await self._last_transcript_response()
            else:
                result = await super().last_response()
        except ChatGPTWebError as exc:
            raise type(exc)(self._adapt_text(str(exc))) from exc
        if isinstance(result.get("message"), str):
            result["message"] = self._adapt_text(result["message"])
        result["provider"] = self.CONFIG.key
        return result

    async def ask(
        self,
        prompt: str,
        *,
        new_chat: bool = True,
        timeout_seconds: int = 600,
        progress: Any = None,
        on_completed: CompletionHook | None = None,
        attach_images: list[str] | None = None,
    ) -> dict[str, Any]:
        async def adapted_progress(percent: float, message: str) -> None:
            if progress is not None:
                await progress(percent, self._adapt_text(message))

        before_prompt: PromptHook | None = None
        if attach_images:
            # Validate before any browser work so a bad path never touches the page.
            try:
                attachment_paths = validate_attachments(attach_images)
            except ImageStoreError as exc:
                raise ChatGPTWebError(str(exc)) from exc
            if not self.CONFIG.attach_input_selectors:
                raise ChatGPTWebError(f"{self.provider_name} image attachments are not supported.")

            async def before_prompt() -> None:  # noqa: F811 - closure bound to the validated paths
                await self._attach_images(attachment_paths)

        try:
            if self.CONFIG.transcript_mode:
                result = await self._ask_transcript(
                    prompt,
                    new_chat=new_chat,
                    timeout_seconds=timeout_seconds,
                    progress=adapted_progress if progress is not None else None,
                    on_completed=on_completed,
                    before_prompt=before_prompt,
                )
            else:
                result = await super().ask(
                    prompt,
                    new_chat=new_chat,
                    timeout_seconds=timeout_seconds,
                    progress=adapted_progress if progress is not None else None,
                    on_completed=on_completed,
                    before_prompt=before_prompt,
                )
        except ChatGPTWebError as exc:
            raise type(exc)(self._adapt_text(str(exc))) from exc
        if isinstance(result.get("message"), str):
            result["message"] = self._adapt_text(result["message"])
        if isinstance(result.get("correlation_warning"), str):
            result["correlation_warning"] = self._adapt_text(result["correlation_warning"])
        result["provider"] = self.CONFIG.key
        return result

    @staticmethod
    def _icon_js() -> str:
        """JS helpers that identify controls by icon name, which is stable across UI languages."""
        return """
          const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const iconName = (el) => {
            const icon = el.querySelector('mat-icon, .mat-icon, [fonticon], [data-mat-icon-name]');
            if (!icon) return null;
            return icon.getAttribute('fonticon') || icon.getAttribute('data-mat-icon-name')
              || (icon.textContent || '').trim().slice(0, 32) || null;
          };
        """

    def _composer_lookup_js(self) -> str:
        return f"""
          let composer = null;
          for (const selector of {self._selector_query_js(self.CONFIG.composer_selectors)}) {{
            composer = document.querySelector(selector);
            if (composer) break;
          }}
        """

    def _attachment_state_js(self) -> str:
        """Count attached previews (blob:/data: images near the composer) and busy indicators."""
        return f"""
        (() => {{
          {self._icon_js()}
          {self._composer_lookup_js()}
          let scope = composer;
          for (let i = 0; i < 8 && scope && scope.parentElement; i++) scope = scope.parentElement;
          scope = scope || document;
          const schemes = {json.dumps(list(self.CONFIG.attach_preview_src_schemes))};
          const previews = Array.from(scope.querySelectorAll('img')).filter((img) =>
            schemes.includes((img.currentSrc || img.src || '').split(':')[0]) && (img.naturalWidth || 0) > 0).length;
          const busy = scope.querySelectorAll(
            'mat-progress-spinner, mat-spinner, mat-progress-bar, [role="progressbar"]').length;
          return {{previews, busy}};
        }})()
        """

    def _attach_open_js(self) -> str:
        """Open the attach menu: nearest control to the composer whose icon name matches."""
        return f"""
        (() => {{
          {self._mutation_guard_js(self._operation_page_identity)}
          {self._icon_js()}
          {self._composer_lookup_js()}
          const icons = {json.dumps(list(self.CONFIG.attach_trigger_icons))};
          let node = composer;
          let button = null;
          for (let depth = 0; node && !button && depth < 12; depth++) {{
            button = Array.from(node.querySelectorAll('button')).filter(visible)
              .find((candidate) => icons.includes(iconName(candidate)));
            node = node.parentElement;
          }}
          if (!button) return {{ok: false, reason: 'attach_control_missing'}};
          button.click();
          return {{ok: true}};
        }})()
        """

    async def _find_attach_input(self) -> int:
        """Return the DOM node id of the file input, polling briefly; 0 when it never appears."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            document = await self._send_cdp_command("DOM.getDocument", {"depth": 0})
            root_id = int(document["root"]["nodeId"])
            for selector in self.CONFIG.attach_input_selectors:
                found = await self._send_cdp_command("DOM.querySelector", {"nodeId": root_id, "selector": selector})
                node_id = int((found or {}).get("nodeId", 0) or 0)
                if node_id:
                    return node_id
            await self._sleep(0.25)
        return 0

    async def _attach_images(self, paths: list[str]) -> None:
        """Attach local images through the page's own file input (CDP DOM.setFileInputFiles)."""
        config = self.CONFIG
        await self._assert_interactive_page(expected_identity=self._operation_page_identity)
        baseline = await self._evaluate(self._attachment_state_js())
        baseline_previews = int(baseline.get("previews", 0)) if isinstance(baseline, dict) else 0
        if config.attach_trigger_icons:
            opened = await self._evaluate(self._attach_open_js())
            if not (isinstance(opened, dict) and opened.get("ok")):
                raise ChatGPTWebError(f"Could not find the {self.provider_name} attachment control.")
        if config.attach_settle_seconds > 0:
            await self._sleep(config.attach_settle_seconds)

        async def issue_files() -> None:
            node_id = await self._find_attach_input()
            if not node_id:
                raise ChatGPTWebError(
                    f"The {self.provider_name} file input did not appear after opening the attach menu."
                )
            await self._send_cdp_command("DOM.setFileInputFiles", {"files": paths, "nodeId": node_id})

        await issue_files()
        issued_at = time.monotonic()
        tries = 1
        max_tries = 3 if config.attach_retry_seconds > 0 else 1
        expected = baseline_previews + len(paths)
        deadline = time.monotonic() + 60.0
        settled = 0
        while time.monotonic() < deadline:
            await self._sleep(0.5)
            state = await self._evaluate(self._attachment_state_js())
            previews = int(state.get("previews", 0)) if isinstance(state, dict) else 0
            busy = int(state.get("busy", 1)) if isinstance(state, dict) else 1
            ready = previews >= expected and busy == 0
            settled = settled + 1 if ready else 0
            if settled >= 2:
                # The user's own images now sit in the transcript ahead of any generated image.
                self._operation_attached_count = len(paths)
                with suppress(ChatGPTWebError):
                    # Close the attach menu if it is still open; harmless otherwise.
                    await self._dispatch_native_key(key="Escape", code="Escape", virtual_key=27)
                return
            progressing = previews > baseline_previews or busy > 0
            if not progressing and tries < max_tries and time.monotonic() - issued_at >= config.attach_retry_seconds:
                # Nothing changed: the page most likely ignored the first set. Issue the files again.
                await issue_files()
                issued_at = time.monotonic()
                tries += 1
        raise ChatGPTWebError(
            f"The {self.provider_name} attachments did not finish uploading within 60 seconds; nothing was sent."
        )

    def _download_click_js(self, src: str, identity: tuple[str, str | None] | None) -> str:
        """Click the page's own download control for exactly one image (ancestor holds one big image)."""
        config = self.CONFIG
        return f"""
        (() => {{
          {self._mutation_guard_js(identity)}
          {self._icon_js()}
          const icons = {json.dumps(list(config.download_button_icons))};
          const controlSelectors = {json.dumps(list(config.download_button_selectors))};
          const isDownloadControl = (candidate) =>
            icons.includes(iconName(candidate)) || controlSelectors.some((s) => candidate.matches(s));
          const src = {json.dumps(src)};
          {self._latest_turn_js()}
          if (!latest) return {{clicked: false, reason: 'no_response'}};
          const img = Array.from(latest.querySelectorAll('img')).find((node) => (node.currentSrc || node.src) === src);
          if (!img) return {{clicked: false, reason: 'image_missing'}};
          let node = img;
          for (let depth = 0; node && node !== latest.parentElement; depth++) {{
            const big = Array.from(node.querySelectorAll('img'))
              .filter((candidate) => (candidate.naturalWidth || 0) >= {MIN_IMAGE_EDGE}).length;
            if (big > 1) return {{clicked: false, reason: 'ambiguous_image_scope'}};
            const button = Array.from(node.querySelectorAll('button, [role="button"]')).find(isDownloadControl);
            if (button) {{ button.click(); return {{clicked: true, depth}}; }}
            node = node.parentElement;
          }}
          return {{clicked: false, reason: 'download_control_missing'}};
        }})()
        """

    async def _download_full_size(
        self,
        src: str,
        directory: Path,
        identity: tuple[str, str | None] | None,
        *,
        timeout_seconds: float = 45.0,
    ) -> bytes | None:
        """Click the page download control and read the file Chrome saved into our private temp dir."""
        before = {path.name for path in directory.iterdir()}
        clicked = await self._evaluate(self._download_click_js(src, identity))
        if not (isinstance(clicked, dict) and clicked.get("clicked")):
            return None
        deadline = time.monotonic() + timeout_seconds
        last: tuple[str, int] | None = None
        stable = 0
        while time.monotonic() < deadline:
            await self._sleep(0.5)
            done = [
                path
                for path in directory.iterdir()
                if path.name not in before
                and not path.name.startswith(".")
                and not path.name.endswith(".crdownload")
                and path.is_file()
            ]
            if not done:
                stable = 0
                continue
            current = (done[0].name, done[0].stat().st_size)
            stable = stable + 1 if current == last and current[1] > 0 else 0
            last = current
            if stable >= 2:
                if current[1] > MAX_IMAGE_BYTES:
                    return None
                return done[0].read_bytes()
        return None

    def _read_transcript_js(self) -> str:
        return f"""
        (() => {{ /*transcript-read*/
          let root = null;
          for (const selector of {self._selector_query_js(self.CONFIG.transcript_root_selectors)}) {{
            root = document.querySelector(selector);
            if (root) break;
          }}
          root = root || document.body;
          const copy = root.cloneNode(true);
          copy.querySelectorAll('form, textarea, [contenteditable="true"], script, style').forEach((n) => n.remove());
          return (copy.innerText || copy.textContent || '').trim();
        }})()
        """

    async def _read_transcript(self) -> str:
        """The whole conversation text (composer excluded); the caller reads it, noise included."""
        value = await self._evaluate(self._read_transcript_js())
        return value if isinstance(value, str) else ""

    async def _last_transcript_response(self) -> dict[str, Any]:
        async with self._lock:
            with self._operation_scope():
                await self._ensure_session()
                state = await self._page_state()
                self._raise_for_page_state(state)
                text = await self._read_transcript()
                if not text:
                    raise ChatGPTWebError(
                        f"No {self.provider_name} conversation text is available on the current page."
                    )
                streaming = bool(state.get("streaming"))
                return {
                    "ok": True,
                    "status": "in_progress" if streaming else "completed",
                    "message": (
                        f"{self.provider_name} is still generating a response."
                        if streaming
                        else f"{self.provider_name} conversation text is ready."
                    ),
                    "response": text[:MAX_RESPONSE_CHARS],
                    "truncated": len(text) > MAX_RESPONSE_CHARS,
                    "transcript": True,
                    "streaming": streaming,
                    "url": state.get("url"),
                }

    async def _ask_transcript(
        self,
        prompt: str,
        *,
        new_chat: bool,
        timeout_seconds: int,
        progress: Any,
        on_completed: CompletionHook | None,
        before_prompt: PromptHook | None,
    ) -> dict[str, Any]:
        """Ask without per-turn selectors: submit, then read the whole conversation text.

        Ownership still comes from lifecycle signals (serialized operation, pinned page identity,
        observable submit acceptance, no automatic resend). The response is the whole conversation
        text, which is entirely ours for a fresh chat and includes earlier turns otherwise.
        """
        config = self.CONFIG
        if not isinstance(prompt, str) or not prompt.strip():
            raise ChatGPTWebError("Prompt must be a non-empty string.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ChatGPTWebError(f"Prompt exceeds the {MAX_PROMPT_CHARS:,}-character limit.")
        if timeout_seconds < 10 or timeout_seconds > 900:
            raise ChatGPTWebError("timeout_seconds must be between 10 and 900.")

        reported = 0.0

        async def report(percent: float, message: str) -> None:
            nonlocal reported
            if progress is not None:
                reported = max(reported, percent)
                try:
                    await asyncio.wait_for(progress(reported, message), timeout=PROGRESS_REPORT_TIMEOUT_SECONDS)
                except Exception:
                    pass  # progress is telemetry; its transport must not consume the tool result

        async with self._lock:
            with self._operation_scope():
                started = time.monotonic()
                readiness_deadline = time.monotonic() + DEFAULT_READINESS_TIMEOUT_SECONDS
                try:
                    async with asyncio.timeout(DEFAULT_READINESS_TIMEOUT_SECONDS):
                        await report(5, f"Opening the dedicated {self.provider_name} session.")
                        await self._ensure_session()
                        if new_chat:
                            await report(15, f"Opening a fresh {self.provider_name} conversation.")
                            await self._navigate()
                        await report(25, f"Preparing the {self.provider_name} prompt.")
                        before = await self._wait_for_composer(deadline=readiness_deadline)
                except TimeoutError:
                    await self._raise_readiness_timeout_after_cleanup(
                        ReadinessTimeoutError(
                            f"{self.provider_name} did not become ready within "
                            f"{DEFAULT_READINESS_TIMEOUT_SECONDS:g} seconds."
                        )
                    )
                except ReadinessTimeoutError as error:
                    await self._raise_readiness_timeout_after_cleanup(error)
                if before.get("streaming"):
                    before = await self._wait_until_idle(timeout_seconds=min(60, timeout_seconds))
                identity = self._page_identity(before)
                if identity is None:
                    raise ChatGPTWebError(f"The {self.provider_name} page has no stable identity for this operation.")
                self._operation_page_identity = identity
                try:
                    baseline_len = int(before.get("page_text_len", 0) or 0)
                except (TypeError, ValueError):
                    baseline_len = 0
                self._operation_baseline_images = {
                    item["src"] for item in before.get("page_images", []) if isinstance(item, dict) and item.get("src")
                }

                self._operation_attached_count = 0
                if before_prompt is not None:
                    await report(28, "Preparing attachments.")
                    await before_prompt()
                await self._focus_and_clear_composer()
                await self._insert_text(prompt)
                await self._submit(prompt)
                await report(35, f"Question sent. Waiting for {self.provider_name} to respond.")

                bound = self._operation_submit_identity or identity
                deadline = time.monotonic() + timeout_seconds
                last_signature: tuple[Any, ...] | None = None
                last_change = time.monotonic()
                streaming_seen = False
                response_started = False
                latest_state: dict[str, Any] = {}
                ready = False
                while time.monotonic() < deadline:
                    state = await self._page_state()
                    self._raise_for_page_state(state)
                    current = self._page_identity(state)
                    if current != bound:
                        if (
                            current is not None
                            and self._is_provisional_conversation_url(bound[0])
                            and self._is_conversation_url(current[0])
                        ):
                            bound = current
                        else:
                            raise ChatGPTWebError(
                                f"The {self.provider_name} conversation changed during response collection."
                            )
                    latest_state = state
                    try:
                        text_len = int(state.get("page_text_len", 0) or 0)
                    except (TypeError, ValueError):
                        text_len = 0
                    streaming = bool(state.get("streaming"))
                    streaming_seen = streaming_seen or streaming
                    fresh_images = [
                        item
                        for item in state.get("page_images", [])
                        if isinstance(item, dict) and item.get("src") not in self._operation_baseline_images
                    ]
                    signature = (text_len, state.get("page_text_tail"), len(fresh_images))
                    now = time.monotonic()
                    if signature != last_signature or streaming:
                        last_signature = signature
                        last_change = now
                    response_started = response_started or text_len > baseline_len or streaming or bool(fresh_images)
                    quiet_needed = TRANSCRIPT_QUIET_AFTER_STOP_SECONDS if streaming_seen else config.transcript_stable_seconds
                    quiet = now - last_change
                    # Without a stop control to watch, an image request that shows no image for this
                    # long is reported as "no image" (the caller can still use last_response later).
                    no_image_quiet = (
                        TRANSCRIPT_QUIET_AFTER_STOP_SECONDS if streaming_seen else config.transcript_no_image_seconds
                    )
                    ready = (
                        response_started
                        and not streaming
                        and quiet >= quiet_needed
                        and (
                            on_completed is None
                            or len(fresh_images) > self._operation_attached_count
                            or quiet >= no_image_quiet
                        )
                    )
                    if ready:
                        break
                    await report(
                        65 if response_started else 45,
                        f"{self.provider_name} is responding." if response_started else "Waiting for the response to start.",
                    )
                    await self._sleep(self._poll_interval)

                if not ready:
                    if response_started:
                        text = await self._read_transcript()
                        return {
                            "ok": False,
                            "status": "timed_out",
                            "message": (
                                f"Timed out while {self.provider_name} was responding. The response may complete later; "
                                "retrieve it with action=last_response without resending."
                            ),
                            "error_type": "GenerationTimeoutError",
                            "response": text[:MAX_RESPONSE_CHARS],
                            "truncated": len(text) > MAX_RESPONSE_CHARS,
                            "transcript": True,
                            "url": latest_state.get("url"),
                            "elapsed_seconds": round(time.monotonic() - started, 2),
                        }
                    raise ChatGPTWebError(
                        f"Timed out after {timeout_seconds} seconds while waiting for {self.provider_name} to respond."
                    )

                text = await self._read_transcript()
                await report(100, f"{self.provider_name} has finished and the final response is ready.")
                result: dict[str, Any] = {
                    "ok": True,
                    "status": "completed",
                    "message": f"{self.provider_name} has finished and the final response is ready.",
                    "response": text[:MAX_RESPONSE_CHARS],
                    "truncated": len(text) > MAX_RESPONSE_CHARS,
                    "transcript": True,
                    "url": latest_state.get("url"),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
                if on_completed is not None:
                    try:
                        result.update(await on_completed(latest_state))
                    except ChatGPTWebError as exc:
                        # The response itself completed; never resend because collection failed.
                        result.update(
                            {
                                "ok": False,
                                "status": "completed_collection_failed",
                                "message": str(exc),
                                "error_type": type(exc).__name__,
                            }
                        )
                return result

    def _latest_turn_js(self) -> str:
        """JS that defines `latest`: the whole latest assistant turn (turn ancestor when configured)."""
        config = self.CONFIG
        if config.transcript_mode:
            # Whole conversation root; per-turn selectors are exactly what drifts.
            return f"""
          let latest = null;
          for (const selector of {self._selector_query_js(config.transcript_root_selectors)}) {{
            latest = document.querySelector(selector);
            if (latest) break;
          }}
          latest = latest || document.body;
        """
        return f"""
          let assistant = [];
          for (const selector of {self._selector_query_js(config.assistant_selectors)}) {{
            const nodes = Array.from(document.querySelectorAll(selector));
            if (nodes.length) {{ assistant = nodes; break; }}
          }}
          let latest = assistant.length ? assistant[assistant.length - 1] : null;
          if (latest) {{
            for (const turnSelector of {self._selector_query_js(config.assistant_turn_selectors)}) {{
              const turn = latest.closest(turnSelector);
              if (turn) {{ latest = turn; break; }}
            }}
          }}
        """

    def _image_enumeration_js(self) -> str:
        """List every <img> in the latest assistant turn; filtering happens in Python."""
        return f"""
        (() => {{
          const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          {self._latest_turn_js()}
          if (!latest) return [];
          return Array.from(latest.querySelectorAll('img')).filter((img) => !img.closest('form')).map((img) => ({{
            src: img.currentSrc || img.src || '',
            width: img.naturalWidth || 0,
            height: img.naturalHeight || 0,
            visible: visible(img)
          }}));
        }})()
        """

    def _image_fetch_js(self, src: str) -> str:
        """Fetch one image inside the approved page; bytes return as base64, cookies are never read."""
        config = self.CONFIG
        return f"""
        (async () => {{
          const url = {json.dumps(src)};
          const limit = {MAX_IMAGE_BYTES};
          const b64 = (buffer) => {{
            const bytes = new Uint8Array(buffer);
            let out = '';
            for (let i = 0; i < bytes.length; i += 0x8000) {{
              out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
            }}
            return btoa(out);
          }};
          let lastError = 'fetch_failed';
          for (const credentials of ['omit', 'include']) {{
            try {{
              const response = await fetch(url, {{ credentials }});
              if (!response.ok) {{ lastError = 'http_' + response.status; continue; }}
              const buffer = await response.arrayBuffer();
              if (buffer.byteLength > limit) return {{ ok: false, error: 'too_large' }};
              return {{ ok: true, method: 'fetch', data: b64(buffer) }};
            }} catch (error) {{
              lastError = 'fetch_blocked';
            }}
          }}
          // Fallback: re-encode the already-rendered image (works when the canvas is not tainted).
          try {{
            {self._latest_turn_js()}
            const img = latest
              ? Array.from(latest.querySelectorAll('img')).find((node) => (node.currentSrc || node.src) === url)
              : null;
            if (img && img.naturalWidth && img.naturalHeight) {{
              const canvas = document.createElement('canvas');
              canvas.width = img.naturalWidth;
              canvas.height = img.naturalHeight;
              canvas.getContext('2d').drawImage(img, 0, 0);
              const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
              if (blob && blob.size <= limit) return {{ ok: true, method: 'canvas', data: b64(await blob.arrayBuffer()) }};
            }}
          }} catch (error) {{
            lastError = 'canvas_blocked';
          }}
          return {{ ok: false, error: lastError }};
        }})()
        """

    async def _download_image(self, src: str, *, full_resolution: bool) -> tuple[bytes, str, str]:
        """Return (bytes, method, source) for one candidate; prefers the original-size URL."""
        attempts: list[tuple[str, str]] = []
        full = full_resolution_url(src) if full_resolution else None
        if full is not None:
            attempts.append((full, "full"))
        attempts.append((src, "displayed"))
        last_error = "download_failed"
        for url, label in attempts:
            result = await self._evaluate(self._image_fetch_js(url))
            if isinstance(result, dict) and result.get("ok") and isinstance(result.get("data"), str):
                try:
                    return base64.b64decode(result["data"], validate=True), str(result.get("method", "fetch")), label
                except (binascii.Error, ValueError):
                    last_error = "invalid_base64"
                    continue
            if isinstance(result, dict) and isinstance(result.get("error"), str):
                last_error = result["error"]
        raise ChatGPTWebError(f"Could not download the generated image ({last_error}).")

    async def _collect_images(
        self,
        latest_state: dict[str, Any],
        *,
        save_dir: Path,
        max_images: int,
        full_resolution: bool,
    ) -> dict[str, Any]:
        """Runs inside the locked operation: enumerate all images, filter, download, save."""
        await self._assert_interactive_page(expected_identity=self._page_identity(latest_state))
        raw_images = await self._evaluate(self._image_enumeration_js())
        if self.CONFIG.transcript_mode:
            # Whole-page enumeration: keep images that were not on the page before submit and take the
            # last ones (generated images come after anything the user attached). Noise is expected.
            fresh = [
                item
                for item in filter_candidates(raw_images, MAX_IMAGES_LIMIT, self.CONFIG.image_host_roots)
                if item["src"] not in self._operation_baseline_images
            ]
            # The images the user attached are rendered in the transcript before any generated image
            # (live: Gemini returned the attached picture as a "result"), so skip those first.
            fresh = fresh[self._operation_attached_count :]
            candidates = fresh[-max(1, min(int(max_images), MAX_IMAGES_LIMIT)) :]
        else:
            candidates = filter_candidates(raw_images, max_images, self.CONFIG.image_host_roots)
        if not candidates:
            return {
                "ok": False,
                "status": "no_image",
                "message": (
                    f"{self.provider_name} finished without a usable image. The text response is included; "
                    "nothing was resent."
                ),
                "error_type": "NoImageGeneratedError",
                "images": [],
                "image_count": 0,
            }
        saved: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        identity = self._page_identity(latest_state)
        download_dir: Path | None = None
        if full_resolution and (self.CONFIG.download_button_icons or self.CONFIG.download_button_selectors):
            # Preferred: the page's own "download full size" control, saved into a private temp dir.
            try:
                download_dir = Path(tempfile.mkdtemp(prefix=f"mcp-chatbridge-{self.CONFIG.key}-"))
                await self._send_cdp_command(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(download_dir), "eventsEnabled": False},
                )
            except ChatGPTWebError:
                if download_dir is not None:
                    shutil.rmtree(download_dir, ignore_errors=True)
                download_dir = None
        try:
            for index, candidate in enumerate(candidates):
                try:
                    data: bytes | None = None
                    method = source = ""
                    if download_dir is not None:
                        try:
                            data = await self._download_full_size(candidate["src"], download_dir, identity)
                        except ChatGPTWebError:
                            data = None
                        if data is not None and sniff_image(data) is None:
                            data = None
                        if data is not None:
                            method, source = "download_button", "full_download"
                    if data is None:
                        data, method, source = await self._download_image(
                            candidate["src"], full_resolution=full_resolution
                        )
                    info = save_image(data, save_dir, provider=self.CONFIG.key)
                except (ChatGPTWebError, ImageStoreError) as exc:
                    errors.append({"index": index, "error": str(exc)})
                    continue
                if info["sha256"] in seen_hashes:
                    continue
                seen_hashes.add(info["sha256"])
                info.update({"index": index, "method": method, "source": source, "displayed_size": [candidate["width"], candidate["height"]]})
                saved.append(info)
        finally:
            if download_dir is not None:
                with suppress(Exception):
                    await self._send_cdp_command("Browser.setDownloadBehavior", {"behavior": "default"})
                shutil.rmtree(download_dir, ignore_errors=True)
        if not saved:
            detail = errors[0]["error"] if errors else "no image could be saved"
            raise ChatGPTWebError(f"{self.provider_name} generated an image but it could not be saved: {detail}")
        result: dict[str, Any] = {"images": saved, "image_count": len(saved), "save_dir": str(save_dir)}
        if errors:
            result["image_errors"] = errors
        return result

    async def generate_image(
        self,
        prompt: str,
        *,
        save_dir: str | None = None,
        max_images: int = 4,
        full_resolution: bool = True,
        new_chat: bool = True,
        timeout_seconds: int = 600,
        progress: Any = None,
        attach_images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ask the provider for an image, then save every content-sized image of the owned turn."""
        try:
            directory = resolve_save_dir(save_dir, REPOSITORY_ROOT, self.CONFIG.key)
        except ImageStoreError as exc:
            raise ChatGPTWebError(str(exc)) from exc

        async def collect(latest_state: dict[str, Any]) -> dict[str, Any]:
            return await self._collect_images(
                latest_state, save_dir=directory, max_images=max_images, full_resolution=full_resolution
            )

        return await self.ask(
            prompt,
            new_chat=new_chat,
            timeout_seconds=timeout_seconds,
            progress=progress,
            on_completed=collect,
            attach_images=attach_images,
        )

    async def _close_session_unlocked(self) -> None:
        try:
            await super()._close_session_unlocked()
        except ChatGPTWebError as exc:
            raise type(exc)(self._adapt_text(str(exc))) from exc


__all__ = [
    "ChallengeDetectedError",
    "ChatGPTWebError",
    "CommonBrowserBridge",
    "LoginRequiredError",
    "ProviderConfig",
    "ReadinessTimeoutError",
    "MAX_PROMPT_CHARS",
    "MAX_RESPONSE_CHARS",
    "MAX_PROGRESS_TAIL_CHARS",
]
