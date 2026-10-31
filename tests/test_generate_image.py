from __future__ import annotations

import base64
import dataclasses
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from common_bridge import ChatGPTWebError  # noqa: E402
from providers import ChatGPTBridge, ClaudeBridge, GeminiBridge  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
PNG_2 = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
LH3 = "https://lh3.googleusercontent.com/gg/abc"


async def no_sleep(_: float) -> None:
    return None


def page(**overrides: Any) -> dict[str, Any]:
    base = {
        "url": "https://gemini.google.com/app/abc123",
        "title": "Gemini",
        "document_token": "doc-1",
        "composer_ready": True,
        "assistant_count": 0,
        "latest_text": "",
        "latest_image_count": 0,
        "page_text_len": 10,
        "page_text_tail": "home",
        "page_images": [],
        "user_count": 0,
        "latest_user_text": "",
        "streaming": False,
        "challenge": False,
        "login_required": False,
    }
    base.update(overrides)
    return base


class ScriptedMixin:
    """Scripted page and in-page image server (no browser) shared by the Gemini and ChatGPT bridges."""

    def __init__(
        self,
        *,
        states: list[dict[str, Any]],
        images: list[dict[str, Any]],
        blobs: dict[str, Any],
    ) -> None:
        super().__init__(  # type: ignore[call-arg]
            profile_dir=Path(f"/tmp/mcp-chatbridge-test-profile-{self.CONFIG.key}-img"),  # type: ignore[attr-defined]
            sleep=no_sleep,
            poll_interval=0,
        )
        self.states = list(states)
        self.images = images
        self.blobs = blobs  # url -> bytes | {"ok": False, "error": ...}
        self.fetched: list[str] = []
        self.submits = 0
        self.events: list[str] = []
        self.cdp: list[tuple[str, dict[str, Any]]] = []
        self.attach_open_result: Any = {"ok": True}
        self.attachment_states: list[dict[str, Any]] = [{"previews": 0, "busy": 0}]
        self.attachment_fn: Any = None  # optional callable overriding attachment_states
        self.transcript_text = "You said: prompt\nAI said: reply"
        self.full_downloads: list[bytes | None] | None = None  # None = use the real implementation
        self.download_requests: list[str] = []

    async def _ensure_session(self) -> object:  # type: ignore[override]
        return object()

    async def _send_cdp_command(self, method: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        self.cdp.append((method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 7}
        return {}

    async def _download_full_size(self, src, directory, identity, *, timeout_seconds: float = 45.0):  # type: ignore[override]
        if self.full_downloads is None:
            return await super()._download_full_size(src, directory, identity, timeout_seconds=timeout_seconds)
        self.download_requests.append(src)
        return self.full_downloads.pop(0) if self.full_downloads else None

    async def _navigate(self, url: str | None = None) -> None:  # type: ignore[override]
        return None

    async def _page_state(self) -> dict[str, Any]:  # type: ignore[override]
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    async def _focus_and_clear_composer(self) -> None:  # type: ignore[override]
        return None

    async def _insert_text(self, text: str) -> None:  # type: ignore[override]
        self.events.append("insert")

    async def _submit(self, prompt: str, timeout_seconds: float | None = None) -> None:  # type: ignore[override]
        self.events.append("submit")
        self.submits += 1
        self._operation_submit_accepted = True
        self._operation_assistant_count_before = 0

    async def _evaluate(self, expression: str) -> Any:  # type: ignore[override]
        if "const limit =" in expression:
            url = json.loads(re.search(r"const url = (\".*?\");", expression).group(1))
            self.fetched.append(url)
            blob = self.blobs.get(url, {"ok": False, "error": "http_404"})
            if isinstance(blob, bytes):
                return {"ok": True, "method": "fetch", "data": base64.b64encode(blob).decode()}
            return blob
        if ".map((img)" in expression:
            return self.images
        if "/*transcript-read*/" in expression:
            return self.transcript_text
        if "attach_control_missing" in expression:
            self.events.append("attach_open")
            return self.attach_open_result
        if "const previews" in expression:
            if self.attachment_fn is not None:
                return self.attachment_fn()
            state = self.attachment_states.pop(0) if len(self.attachment_states) > 1 else self.attachment_states[0]
            return state
        if "download_control_missing" in expression:
            return {"clicked": False, "reason": "download_control_missing"}
        raise AssertionError("unexpected page script")


def fast(config):
    """Same adapter, but without the quiet-time waits (those have their own virtual-clock tests)."""
    return dataclasses.replace(config, transcript_stable_seconds=0.0, transcript_no_image_seconds=0.0)


class ScriptedGemini(ScriptedMixin, GeminiBridge):
    """Gemini bridge with a scripted page (no browser)."""

    CONFIG = fast(GeminiBridge.CONFIG)


class ScriptedChatGPT(ScriptedMixin, ChatGPTBridge):
    """ChatGPT bridge with a scripted page (no browser)."""

    CONFIG = fast(ChatGPTBridge.CONFIG)


class ScriptedClaude(ScriptedMixin, ClaudeBridge):
    """Claude bridge with a scripted page (no browser)."""

    CONFIG = fast(ClaudeBridge.CONFIG)


CLAUDE_URL = "https://claude.ai/chat/abc123"


CHATGPT_URL = "https://chatgpt.com/c/abc123"
CHATGPT_IMG = "https://chatgpt.com/backend-api/estuary/content?id=file_abc&ts=1"


def finished(image_count: int, text: str = "", url: str | None = None) -> list[dict[str, Any]]:
    """Before/before/after page states. The legacy turn counters are deliberately left at 0: the
    transcript mode must not need them (that is the whole point of the mode)."""
    extra = {"url": url} if url else {}
    fresh = [{"src": f"fresh://image-{i}", "w": 1024, "h": 1024} for i in range(image_count)]
    return [
        page(**extra),  # readiness / before
        page(**extra),  # before snapshot
        page(page_text_len=200, page_text_tail=text or "done", page_images=fresh, **extra),
    ]


def text_only(text: str, url: str | None = None) -> list[dict[str, Any]]:
    return finished(0, text=text, url=url)


def img(src: str, size: int = 1024) -> dict[str, Any]:
    return {"src": src, "width": size, "height": size, "visible": True}


class GenerateImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_only_turn_completes_and_saves_filtered_images(self) -> None:
        bridge = ScriptedGemini(
            states=finished(2),
            images=[
                img("https://www.gstatic.com/avatar.png", size=32),  # UI noise: filtered out
                img(f"{LH3}=s512"),
                img(f"{LH3}2=s512"),
                img("https://evil.example/tracker.png"),  # host not allowed: filtered out
            ],
            blobs={f"{LH3}=s0": PNG, f"{LH3}2=s0": PNG_2},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: a red fox", save_dir=root)
            saved = [Path(item["path"]) for item in result["images"]]
            contents = sorted(path.read_bytes() for path in saved)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(contents, sorted([PNG, PNG_2]))
        self.assertEqual({item["source"] for item in result["images"]}, {"full"})
        self.assertEqual(result["response"], bridge.transcript_text)  # the whole chat, read by the caller
        self.assertTrue(result["transcript"])
        self.assertNotIn("data", result["images"][0])
        self.assertEqual(bridge.submits, 1)
        self.assertEqual(bridge.fetched, [f"{LH3}=s0", f"{LH3}2=s0"])

    async def test_falls_back_to_displayed_url_when_full_size_fails(self) -> None:
        bridge = ScriptedGemini(
            states=finished(1, text="Here is your fox."),
            images=[img(f"{LH3}=s512")],
            blobs={f"{LH3}=s0": {"ok": False, "error": "http_403"}, f"{LH3}=s512": PNG},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["images"][0]["source"], "displayed")
        self.assertIn("reply", result["response"])

    async def test_identical_images_are_saved_once(self) -> None:
        bridge = ScriptedGemini(
            states=finished(2),
            images=[img(f"{LH3}=s512"), img(f"{LH3}b=s512")],
            blobs={f"{LH3}=s0": PNG, f"{LH3}b=s0": PNG},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)
            files = list(Path(root).iterdir())

        self.assertEqual(result["image_count"], 1)
        self.assertEqual(len(files), 1)

    async def test_text_only_answer_is_reported_without_resend(self) -> None:
        bridge = ScriptedGemini(states=text_only("I can't create that image."), images=[], blobs={})
        bridge.transcript_text = "You said: x\nGemini said: I can't create that image."
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)
            self.assertEqual(list(Path(root).iterdir()), [])

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_image")
        self.assertEqual(result["error_type"], "NoImageGeneratedError")
        self.assertIn("can't create that image", result["response"])
        self.assertEqual(bridge.submits, 1)
    async def test_download_failure_keeps_response_and_never_resends(self) -> None:
        bridge = ScriptedGemini(
            states=finished(1, text="done"),
            images=[img(f"{LH3}=s512")],
            blobs={},  # every fetch fails
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "completed_collection_failed")
        self.assertEqual(result["response"], bridge.transcript_text)
        self.assertIn("could not be saved", result["message"])
        self.assertEqual(bridge.submits, 1)

    async def test_non_image_payload_is_rejected_by_magic_bytes(self) -> None:
        bridge = ScriptedGemini(
            states=finished(1),
            images=[img(f"{LH3}=s512")],
            blobs={f"{LH3}=s0": b"<html>login</html>", f"{LH3}=s512": b"<html>login</html>"},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)
            self.assertEqual(list(Path(root).iterdir()), [])

        self.assertEqual(result["status"], "completed_collection_failed")

    async def test_partial_failure_returns_saved_images_with_errors(self) -> None:
        bridge = ScriptedGemini(
            states=finished(2),
            images=[img(f"{LH3}=s512"), img(f"{LH3}2=s512")],
            blobs={f"{LH3}=s0": PNG},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(len(result["image_errors"]), 1)

    async def test_repository_save_dir_is_rejected_before_any_browser_work(self) -> None:
        bridge = ScriptedGemini(states=finished(1), images=[], blobs={})
        with self.assertRaises(ChatGPTWebError):
            await bridge.generate_image("a fox", save_dir=str(PROJECT_DIR / "images"))
        self.assertEqual(bridge.submits, 0)

    async def test_plain_ask_reads_the_whole_chat_without_turn_counters(self) -> None:
        bridge = ScriptedGemini(states=text_only("hello back"), images=[], blobs={})
        bridge.transcript_text = "You said: hello\nGemini said: hello back"
        result = await bridge.ask("hello", timeout_seconds=10)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response"], "You said: hello\nGemini said: hello back")
        self.assertTrue(result["transcript"])
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(bridge.submits, 1)
    async def test_full_size_download_is_preferred_and_download_dir_is_restored(self) -> None:
        full = b"\xff\xd8\xff\xe0" + b"\x07" * 64  # JPEG magic
        bridge = ScriptedGemini(
            states=finished(1),
            images=[img(f"{LH3}=s512")],
            blobs={f"{LH3}=s0": PNG, f"{LH3}=s512": PNG},
        )
        bridge.full_downloads = [full]
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root)
            saved = Path(result["images"][0]["path"]).read_bytes()

        self.assertTrue(result["ok"], result)
        self.assertEqual(saved, full)
        self.assertEqual(result["images"][0]["source"], "full_download")
        self.assertEqual(result["images"][0]["method"], "download_button")
        self.assertEqual(bridge.fetched, [])  # no fetch/canvas fallback was needed
        self.assertEqual(bridge.download_requests, [f"{LH3}=s512"])
        behaviors = [params for method, params in bridge.cdp if method == "Browser.setDownloadBehavior"]
        self.assertEqual([item["behavior"] for item in behaviors], ["allow", "default"])
        self.assertFalse(Path(behaviors[0]["downloadPath"]).exists())  # private temp dir cleaned up

    async def test_failed_or_non_image_download_falls_back_to_fetch(self) -> None:
        for download in (None, b"<html>not an image</html>"):
            bridge = ScriptedGemini(
                states=finished(1),
                images=[img(f"{LH3}=s512")],
                blobs={f"{LH3}=s0": PNG},
            )
            bridge.full_downloads = [download]
            with tempfile.TemporaryDirectory() as root:
                result = await bridge.generate_image("a fox", save_dir=root)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["images"][0]["source"], "full")
            self.assertEqual(result["images"][0]["method"], "fetch")

    async def test_full_resolution_off_never_touches_download_behavior(self) -> None:
        bridge = ScriptedGemini(states=finished(1), images=[img(f"{LH3}=s512")], blobs={f"{LH3}=s512": PNG})
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("a fox", save_dir=root, full_resolution=False)

        self.assertTrue(result["ok"], result)
        self.assertFalse([m for m, _ in bridge.cdp if m == "Browser.setDownloadBehavior"])

    async def test_attachment_runs_before_prompt_and_submit(self) -> None:
        # With an attachment the page shows the user's own picture first, then the generated one.
        bridge = ScriptedGemini(
            states=finished(2), images=[img(f"{LH3}user=s512"), img(f"{LH3}=s512")], blobs={f"{LH3}=s0": PNG}
        )
        bridge.attachment_states = [{"previews": 0, "busy": 0}, {"previews": 1, "busy": 0}]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            result = await bridge.generate_image("edit it", save_dir=str(Path(root) / "out"), attach_images=[str(source)])

        self.assertTrue(result["ok"], result)
        self.assertLess(bridge.events.index("attach_open"), bridge.events.index("insert"))
        self.assertLess(bridge.events.index("insert"), bridge.events.index("submit"))
        methods = [method for method, _ in bridge.cdp]
        self.assertEqual(
            [m for m in methods if m.startswith("DOM.")], ["DOM.getDocument", "DOM.querySelector", "DOM.setFileInputFiles"]
        )
        selector = next(p for m, p in bridge.cdp if m == "DOM.querySelector")["selector"]
        self.assertIn("image/*", selector)  # prefers the image-only input
        files = next(p for m, p in bridge.cdp if m == "DOM.setFileInputFiles")
        self.assertEqual(files["files"], [str(source.resolve())])
        self.assertEqual(files["nodeId"], 7)

    async def test_attach_waits_for_upload_spinner_to_finish(self) -> None:
        bridge = ScriptedGemini(states=finished(1), images=[], blobs={})
        bridge.attachment_states = [
            {"previews": 0, "busy": 0},  # baseline
            {"previews": 1, "busy": 1},  # preview shown, still uploading
            {"previews": 1, "busy": 0},
            {"previews": 1, "busy": 0},
        ]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            await bridge._attach_images([str(source)])

        self.assertEqual(bridge.attachment_states, [{"previews": 1, "busy": 0}])  # consumed through the busy state

    async def test_missing_attach_control_or_bad_path_stops_before_anything_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)

            bridge = ScriptedGemini(states=finished(1), images=[], blobs={})
            bridge.attach_open_result = {"ok": False, "reason": "attach_control_missing"}
            with self.assertRaises(ChatGPTWebError):
                await bridge.generate_image("x", save_dir=str(Path(root) / "o"), attach_images=[str(source)])
            self.assertEqual(bridge.submits, 0)
            self.assertNotIn("insert", bridge.events)

            for bad in ("relative.png", str(Path(root) / "missing.png")):
                bridge = ScriptedGemini(states=finished(1), images=[], blobs={})
                with self.assertRaises(ChatGPTWebError):
                    await bridge.generate_image("x", save_dir=str(Path(root) / "o"), attach_images=[bad])
                self.assertEqual(bridge.events, [])  # rejected before any browser work

            not_image = Path(root) / "notes.png"
            not_image.write_text("plain text pretending to be an image")
            bridge = ScriptedGemini(states=finished(1), images=[], blobs={})
            with self.assertRaises(ChatGPTWebError):
                await bridge.generate_image("x", save_dir=str(Path(root) / "o"), attach_images=[str(not_image)])
            self.assertEqual(bridge.events, [])

    async def test_providers_without_attach_support_refuse_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            with self.assertRaises(ChatGPTWebError):
                await ChatGPTBridge(profile_dir=Path(root) / "p").ask("hi", attach_images=[str(source)])

    def test_icon_based_controls_do_not_depend_on_ui_language(self) -> None:
        config = GeminiBridge.CONFIG
        self.assertIn('button:has(mat-icon[fonticon="stop"])', config.stop_selectors)
        self.assertIn('button:has(mat-icon[fonticon="arrow_upward"])', config.send_selectors)
        self.assertEqual(config.attach_trigger_icons, ("plus",))
        self.assertEqual(config.download_button_icons, ("download",))
        bridge = ScriptedGemini(states=finished(0), images=[], blobs={})
        for script in (
            bridge._attach_open_js(),
            bridge._download_click_js(f"{LH3}=s512", ("https://gemini.google.com/app/x", "doc-1")),
        ):
            self.assertIn("iconName(", script)
            self.assertNotIn("Download full size image", script)  # localized label is never matched
            self.assertNotIn("Upload & tools", script)
        self.assertIn('"download"', bridge._download_click_js("x", None))
        self.assertIn("ambiguous_image_scope", bridge._download_click_js("x", None))

    async def test_chatgpt_generates_and_saves_with_its_own_host_allowlist(self) -> None:
        bridge = ScriptedChatGPT(
            states=finished(1, url=CHATGPT_URL),
            images=[
                img(CHATGPT_IMG),
                img(f"{LH3}=s512"),  # a Google host is not allowed for ChatGPT
            ],
            blobs={CHATGPT_IMG: PNG},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: a cactus", save_dir=root)
            saved = [Path(item["path"]).read_bytes() for item in result["images"]]

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["provider"], "chatgpt")
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(saved, [PNG])
        # ChatGPT has no inline download control (verified live), so the served bytes are fetched.
        self.assertEqual(bridge.download_requests, [])
        self.assertEqual((result["images"][0]["source"], result["images"][0]["method"]), ("displayed", "fetch"))
        self.assertEqual(bridge.fetched, [CHATGPT_IMG])
        self.assertFalse([m for m, _ in bridge.cdp if m == "Browser.setDownloadBehavior"])
        self.assertTrue(Path(result["images"][0]["path"]).name.startswith("chatgpt-"))

    async def test_each_provider_rejects_the_others_image_hosts(self) -> None:
        gemini = ScriptedGemini(states=finished(1), images=[img(CHATGPT_IMG)], blobs={CHATGPT_IMG: PNG})
        with tempfile.TemporaryDirectory() as root:
            result = await gemini.generate_image("x", save_dir=root)
        self.assertEqual(result["status"], "no_image")  # a ChatGPT URL is not a Gemini image
        self.assertEqual(gemini.fetched, [])

        chatgpt = ScriptedChatGPT(states=finished(1, url=CHATGPT_URL), images=[img(f"{LH3}=s512")], blobs={})
        with tempfile.TemporaryDirectory() as root:
            result = await chatgpt.generate_image("x", save_dir=root)
        self.assertEqual(result["status"], "no_image")
        self.assertEqual(chatgpt.fetched, [])

    async def test_chatgpt_free_limit_text_is_reported_as_no_image_without_resend(self) -> None:
        bridge = ScriptedChatGPT(states=text_only("You've hit the image limit for today.", url=CHATGPT_URL), images=[], blobs={})
        bridge.transcript_text = "You said: x\nChatGPT said: You've hit the image limit for today."
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: a cactus", save_dir=root)
            self.assertEqual(list(Path(root).iterdir()), [])

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_image")
        self.assertIn("hit the image limit", result["response"])
        self.assertEqual(bridge.submits, 1)  # the single paid-for request is never repeated
    async def test_chatgpt_attaches_without_a_menu_through_the_accept_less_input(self) -> None:
        # With an attachment the page shows the user's own picture first, then the generated one.
        bridge = ScriptedChatGPT(
            states=finished(2, url=CHATGPT_URL),
            images=[img("https://chatgpt.com/files/user.png"), img(CHATGPT_IMG)],
            blobs={CHATGPT_IMG: PNG},
        )
        bridge.attachment_states = [{"previews": 0, "busy": 0}, {"previews": 1, "busy": 0}]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            result = await bridge.generate_image("edit it", save_dir=str(Path(root) / "out"), attach_images=[str(source)])

        self.assertTrue(result["ok"], result)
        self.assertNotIn("attach_open", bridge.events)  # ChatGPT's file input already exists
        self.assertLess(bridge.events.index("insert"), bridge.events.index("submit"))
        selector = next(p for m, p in bridge.cdp if m == "DOM.querySelector")["selector"]
        # Live finding: only the input without an accept attribute attaches; image/* inputs do not.
        self.assertEqual(selector, 'input[type="file"]:not([accept])')
        self.assertNotIn("image/*", selector)
        self.assertEqual(next(p for m, p in bridge.cdp if m == "DOM.setFileInputFiles")["files"], [str(source.resolve())])

    async def _attach_on_virtual_clock(self, bridge_class: type, attaches_after_sets: int | None):
        """Run the real _attach_images on a virtual clock.

        The scripted page shows a finished preview once the file input was set ``attaches_after_sets``
        times (None = the page never reacts). Returns (clock, slept, set_calls, error).
        """
        import common_bridge
        from unittest.mock import AsyncMock, patch

        bridge = bridge_class(states=finished(0, url=CHATGPT_URL if bridge_class is ScriptedChatGPT else None),
                              images=[], blobs={})
        clock = {"value": 0.0}
        slept: list[float] = []

        def sets() -> int:
            return len([m for m, _ in bridge.cdp if m == "DOM.setFileInputFiles"])

        def fn() -> dict[str, int]:
            attached = attaches_after_sets is not None and sets() >= attaches_after_sets
            return {"previews": 1 if attached else 0, "busy": 0}

        bridge.attachment_fn = fn

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["value"] += seconds

        error = None
        with (
            patch.object(common_bridge.time, "monotonic", side_effect=lambda: clock["value"]),
            patch.object(bridge, "_sleep", AsyncMock(side_effect=fake_sleep)),
        ):
            try:
                await bridge._attach_images(["/x/in.png"])
            except ChatGPTWebError as exc:
                error = exc
        return clock, slept, sets(), error

    async def test_chatgpt_waits_for_the_page_to_settle_before_the_first_try(self) -> None:
        clock, slept, sets, error = await self._attach_on_virtual_clock(ScriptedChatGPT, attaches_after_sets=1)

        self.assertIsNone(error)
        self.assertEqual(sets, 1)
        self.assertEqual(slept[0], 3.0)  # live: an immediate set was ignored, one after 3s worked
        self.assertLess(clock["value"], 8.0)

    async def test_chatgpt_reissues_files_when_the_first_set_is_silently_ignored(self) -> None:
        clock, slept, sets, error = await self._attach_on_virtual_clock(ScriptedChatGPT, attaches_after_sets=2)

        self.assertIsNone(error)
        self.assertEqual(sets, 2)  # one retry after the 6s retry delay, not more
        self.assertGreaterEqual(clock["value"], 3.0 + 6.0)
        self.assertLess(clock["value"], 20.0)  # polled and finished soon after the retry, no 60s wait

    async def test_chatgpt_gives_up_after_three_tries_and_never_gets_to_send(self) -> None:
        clock, slept, sets, error = await self._attach_on_virtual_clock(ScriptedChatGPT, attaches_after_sets=None)

        self.assertIsInstance(error, ChatGPTWebError)
        self.assertIn("nothing was sent", str(error))
        self.assertEqual(sets, 3)
        self.assertGreaterEqual(clock["value"], 60.0)
        self.assertLess(clock["value"], 65.0)

    async def test_gemini_attach_has_no_settle_delay_or_retry(self) -> None:
        self.assertEqual(GeminiBridge.CONFIG.attach_settle_seconds, 0.0)
        self.assertEqual(GeminiBridge.CONFIG.attach_retry_seconds, 0.0)
        clock, slept, sets, error = await self._attach_on_virtual_clock(ScriptedGemini, attaches_after_sets=None)

        self.assertIsInstance(error, ChatGPTWebError)
        self.assertEqual(sets, 1)  # an ignored set is not repeated for Gemini
        self.assertNotIn(3.0, slept)

    def test_chatgpt_image_controls_read_the_whole_page_not_turn_nodes(self) -> None:
        config = ChatGPTBridge.CONFIG
        self.assertTrue(config.transcript_mode)
        self.assertEqual(config.transcript_root_selectors, ("main", "body"))
        self.assertEqual(config.image_host_roots[0], "chatgpt.com")
        self.assertEqual(config.attach_trigger_icons, ())
        self.assertEqual(config.submit_timeout_seconds, 120.0)
        bridge = ScriptedChatGPT(states=finished(0, url=CHATGPT_URL), images=[], blobs={})
        for script in (bridge._image_enumeration_js(), bridge._download_click_js(CHATGPT_IMG, None)):
            self.assertIn('"main"', script)  # whole-page root; per-turn selectors are what drifts
            self.assertNotIn("conversation-turn", script)
            self.assertNotIn("data-message-author-role", script)
        self.assertIn("closest('form')", bridge._image_enumeration_js())  # composer previews are not results
        self.assertEqual(config.download_button_selectors, ())  # nothing guessed: no inline download control
        self.assertEqual(config.download_button_icons, ())
    def test_only_gemini_is_exposed_to_image_generation_in_the_server(self) -> None:
        import server

        self.assertIs(server.BRIDGES["gemini"].__class__, GeminiBridge)
        self.assertTrue(hasattr(ChatGPTBridge, "generate_image"))  # shared layer; server gates by provider


if __name__ == "__main__":
    unittest.main()
