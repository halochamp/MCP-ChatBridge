"""Transcript mode: read the whole conversation, never depend on per-turn DOM selectors.

The page DOM of all three providers drifts. These tests prove the bridge keeps working when the legacy
turn counters (user_count/assistant_count/latest_text) stay at zero, and pin the generic signals it uses
instead: submit acceptance by text growth, completion by quiet time, a stop control when present, and
images that were not on the page before submit.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, PROJECT_DIR.as_posix())
sys.path.insert(0, (PROJECT_DIR / "tests").as_posix())

import common_bridge  # noqa: E402
from common_bridge import ChatGPTWebError  # noqa: E402
from providers import ChatGPTBridge, ClaudeBridge, GeminiBridge  # noqa: E402
from test_generate_image import (  # noqa: E402
    CHATGPT_URL,
    CLAUDE_URL,
    LH3,
    PNG,
    PNG_2,
    ScriptedChatGPT,
    ScriptedClaude,
    ScriptedGemini,
    ScriptedMixin,
    finished,
    img,
    page,
    text_only,
)

GEMINI_URL = "https://gemini.google.com/app/abc123"
PROVIDERS = (
    (ScriptedGemini, GEMINI_URL, "gemini"),
    (ScriptedChatGPT, CHATGPT_URL, "chatgpt"),
    (ScriptedClaude, CLAUDE_URL, "claude"),
)


def with_waits(config, *, stable: float, no_image: float = 45.0):
    return dataclasses.replace(config, transcript_stable_seconds=stable, transcript_no_image_seconds=no_image)


class SlowChatGPT(ScriptedMixin, ChatGPTBridge):
    """ChatGPT with the real quiet-time waits (driven by a virtual clock in the tests)."""

    CONFIG = with_waits(ChatGPTBridge.CONFIG, stable=2.0)


class RealWaitChatGPT(ScriptedMixin, ChatGPTBridge):
    CONFIG = with_waits(ChatGPTBridge.CONFIG, stable=8.0, no_image=45.0)


class ProvisionalChatGPT(ScriptedMixin, ChatGPTBridge):
    """Submit leaves the page on ChatGPT's provisional local route, like the real site.

    Uses the real quiet-time wait so the loop keeps polling long enough to see the final route.
    """

    CONFIG = with_waits(ChatGPTBridge.CONFIG, stable=2.0)

    async def _submit(self, prompt: str, timeout_seconds: float | None = None) -> None:  # type: ignore[override]
        await super()._submit(prompt, timeout_seconds)
        self._operation_submit_identity = ("https://chatgpt.com/c/local-chatgpt%3Aabc", "doc-1")


class VirtualClock:
    """Patches the clock and the bridge's sleep so quiet-time logic runs instantly and deterministically."""

    def __init__(self, bridge: Any, poll_interval: float = 0.75) -> None:
        self.bridge = bridge
        self.value = 0.0
        bridge._poll_interval = poll_interval

    async def _sleep(self, seconds: float) -> None:
        self.value += seconds

    def __enter__(self) -> "VirtualClock":
        self._patches = [
            patch.object(common_bridge.time, "monotonic", side_effect=lambda: self.value),
            patch.object(self.bridge, "_sleep", AsyncMock(side_effect=self._sleep)),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for item in self._patches:
            item.stop()


class TranscriptModeTests(unittest.IsolatedAsyncioTestCase):
    def test_all_three_providers_use_transcript_mode(self) -> None:
        for bridge_class in (GeminiBridge, ChatGPTBridge, ClaudeBridge):
            with self.subTest(provider=bridge_class.CONFIG.key):
                self.assertTrue(bridge_class.CONFIG.transcript_mode)
                self.assertEqual(bridge_class.CONFIG.transcript_root_selectors, ("main", "body"))

    async def test_every_provider_reads_the_whole_chat_even_when_turn_counters_stay_zero(self) -> None:
        for bridge_class, url, key in PROVIDERS:
            with self.subTest(provider=key):
                states = text_only("answer", url=url)
                self.assertEqual(states[-1]["user_count"], 0)  # the legacy counters see nothing
                self.assertEqual(states[-1]["assistant_count"], 0)
                bridge = bridge_class(states=states, images=[], blobs={})
                bridge.transcript_text = f"You said: q\n{key} said: answer"
                result = await bridge.ask("q", timeout_seconds=10)

                self.assertTrue(result["ok"], result)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["response"], bridge.transcript_text)
                self.assertTrue(result["transcript"])
                self.assertEqual(result["provider"], key)
                self.assertEqual(bridge.submits, 1)

    async def test_completion_waits_for_the_text_to_stop_changing(self) -> None:
        before = page(url=CHATGPT_URL)
        grow = [page(url=CHATGPT_URL, page_text_len=n, page_text_tail=f"t{n}") for n in (40, 80, 120)]
        bridge = SlowChatGPT(states=[before, before, *grow], images=[], blobs={})
        with VirtualClock(bridge) as clock:
            result = await bridge.ask("q", timeout_seconds=60)
            end = clock.value

        self.assertTrue(result["ok"], result)
        # 3 growth polls (~0.75s apart), then the 2s quiet window must elapse before it returns.
        self.assertGreaterEqual(end, 2.0 + 0.75 * 2)
        self.assertLess(end, 8.0)

    async def test_a_stop_control_ends_the_wait_soon_after_it_disappears(self) -> None:
        before = page(url=CHATGPT_URL)
        streaming = [page(url=CHATGPT_URL, page_text_len=n, page_text_tail=f"t{n}", streaming=True) for n in (40, 80, 120)]
        done = page(url=CHATGPT_URL, page_text_len=120, page_text_tail="t120", streaming=False)
        bridge = RealWaitChatGPT(states=[before, before, *streaming, done], images=[], blobs={})
        with VirtualClock(bridge) as clock:
            result = await bridge.ask("q", timeout_seconds=120)
            end = clock.value

        self.assertTrue(result["ok"], result)
        # Without a stop control the quiet window is 8s; having seen one it is 3s after the last stop poll.
        self.assertLess(end, 8.0)
        self.assertGreaterEqual(end, 3.0)

    async def test_image_request_with_a_stop_control_and_no_image_reports_no_image_quickly(self) -> None:
        before = page(url=CHATGPT_URL)
        streaming = [page(url=CHATGPT_URL, page_text_len=n, page_text_tail=f"t{n}", streaming=True) for n in (40, 80)]
        done = page(url=CHATGPT_URL, page_text_len=80, page_text_tail="t80", streaming=False)
        bridge = RealWaitChatGPT(states=[before, before, *streaming, done], images=[], blobs={})
        bridge.transcript_text = "You said: x\nChatGPT said: I could not make that."
        with VirtualClock(bridge) as clock, tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: x", save_dir=root)
            end = clock.value

        self.assertEqual(result["status"], "no_image")
        self.assertIn("could not make that", result["response"])
        self.assertLess(end, 10.0)  # not the 45s "never saw a stop control" fallback
        self.assertEqual(bridge.submits, 1)

    async def test_without_any_stop_control_an_imageless_request_waits_the_long_fallback(self) -> None:
        before = page(url=CHATGPT_URL)
        after = page(url=CHATGPT_URL, page_text_len=80, page_text_tail="t80")
        bridge = RealWaitChatGPT(states=[before, before, after], images=[], blobs={})
        with VirtualClock(bridge) as clock, tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: x", save_dir=root)
            end = clock.value

        self.assertEqual(result["status"], "no_image")
        self.assertGreaterEqual(end, 45.0)

    async def test_only_images_that_were_not_there_before_submit_are_saved_last_ones_first(self) -> None:
        old, new_1, new_2 = f"{LH3}old=s512", f"{LH3}one=s512", f"{LH3}two=s512"
        before = page(page_images=[{"src": old, "w": 1024, "h": 1024}])  # Gemini's own URL by default
        after = page(
            page_text_len=300,
            page_text_tail="done",
            page_images=[{"src": s, "w": 1024, "h": 1024} for s in (old, new_1, new_2)],
        )
        listed = [img(old), img(new_1), img(new_2)]
        blobs = {new_1: PNG, new_2: PNG_2}
        for max_images, expected in ((1, [PNG_2]), (2, [PNG, PNG_2])):
            with self.subTest(max_images=max_images):
                bridge = ScriptedGemini(states=[before, before, after], images=listed, blobs=blobs)
                with tempfile.TemporaryDirectory() as root:
                    result = await bridge.generate_image(
                        "x", save_dir=root, max_images=max_images, full_resolution=False
                    )
                    saved = sorted(Path(i["path"]).read_bytes() for i in result["images"])

                self.assertTrue(result["ok"], result)
                self.assertEqual(saved, sorted(expected))
                self.assertNotIn(old, bridge.fetched)  # the old image is never touched

    async def test_the_users_attached_image_is_never_returned_as_a_result(self) -> None:
        mine, made = f"{LH3}mine=s512", f"{LH3}made=s512"
        before = page()
        after = page(
            page_text_len=300,
            page_text_tail="done",
            # The attached picture is rendered in the transcript first, the generated one after it.
            page_images=[{"src": s, "w": 1024, "h": 1024} for s in (mine, made)],
        )
        bridge = ScriptedGemini(states=[before, before, after], images=[img(mine), img(made)], blobs={mine: PNG, made: PNG_2})
        bridge.attachment_states = [{"previews": 0, "busy": 0}, {"previews": 1, "busy": 0}]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            result = await bridge.generate_image(
                "edit", save_dir=str(Path(root) / "out"), attach_images=[str(source)], full_resolution=False
            )
            saved = [Path(i["path"]).read_bytes() for i in result["images"]]

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(saved, [PNG_2])  # only the generated image
        self.assertEqual(bridge.fetched, [made])  # the user's own picture is never even fetched

    async def test_with_an_attachment_one_visible_image_is_not_yet_a_result(self) -> None:
        # Only the user's own picture is on the page at first; the generated one arrives later.
        mine, made = f"{LH3}mine=s512", f"{LH3}made=s512"
        before = page(url=CHATGPT_URL)
        only_mine = page(
            url=CHATGPT_URL, page_text_len=60, page_text_tail="a", page_images=[{"src": mine, "w": 1024, "h": 1024}]
        )
        both = page(
            url=CHATGPT_URL,
            page_text_len=300,
            page_text_tail="b",
            page_images=[{"src": s, "w": 1024, "h": 1024} for s in (mine, made)],
        )
        bridge = SlowChatGPT(
            states=[before, before, only_mine, only_mine, only_mine, only_mine, both],
            images=[img(f"https://chatgpt.com/files/mine.png"), img("https://chatgpt.com/files/made.png")],
            blobs={"https://chatgpt.com/files/made.png": PNG_2},
        )
        bridge.attachment_states = [{"previews": 0, "busy": 0}, {"previews": 1, "busy": 0}]
        with VirtualClock(bridge) as clock, tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.png"
            source.write_bytes(PNG)
            result = await bridge.generate_image(
                "edit", save_dir=str(Path(root) / "out"), attach_images=[str(source)], timeout_seconds=120
            )
            end = clock.value

        self.assertTrue(result["ok"], result)
        # It kept waiting past the 2s quiet window because one image (the user's) is not a result yet.
        self.assertGreater(end, 3.0)
        self.assertEqual(result["image_count"], 1)

    async def test_last_response_returns_the_conversation_text(self) -> None:
        for bridge_class, url, key in PROVIDERS:
            with self.subTest(provider=key):
                bridge = bridge_class(states=[page(url=url)], images=[], blobs={})
                bridge.transcript_text = f"{key} conversation text"
                result = await bridge.last_response()

                self.assertTrue(result["ok"])
                self.assertEqual(result["response"], f"{key} conversation text")
                self.assertTrue(result["transcript"])
                self.assertEqual(result["provider"], key)

    async def test_an_unrelated_route_change_during_the_wait_fails_closed(self) -> None:
        before = page(url=CHATGPT_URL)
        elsewhere = page(url="https://chatgpt.com/c/someone-else", page_text_len=99)
        bridge = ScriptedChatGPT(states=[before, before, elsewhere], images=[], blobs={})
        with self.assertRaises(ChatGPTWebError) as raised:
            await bridge.ask("q", timeout_seconds=10)
        self.assertIn("changed during response collection", str(raised.exception))

    async def test_a_provisional_route_may_move_to_the_final_route_once(self) -> None:
        home = page(url="https://chatgpt.com/")
        provisional = page(url="https://chatgpt.com/c/local-chatgpt%3Aabc", page_text_len=30, page_text_tail="p")
        final = page(url="https://chatgpt.com/c/final-id", page_text_len=90, page_text_tail="f")
        # wait_for_composer consumes `home`; after the send the page is already on the provisional route.
        bridge = ProvisionalChatGPT(states=[home, provisional, final], images=[], blobs={})
        with VirtualClock(bridge):
            result = await bridge.ask("q", timeout_seconds=60)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["url"], "https://chatgpt.com/c/final-id")

    async def test_image_request_completes_from_a_page_with_no_turn_markup_at_all(self) -> None:
        # Mirrors the live ChatGPT page: no <article>, no author-role attributes, all counters 0.
        bridge = ScriptedChatGPT(
            states=finished(1, url=CHATGPT_URL),
            images=[img("https://chatgpt.com/backend-api/estuary/content?id=file_x&sig=1")],
            blobs={"https://chatgpt.com/backend-api/estuary/content?id=file_x&sig=1": PNG},
        )
        with tempfile.TemporaryDirectory() as root:
            result = await bridge.generate_image("Generate an image: a cactus", save_dir=root)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["provider"], "chatgpt")


class TranscriptSubmitAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """The real _submit accepts a send from page-text growth when the turn counters never move."""

    async def _submit_with_text_signal(self, bridge_class: type, url: str, grows_at: float | None) -> tuple[object, int, float]:
        with tempfile.TemporaryDirectory() as root:
            bridge = bridge_class(profile_dir=Path(root) / "profile")
            identity = (url, "123")
            bridge._operation_page_identity = identity
            before = {
                "url": url,
                "document_token": "123",
                "composer_ready": True,
                "assistant_count": 0,
                "user_count": 0,
                "streaming": False,
                "page_text_len": 10,
            }
            after = dict(before, page_text_len=60)  # only the page text moved
            clock = {"value": 0.0}
            sends = {"count": 0}

            async def fake_sleep(seconds: float) -> None:
                clock["value"] += seconds

            async def fake_state() -> dict[str, object]:
                return after if grows_at is not None and clock["value"] >= grows_at else before

            async def fake_evaluate(expression: str) -> object:
                if "const sendSelectors" in expression:
                    sends["count"] += 1
                    return True
                return {"ok": True}

            async def fake_key(**_: object) -> None:
                sends["count"] += 1

            outcome: object = "accepted"
            with (
                patch.object(common_bridge.time, "monotonic", side_effect=lambda: clock["value"]),
                patch.object(bridge, "_assert_interactive_page", AsyncMock(return_value=before)),
                patch.object(bridge, "_evaluate", AsyncMock(side_effect=fake_evaluate)),
                patch.object(bridge, "_dispatch_native_key", AsyncMock(side_effect=fake_key)),
                patch.object(bridge, "_page_state", AsyncMock(side_effect=fake_state)),
                patch.object(bridge, "_sleep", AsyncMock(side_effect=fake_sleep)),
            ):
                try:
                    await bridge._submit("prompt")
                except ChatGPTWebError as exc:
                    outcome = exc
            return outcome, sends["count"], clock["value"]

    async def test_text_growth_alone_accepts_the_submit_for_every_provider(self) -> None:
        for bridge_class, url in (
            (GeminiBridge, "https://gemini.google.com/app/existing"),
            (ChatGPTBridge, "https://chatgpt.com/c/existing"),
            (ClaudeBridge, "https://claude.ai/chat/existing"),
        ):
            with self.subTest(provider=bridge_class.CONFIG.key):
                outcome, sends, end = await self._submit_with_text_signal(bridge_class, url, grows_at=0.45)

                self.assertEqual(outcome, "accepted")
                self.assertEqual(sends, 1)
                self.assertLess(end, 1.0)  # as soon as the text moved, not after the 120s ceiling

    async def test_no_signal_at_all_still_fails_closed_without_resending(self) -> None:
        for bridge_class, url in (
            (GeminiBridge, "https://gemini.google.com/app/existing"),
            (ChatGPTBridge, "https://chatgpt.com/c/existing"),
        ):
            with self.subTest(provider=bridge_class.CONFIG.key):
                outcome, sends, end = await self._submit_with_text_signal(bridge_class, url, grows_at=None)

                self.assertIsInstance(outcome, ChatGPTWebError)
                self.assertIn("acceptance signal", str(outcome))
                self.assertEqual(sends, 1)
                self.assertGreaterEqual(end, 120.0)


if __name__ == "__main__":
    unittest.main()
