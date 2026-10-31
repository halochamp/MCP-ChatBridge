from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import common_bridge


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from common_bridge import ChatGPTWebError  # noqa: E402
from providers import ChatGPTBridge, ClaudeBridge, GeminiBridge  # noqa: E402


class _FakeSession:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = False
        self.killed = False

    async def start(self) -> None:
        self.started = True

    async def kill(self) -> None:
        self.killed = True


class _FailingStartSession(_FakeSession):
    async def start(self) -> None:
        self.started = True
        raise RuntimeError("startup failed")


class _CancelledStartSession(_FakeSession):
    async def start(self) -> None:
        self.started = True
        raise asyncio.CancelledError()


class _CaptureGemini(GeminiBridge):
    def __init__(self, *, profile_dir: Path) -> None:
        super().__init__(profile_dir=profile_dir)
        self.expression = ""

    async def _evaluate(self, expression: str):  # type: ignore[override]
        self.expression = expression
        return {
            "url": "https://gemini.google.com/app/abc",
            "title": "Gemini",
            "document_token": "123",
            "composer_ready": True,
            "assistant_count": 0,
            "latest_text": "",
            "user_count": 0,
            "latest_user_text": "",
            "streaming": False,
            "challenge": False,
            "login_required": False,
        }


class CommonBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_profiles_and_domains_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            env = {
                "CODEX_CHATGPT_PROFILE_DIR": str(base / "chatgpt"),
                "CODEX_GEMINI_PROFILE_DIR": str(base / "gemini"),
                "CODEX_CLAUDE_PROFILE_DIR": str(base / "claude"),
            }
            with patch.dict(os.environ, env, clear=False):
                chatgpt = ChatGPTBridge()
                gemini = GeminiBridge()
                claude = ClaudeBridge()

            self.assertEqual(chatgpt.profile_dir, (base / "chatgpt").resolve())
            self.assertEqual(gemini.profile_dir, (base / "gemini").resolve())
            self.assertEqual(claude.profile_dir, (base / "claude").resolve())
            self.assertEqual(len({chatgpt.profile_dir, gemini.profile_dir, claude.profile_dir}), 3)
            self.assertIn("chatgpt.com", chatgpt.CONFIG.allowed_domains)
            self.assertIn("gemini.google.com", gemini.CONFIG.allowed_domains)
            self.assertIn("claude.ai", claude.CONFIG.allowed_domains)
            self.assertNotIn("claude.ai", gemini.CONFIG.allowed_domains)

    def test_submit_acceptance_wait_is_per_provider_and_defaults_to_config(self) -> None:
        import inspect

        # Gemini's fresh-chat route appeared ~9-11s after Enter in a live probe, past the old fixed 8s
        # wait. User decision: 8s was too short and the services are sometimes slow, so every provider waits
        # two minutes for an observable acceptance signal (still fail-closed, still no resend).
        self.assertEqual(GeminiBridge.CONFIG.submit_timeout_seconds, 120.0)
        self.assertEqual(ChatGPTBridge.CONFIG.submit_timeout_seconds, 120.0)
        self.assertEqual(ClaudeBridge.CONFIG.submit_timeout_seconds, 120.0)
        self.assertIsNone(inspect.signature(common_bridge.CommonBrowserBridge._submit).parameters["timeout_seconds"].default)

    def test_chatgpt_provisional_routes_cover_the_current_local_route_form(self) -> None:
        provisional = ChatGPTBridge._is_provisional_conversation_url
        # Observed live: a fresh chat goes / -> /c/local-chatgpt%3A<uuid> -> /c/<uuid>.
        self.assertTrue(provisional("https://chatgpt.com/c/local-chatgpt%3A4cf4b90d-36c0-483e-9693-d0ad8de88f19"))
        self.assertTrue(provisional("https://chatgpt.com/c/local-chatgpt:4cf4b90d"))
        self.assertTrue(provisional("https://chatgpt.com/c/WEB:temporary"))  # the older form still works
        self.assertFalse(provisional("https://chatgpt.com/c/6ae5a5d5-b8f4-83ec-87c6-0ba226068c4e"))
        self.assertFalse(provisional("https://chatgpt.com/"))
        self.assertFalse(provisional("https://chatgpt.com.evil.example/c/local-chatgpt%3Aabc"))
        self.assertTrue(ChatGPTBridge._is_conversation_url("https://chatgpt.com/c/local-chatgpt%3Aabc"))
        # Other providers must not inherit it.
        self.assertFalse(GeminiBridge._is_provisional_conversation_url("https://gemini.google.com/app/local-chatgpt"))
        self.assertFalse(ClaudeBridge._is_provisional_conversation_url("https://claude.ai/chat/local-chatgpt"))

    def test_a_login_path_is_a_login_gate_whatever_the_dom_looks_like(self) -> None:
        from common_bridge import LoginRequiredError

        for bridge_class, base in (
            (ClaudeBridge, "https://claude.ai"),
            (ChatGPTBridge, "https://chatgpt.com"),
        ):
            with self.subTest(provider=bridge_class.CONFIG.key):
                state = {"url": f"{base}/login", "login_required": False, "challenge": False, "composer_ready": False}
                with self.assertRaises(LoginRequiredError):
                    bridge_class._raise_for_page_state(state)
                self.assertEqual(bridge_class._readiness_status(state), "login_required")
                self.assertTrue(bridge_class._is_login_path_url(f"{base}/login?returnTo=%2Fnew"))
                self.assertTrue(bridge_class._is_login_path_url(f"{base}/auth/login"))
        # Ordinary pages and look-alike paths are not login gates.
        self.assertFalse(ClaudeBridge._is_login_path_url("https://claude.ai/new"))
        self.assertFalse(ClaudeBridge._is_login_path_url("https://claude.ai/chat/login-help"))
        self.assertFalse(ChatGPTBridge._is_login_path_url("https://chatgpt.com/c/abc"))
        self.assertFalse(ChatGPTBridge._is_login_path_url("https://chatgpt.com.evil.example/login"))

    async def test_sign_in_hosts_are_authentication_only_for_every_provider(self) -> None:
        from common_bridge import (
            APPLE_SIGNIN_HOSTS,
            GOOGLE_SIGNIN_HOSTS,
            MICROSOFT_SIGNIN_HOSTS,
            LoginRequiredError,
        )

        sign_in = {
            ChatGPTBridge: GOOGLE_SIGNIN_HOSTS + APPLE_SIGNIN_HOSTS + MICROSOFT_SIGNIN_HOSTS,
            GeminiBridge: GOOGLE_SIGNIN_HOSTS,
            ClaudeBridge: GOOGLE_SIGNIN_HOSTS,
        }
        every_sign_in_host = set(GOOGLE_SIGNIN_HOSTS + APPLE_SIGNIN_HOSTS + MICROSOFT_SIGNIN_HOSTS)
        for bridge_class, hosts in sign_in.items():
            config = bridge_class.CONFIG
            with self.subTest(provider=config.key):
                for host in hosts:
                    self.assertIn(host, config.allowed_domains)  # a sign-in may finish in the window
                    self.assertIn(host, config.auth_roots)
                    self.assertNotIn(host, config.interactive_roots)  # but nothing is ever typed or clicked there
                    url = f"https://{host}/signin"
                    self.assertFalse(bridge_class._is_interactive_chatgpt_url(url))
                    with self.assertRaises(LoginRequiredError):  # a login gate, not "outside the domain"
                        bridge_class._raise_for_page_state({"url": url, "login_required": False, "challenge": False})
                    self.assertFalse(bridge_class._is_openai_auth_url(f"https://{host}.evil.example/signin"))
                # The mutation guard admits only the provider's own interactive root.
                guard = bridge_class._origin_guard_js()
                for host in every_sign_in_host:
                    self.assertNotIn(host, guard)
                # A provider is not given sign-in hosts it does not use.
                for host in every_sign_in_host - set(hosts):
                    self.assertNotIn(host, config.allowed_domains)
        # Interactive roots are unchanged: one per provider.
        self.assertEqual(ChatGPTBridge.CONFIG.interactive_roots, ("chatgpt.com",))
        self.assertEqual(GeminiBridge.CONFIG.interactive_roots, ("gemini.google.com",))
        self.assertEqual(ClaudeBridge.CONFIG.interactive_roots, ("claude.ai",))
    def test_provider_url_guards_are_fail_closed_and_separate(self) -> None:
        self.assertTrue(ChatGPTBridge._is_interactive_chatgpt_url("https://chatgpt.com/c/abc"))
        self.assertFalse(ChatGPTBridge._is_interactive_chatgpt_url("https://gemini.google.com/app"))
        self.assertTrue(GeminiBridge._is_interactive_chatgpt_url("https://gemini.google.com/app/abc"))
        self.assertFalse(GeminiBridge._is_interactive_chatgpt_url("https://gemini.google.com.evil.example/app"))
        self.assertTrue(ClaudeBridge._is_interactive_chatgpt_url("https://claude.ai/new"))
        self.assertFalse(ClaudeBridge._is_interactive_chatgpt_url("http://claude.ai/new"))
        self.assertFalse(ClaudeBridge._is_interactive_chatgpt_url("https://user@claude.ai/new"))

    def test_rich_editor_providers_use_native_input_without_changing_chatgpt(self) -> None:
        self.assertFalse(ChatGPTBridge.CONFIG.native_editor_input)
        self.assertFalse(ChatGPTBridge.CONFIG.submit_with_enter)
        self.assertIn('button[aria-label="Send"]', ChatGPTBridge.CONFIG.send_selectors)
        self.assertIn(
            'form:has(div[contenteditable="true"][data-virtualkeyboard="true"]) button[type="submit"]',
            ChatGPTBridge.CONFIG.send_selectors,
        )
        self.assertTrue(GeminiBridge.CONFIG.native_editor_input)
        self.assertTrue(GeminiBridge.CONFIG.submit_with_enter)
        self.assertTrue(ClaudeBridge.CONFIG.native_editor_input)
        self.assertTrue(ClaudeBridge.CONFIG.submit_with_enter)
        self.assertIn("structured-content-container.model-response-text", GeminiBridge.CONFIG.assistant_content_selectors)
        self.assertEqual(GeminiBridge.CONFIG.user_content_selectors[0], "p.query-text-line")
        self.assertEqual(ClaudeBridge.CONFIG.assistant_content_selectors[0], ".font-claude-response")
        self.assertEqual(ClaudeBridge.CONFIG.user_content_selectors[0], "p.whitespace-pre-wrap")

    def test_response_ownership_uses_submit_lifecycle_not_rendered_text(self) -> None:
        prompt = "source prompt whose UI rendering may change"
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            bridge._operation_user_count_before = 0
            bridge._operation_assistant_count_before = 0
            bridge._operation_submit_accepted = True

            state = {
                "user_count": 1,
                "assistant_count": 0,
                "latest_user_text": "completely reformatted by the provider UI",
                "streaming": False,
            }
            self.assertTrue(bridge._submitted_user_turn_is_visible(state, prompt))

            hidden_user_turn = dict(state, user_count=0, assistant_count=1)
            self.assertTrue(bridge._submitted_user_turn_is_visible(hidden_user_turn, prompt))

            extra_turn = dict(state, user_count=2)
            with self.assertRaises(ChatGPTWebError):
                bridge._submitted_user_turn_is_visible(extra_turn, prompt)

            bridge._operation_submit_accepted = False
            self.assertFalse(bridge._submitted_user_turn_is_visible(state, prompt))

    async def test_native_cdp_helper_uses_typed_input_library_not_send_as_callable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            insert_text = AsyncMock(return_value={})
            dispatch_key = AsyncMock(return_value={})
            cdp_session = SimpleNamespace(
                session_id="session-1",
                cdp_client=SimpleNamespace(
                    send=SimpleNamespace(
                        Input=SimpleNamespace(
                            insertText=insert_text,
                            dispatchKeyEvent=dispatch_key,
                        )
                    )
                ),
            )
            browser_session = object()
            with (
                patch.object(bridge, "_ensure_session", AsyncMock(return_value=browser_session)),
                patch.object(bridge, "_get_bound_cdp_session", AsyncMock(return_value=cdp_session)),
            ):
                await bridge._send_cdp_command("Input.insertText", {"text": "hello"})
                await bridge._send_cdp_command("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter"})

            insert_text.assert_awaited_once_with(params={"text": "hello"}, session_id="session-1")
            dispatch_key.assert_awaited_once_with(
                params={"type": "keyDown", "key": "Enter"},
                session_id="session-1",
            )

    async def test_native_cdp_type_error_is_not_misclassified_as_connection_loss(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            insert_text = AsyncMock(side_effect=TypeError("wrong call shape"))
            cdp_session = SimpleNamespace(
                session_id="session-1",
                cdp_client=SimpleNamespace(
                    send=SimpleNamespace(
                        Input=SimpleNamespace(
                            insertText=insert_text,
                            dispatchKeyEvent=AsyncMock(return_value={}),
                        )
                    )
                ),
            )
            browser_session = object()
            with (
                patch.object(bridge, "_ensure_session", AsyncMock(return_value=browser_session)),
                patch.object(bridge, "_get_bound_cdp_session", AsyncMock(return_value=cdp_session)),
                patch.object(bridge, "_invalidate_session", AsyncMock()) as invalidate,
            ):
                with self.assertRaisesRegex(ChatGPTWebError, "native CDP input command was invoked incorrectly"):
                    await bridge._send_cdp_command("Input.insertText", {"text": "hello"})

            invalidate.assert_not_awaited()

    async def test_gemini_long_prompt_uses_native_cdp_editor_input(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            identity = ("https://gemini.google.com/app/abc", "123")
            bridge._operation_page_identity = identity
            state = {
                "url": identity[0],
                "document_token": identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
            }
            prompt = ("line one\n\n```python\nprint('x')\n```\n" * 700)[:20000]
            with (
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=state)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value=True)),
                patch.object(bridge, "_send_cdp_command", AsyncMock(return_value={})) as send,
            ):
                await bridge._focus_and_clear_composer()
                await bridge._insert_text(prompt)

            methods = [call.args[0] for call in send.await_args_list]
            self.assertEqual(methods.count("Input.dispatchKeyEvent"), 4)
            self.assertEqual(methods.count("Input.insertText"), 1)
            insert_call = next(call for call in send.await_args_list if call.args[0] == "Input.insertText")
            self.assertEqual(insert_call.args[1], {"text": prompt})

    async def test_submit_preflight_is_structural_and_does_not_embed_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = ClaudeBridge(profile_dir=Path(root) / "profile")
            identity = ("https://claude.ai/chat/existing", "123")
            bridge._operation_page_identity = identity
            before = {
                "url": identity[0],
                "document_token": identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
            }
            after = dict(before, user_count=1)
            secret_fragment = "DO_NOT_EMBED_THIS_PROMPT_IN_PREFLIGHT_JS"
            with (
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value={"ok": True})) as evaluate,
                patch.object(bridge, "_dispatch_native_key", AsyncMock()),
                patch.object(bridge, "_page_state", AsyncMock(return_value=after)),
                patch.object(bridge, "_sleep", AsyncMock()),
            ):
                await bridge._submit(secret_fragment, timeout_seconds=1.0)

            preflight_expression = evaluate.await_args_list[0].args[0]
            self.assertNotIn(secret_fragment, preflight_expression)
            self.assertNotIn("expectedPrompt", preflight_expression)
            self.assertNotIn("exactMatch", preflight_expression)
            self.assertIn("composer_empty", preflight_expression)
            self.assertTrue(bridge._operation_submit_accepted)

    async def test_claude_native_enter_submit_stops_before_click_on_observed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = ClaudeBridge(profile_dir=Path(root) / "profile")
            identity = ("https://claude.ai/chat/existing", "123")
            bridge._operation_page_identity = identity
            before = {
                "url": identity[0],
                "document_token": identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
            }
            after = dict(before, user_count=1)
            with (
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value={"ok": True})) as evaluate,
                patch.object(bridge, "_dispatch_native_key", AsyncMock()) as key,
                patch.object(bridge, "_page_state", AsyncMock(return_value=after)),
                patch.object(bridge, "_sleep", AsyncMock()),
            ):
                await bridge._submit("long prompt")

            key.assert_awaited_once_with(key="Enter", code="Enter", virtual_key=13, text="\r")
            self.assertEqual(evaluate.await_count, 1, "button fallback must not run after an observed submitted turn")
            preflight_expression = evaluate.await_args.args[0]
            self.assertNotIn("expectedPrompt", preflight_expression)
            self.assertNotIn("exactMatch", preflight_expression)
            self.assertTrue(bridge._operation_submit_accepted)

    async def test_native_enter_waits_for_delayed_acceptance_without_click_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = ClaudeBridge(profile_dir=Path(root) / "profile")
            identity = ("https://claude.ai/chat/existing", "123")
            bridge._operation_page_identity = identity
            before = {
                "url": identity[0],
                "document_token": identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
            }
            after = dict(before, user_count=1)
            clock = {"value": 0.0}

            async def fake_sleep(seconds: float) -> None:
                clock["value"] += seconds

            async def fake_page_state() -> dict[str, object]:
                return after if clock["value"] >= 2.25 else before

            with (
                patch.object(common_bridge.time, "monotonic", side_effect=lambda: clock["value"]),
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value={"ok": True})) as evaluate,
                patch.object(bridge, "_dispatch_native_key", AsyncMock()) as key,
                patch.object(bridge, "_page_state", AsyncMock(side_effect=fake_page_state)),
                patch.object(bridge, "_sleep", AsyncMock(side_effect=fake_sleep)),
            ):
                await bridge._submit("long prompt")

            key.assert_awaited_once_with(key="Enter", code="Enter", virtual_key=13, text="\r")
            self.assertEqual(evaluate.await_count, 1, "native submit must never issue a second click action")
            self.assertGreaterEqual(clock["value"], 2.25)
            self.assertTrue(bridge._operation_submit_accepted)

    async def _run_submit_on_virtual_clock(
        self, bridge_class: type, url: str, accept_at: float | None, *, control_ready_at: float = 0.0
    ) -> tuple[dict[str, float], int, object, int]:
        """Drive a provider's real _submit on a virtual clock.

        Returns (clock, page polls, outcome, number of send actions). Gemini submits with a native
        Enter; ChatGPT clicks its send button. ``control_ready_at`` delays the send control (click path).
        """
        with tempfile.TemporaryDirectory() as root:
            bridge = bridge_class(profile_dir=Path(root) / "profile")
            identity = (url, "123")
            bridge._operation_page_identity = identity
            before = {
                "url": identity[0],
                "document_token": identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
            }
            after = dict(before, user_count=1)
            clock = {"value": 0.0}
            polls = {"count": 0}
            sends = {"count": 0}

            async def fake_sleep(seconds: float) -> None:
                clock["value"] += seconds

            async def fake_page_state() -> dict[str, object]:
                polls["count"] += 1
                return after if accept_at is not None and clock["value"] >= accept_at else before

            async def fake_evaluate(expression: str) -> object:
                if "const sendSelectors" in expression:  # the click path's send action
                    if clock["value"] < control_ready_at:
                        return False  # send control not available yet
                    sends["count"] += 1
                    return True
                return {"ok": True}  # structural preflight

            async def fake_key(**_: object) -> None:
                sends["count"] += 1

            outcome: object = "accepted"
            with (
                patch.object(common_bridge.time, "monotonic", side_effect=lambda: clock["value"]),
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(side_effect=fake_evaluate)),
                patch.object(bridge, "_dispatch_native_key", AsyncMock(side_effect=fake_key)),
                patch.object(bridge, "_page_state", AsyncMock(side_effect=fake_page_state)),
                patch.object(bridge, "_sleep", AsyncMock(side_effect=fake_sleep)),
            ):
                try:
                    await bridge._submit("prompt")  # no explicit timeout: uses the provider config
                except ChatGPTWebError as exc:
                    outcome = exc
            self.assertEqual(bridge_class.CONFIG.submit_timeout_seconds, 120.0)
            return clock, polls["count"], outcome, sends["count"]

    async def test_submit_wait_polls_and_returns_as_soon_as_accepted_not_after_two_minutes(self) -> None:
        for bridge_class, url in (
            (GeminiBridge, "https://gemini.google.com/app/existing"),
            (ChatGPTBridge, "https://chatgpt.com/c/existing"),
        ):
            with self.subTest(provider=bridge_class.CONFIG.key):
                clock, polls, outcome, sends = await self._run_submit_on_virtual_clock(bridge_class, url, accept_at=0.45)

                self.assertEqual(outcome, "accepted")
                self.assertGreaterEqual(clock["value"], 0.45)
                self.assertLess(clock["value"], 1.0)  # nowhere near the 120s ceiling
                self.assertGreaterEqual(polls, 3)  # it polled the page repeatedly while waiting
                self.assertEqual(sends, 1)

    async def test_submit_wait_only_gives_up_at_the_two_minute_ceiling_and_never_resends(self) -> None:
        for bridge_class, url in (
            (GeminiBridge, "https://gemini.google.com/app/existing"),
            (ChatGPTBridge, "https://chatgpt.com/c/existing"),
        ):
            with self.subTest(provider=bridge_class.CONFIG.key):
                clock, polls, outcome, sends = await self._run_submit_on_virtual_clock(bridge_class, url, accept_at=None)

                self.assertIsInstance(outcome, ChatGPTWebError)
                self.assertIn("acceptance signal", str(outcome))
                self.assertGreaterEqual(clock["value"], 120.0)
                self.assertLess(clock["value"], 121.0)
                self.assertGreater(polls, 100)  # checked periodically the whole time, not one long sleep
                self.assertEqual(sends, 1)  # exactly one send action in two minutes: never resent

    async def test_chatgpt_waits_for_a_late_send_control_then_accepts_early(self) -> None:
        clock, polls, outcome, sends = await self._run_submit_on_virtual_clock(
            ChatGPTBridge, "https://chatgpt.com/c/existing", accept_at=1.6, control_ready_at=1.0
        )

        self.assertEqual(outcome, "accepted")
        self.assertGreaterEqual(clock["value"], 1.6)
        self.assertLess(clock["value"], 3.0)  # polled for the control, then for acceptance, no long wait
        self.assertEqual(sends, 1)

    async def test_fresh_submit_does_not_accept_static_foreign_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            before_identity = ("https://gemini.google.com/app", "123")
            foreign_identity = ("https://gemini.google.com/app/foreign", "123")
            bridge._operation_page_identity = before_identity
            before = {
                "url": before_identity[0],
                "document_token": before_identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
                "challenge": False,
                "login_required": False,
            }
            foreign = dict(
                before,
                url=foreign_identity[0],
                assistant_count=1,
                user_count=1,
                latest_text="static foreign response",
            )
            clock = {"value": 0.0}

            async def fake_sleep(seconds: float) -> None:
                clock["value"] += seconds

            with (
                patch.object(common_bridge.time, "monotonic", side_effect=lambda: clock["value"]),
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value={"ok": True})),
                patch.object(bridge, "_dispatch_native_key", AsyncMock()),
                patch.object(bridge, "_page_state", AsyncMock(return_value=foreign)),
                patch.object(bridge, "_sleep", AsyncMock(side_effect=fake_sleep)),
            ):
                with self.assertRaisesRegex(ChatGPTWebError, "no observable lifecycle acceptance signal"):
                    await bridge._submit("prompt", timeout_seconds=0.6)

            self.assertFalse(bridge._operation_submit_accepted)
            self.assertIsNone(bridge._operation_submit_identity)

    async def test_fresh_submit_binds_route_only_after_lifecycle_changes_on_that_route(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile")
            before_identity = ("https://gemini.google.com/app", "123")
            owned_identity = ("https://gemini.google.com/app/owned", "123")
            bridge._operation_page_identity = before_identity
            before = {
                "url": before_identity[0],
                "document_token": before_identity[1],
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
                "challenge": False,
                "login_required": False,
            }
            candidate = dict(before, url=owned_identity[0], user_count=1)
            changed = dict(candidate, assistant_count=1, streaming=True, latest_text="started")
            with (
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(return_value={"ok": True})),
                patch.object(bridge, "_dispatch_native_key", AsyncMock()),
                patch.object(bridge, "_page_state", AsyncMock(side_effect=[candidate, changed])),
                patch.object(bridge, "_sleep", AsyncMock()),
            ):
                await bridge._submit("prompt")

            self.assertTrue(bridge._operation_submit_accepted)
            self.assertEqual(bridge._operation_submit_identity, owned_identity)

            bridge._operation_response_identity = before_identity
            bridge._operation_user_count_before = 0
            bridge._operation_allow_new_chat_transition = True
            foreign = dict(changed, url="https://gemini.google.com/app/foreign")
            with patch.object(bridge, "_page_state", AsyncMock(return_value=foreign)):
                with self.assertRaisesRegex(ChatGPTWebError, "conversation changed"):
                    await bridge._response_page_state("prompt")

    def test_new_chat_transition_routes_are_provider_specific(self) -> None:
        self.assertFalse(GeminiBridge._is_conversation_url("https://gemini.google.com/app"))
        self.assertTrue(GeminiBridge._is_conversation_url("https://gemini.google.com/app/abc"))
        self.assertFalse(ClaudeBridge._is_conversation_url("https://claude.ai/new"))
        self.assertTrue(ClaudeBridge._is_conversation_url("https://claude.ai/chat/abc"))
        self.assertFalse(ChatGPTBridge._is_conversation_url("https://chatgpt.com/"))
        self.assertTrue(ChatGPTBridge._is_conversation_url("https://chatgpt.com/c/abc"))

    def test_origin_guard_contains_only_the_adapter_interactive_root(self) -> None:
        gemini_guard = GeminiBridge._origin_guard_js()
        claude_guard = ClaudeBridge._origin_guard_js()
        self.assertIn("gemini.google.com", gemini_guard)
        self.assertNotIn("claude.ai", gemini_guard)
        self.assertIn("claude.ai", claude_guard)
        self.assertNotIn("chatgpt.com", claude_guard)
        self.assertIn("currentUrl.username !== ''", gemini_guard)
        self.assertIn("currentUrl.password !== ''", gemini_guard)

    async def test_session_factory_receives_only_provider_allowed_domains(self) -> None:
        created: list[_FakeSession] = []

        def factory(**kwargs: object) -> _FakeSession:
            session = _FakeSession(**kwargs)
            created.append(session)
            return session

        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile", session_factory=factory)
            session = await bridge._ensure_session()
            self.assertTrue(session.started)
            self.assertEqual(session.kwargs["allowed_domains"], list(GeminiBridge.CONFIG.allowed_domains))
            self.assertNotIn("chatgpt.com", session.kwargs["allowed_domains"])
            await bridge.close()
            self.assertTrue(session.killed)

    async def test_failed_provider_start_kills_partial_session_and_releases_ownership(self) -> None:
        created: list[_FailingStartSession] = []

        def factory(**kwargs: object) -> _FailingStartSession:
            session = _FailingStartSession(**kwargs)
            created.append(session)
            return session

        with tempfile.TemporaryDirectory() as root:
            bridge = GeminiBridge(profile_dir=Path(root) / "profile", session_factory=factory)
            with self.assertRaisesRegex(Exception, "Could not start the dedicated Gemini browser session"):
                await bridge._ensure_session()

            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].killed)
            self.assertIsNone(bridge._session)
            self.assertIsNone(bridge._profile_lock_handle)
            self.assertFalse(bridge._ownership_marker_file().exists())

    async def test_cancelled_provider_start_keeps_ownership_until_explicit_close(self) -> None:
        created: list[_CancelledStartSession] = []

        def factory(**kwargs: object) -> _CancelledStartSession:
            session = _CancelledStartSession(**kwargs)
            created.append(session)
            return session

        with tempfile.TemporaryDirectory() as root:
            bridge = ClaudeBridge(profile_dir=Path(root) / "profile", session_factory=factory)
            with self.assertRaises(asyncio.CancelledError):
                await bridge._ensure_session()

            self.assertEqual(len(created), 1)
            self.assertIs(bridge._session, created[0])
            self.assertTrue(bridge._reconnect_blocked)
            self.assertIsNotNone(bridge._profile_lock_handle)
            self.assertTrue(bridge._ownership_marker_file().exists())

            await bridge.close()
            self.assertTrue(created[0].killed)
            self.assertFalse(bridge._reconnect_blocked)
            self.assertIsNone(bridge._profile_lock_handle)
            self.assertFalse(bridge._ownership_marker_file().exists())

    async def test_gemini_page_state_uses_semantic_provider_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bridge = _CaptureGemini(profile_dir=Path(root) / "profile")
            state = await bridge._page_state()

        self.assertEqual(state["url"], "https://gemini.google.com/app/abc")
        self.assertIn("rich-textarea .ql-editor", bridge.expression)
        self.assertIn("model-response", bridge.expression)
        self.assertIn("structured-content-container.model-response-text", bridge.expression)
        self.assertIn("user-query", bridge.expression)
        self.assertIn("p.query-text-line", bridge.expression)
        self.assertIn("latestAssistantContent || latestAssistant", bridge.expression)
        self.assertIn("latestUserContentNodes.map", bridge.expression)
        self.assertIn("join('\\n')", bridge.expression)
        self.assertIn("firstAll", bridge.expression)

    def test_provider_text_adaptation_keeps_chatgpt_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            chatgpt = ChatGPTBridge(profile_dir=base / "chatgpt")
            gemini = GeminiBridge(profile_dir=base / "gemini")
            claude = ClaudeBridge(profile_dir=base / "claude")

        source = "ChatGPT is generating. Recover with chatgpt_last_response."
        self.assertEqual(chatgpt._adapt_text(source), source)
        self.assertEqual(
            gemini._adapt_text(source),
            "Gemini is generating. Recover with gemini_last_response.",
        )
        self.assertEqual(
            claude._adapt_text(source),
            "Claude is generating. Recover with claude_last_response.",
        )


if __name__ == "__main__":
    unittest.main()
