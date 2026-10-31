from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import server  # noqa: E402
from test_chatgpt_web import ScriptedBridge, state  # noqa: E402


class _CapturingContext:
    def __init__(self) -> None:
        self.events: list[tuple[float, float | None, str | None]] = []

    async def report_progress(self, progress: float, total: float | None, message: str | None) -> None:
        self.events.append((progress, total, message))


class _ProgressReportingBridge:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, object]] = []

    async def ask(self, prompt: str, *, progress: object = None, **kwargs: object) -> dict[str, object]:
        self.calls.append(("ask", (prompt, kwargs)))
        assert callable(progress)
        await progress(35, f"Question sent. Waiting for {self.name} to start responding.")
        await progress(65, f"{self.name} is generating its response.\n\nLatest visible response tail:\npartial context")
        await progress(100, f"{self.name} has finished and the final response is ready.")
        return {
            "ok": True,
            "provider": self.name.lower(),
            "status": "completed",
            "message": f"{self.name} has finished and the final response is ready.",
            "response": "final answer",
        }

    async def status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {"ok": True, "provider": self.name.lower(), "status": "ready"}

    async def last_response(self) -> dict[str, object]:
        self.calls.append(("last_response", None))
        return {"ok": True, "provider": self.name.lower(), "status": "completed", "response": "recovered"}

    async def new_chat(self) -> dict[str, object]:
        self.calls.append(("new_chat", None))
        return {"ok": True, "provider": self.name.lower(), "status": "ready"}

    async def close(self) -> None:
        self.calls.append(("close", None))


class ServerToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_incomplete_result_is_not_replaced_by_generic_error(self) -> None:
        class IncompleteBridge(_ProgressReportingBridge):
            async def ask(self, prompt: str, *, progress: object = None, **kwargs: object) -> dict[str, object]:
                return {
                    "ok": False,
                    "provider": "gemini",
                    "status": "timed_out",
                    "response": "all owned text so far",
                    "truncated": False,
                    "error_type": "GenerationTimeoutError",
                }

        with patch.dict(server.BRIDGES, {"gemini": IncompleteBridge("Gemini")}):
            result = await server.ask_gemini("prompt")
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["response"], "all owned text so far")
        self.assertEqual(result["provider"], "gemini")

    async def test_partial_progress_before_timeout_never_becomes_completed(self) -> None:
        class TimedOutBridge(_ProgressReportingBridge):
            async def ask(self, prompt: str, *, progress: object = None, **kwargs: object) -> dict[str, object]:
                assert callable(progress)
                await progress(65, "Latest visible response tail:\npartial answer")
                raise server.ChatGPTWebError("Timed out while waiting for final response")

        context = _CapturingContext()
        with patch.dict(server.BRIDGES, {"gemini": TimedOutBridge("Gemini")}):
            result = await server.ask_gemini("prompt", ctx=context)  # type: ignore[arg-type]
        self.assertEqual(context.events[0][0], 65)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result.get("status"), "completed")

    async def test_caller_gets_terminal_result_when_final_progress_delivery_fails(self) -> None:
        class FailingFinalContext(_CapturingContext):
            async def report_progress(self, progress: float, total: float | None, message: str | None) -> None:
                await super().report_progress(progress, total, message)
                if progress == 100:
                    raise RuntimeError("progress notification unavailable")

        context = FailingFinalContext()
        bridge = ScriptedBridge([
            state(),
            state(assistant_count=1, latest_text="bound final", user_count=1, latest_user_text="prompt"),
            state(assistant_count=1, latest_text="bound final", user_count=1, latest_user_text="prompt"),
            state(assistant_count=1, latest_text="bound final", user_count=1, latest_user_text="prompt"),
        ])
        with patch.dict(server.BRIDGES, {"chatgpt": bridge}):
            result = await server.ask_chatgpt("prompt", ctx=context)  # type: ignore[arg-type]
            recovered = await server.ask_chatgpt(action="last_response")

        self.assertEqual(context.events[-1][0], 100)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response"], "bound final")
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["response"], "bound final")
        self.assertEqual(bridge.submits, 1)

    async def test_ask_chatgpt_forwards_safe_progress_and_completed_result(self) -> None:
        context = _CapturingContext()
        bridge = _ProgressReportingBridge("ChatGPT")
        with patch.dict(server.BRIDGES, {"chatgpt": bridge}):
            result = await server.ask_chatgpt("review this", ctx=context)  # type: ignore[arg-type]

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message"], "ChatGPT has finished and the final response is ready.")
        self.assertEqual(
            context.events,
            [
                (35, 100, "Question sent. Waiting for ChatGPT to start responding."),
                (65, 100, "ChatGPT is generating its response.\n\nLatest visible response tail:\npartial context"),
                (100, 100, "ChatGPT has finished and the final response is ready."),
            ],
        )
        self.assertEqual(bridge.calls[0][0], "ask")

    async def test_each_tool_routes_to_its_own_provider_bridge(self) -> None:
        chatgpt = _ProgressReportingBridge("ChatGPT")
        gemini = _ProgressReportingBridge("Gemini")
        claude = _ProgressReportingBridge("Claude")
        with patch.dict(
            server.BRIDGES,
            {"chatgpt": chatgpt, "gemini": gemini, "claude": claude},
            clear=True,
        ):
            self.assertEqual((await server.ask_chatgpt(action="status"))["provider"], "chatgpt")
            self.assertEqual((await server.ask_gemini(action="status"))["provider"], "gemini")
            self.assertEqual((await server.ask_claude(action="status"))["provider"], "claude")

        self.assertEqual(chatgpt.calls, [("status", None)])
        self.assertEqual(gemini.calls, [("status", None)])
        self.assertEqual(claude.calls, [("status", None)])

    async def test_single_tool_surface_keeps_timeout_recovery_and_close_actions(self) -> None:
        bridge = _ProgressReportingBridge("Gemini")
        with patch.dict(server.BRIDGES, {"gemini": bridge}):
            recovered = await server.ask_gemini(action="last_response")
            closed = await server.ask_gemini(action="close")

        self.assertEqual(recovered["response"], "recovered")
        self.assertEqual(closed, {"ok": True, "provider": "gemini", "status": "closed"})
        self.assertEqual(bridge.calls, [("last_response", None), ("close", None)])


if __name__ == "__main__":
    unittest.main()
