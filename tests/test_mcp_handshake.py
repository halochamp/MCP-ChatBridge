from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_DIR = Path(__file__).resolve().parents[1]


class MCPHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_progress_does_not_replace_terminal_tool_result(self) -> None:
        env = dict(os.environ)
        env["BROWSER_USE_CONFIG_DIR"] = "/tmp/mcp-chatbridge-test-browseruse-config"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        harness = f"""
import sys
sys.path.insert(0, {str(PROJECT_DIR)!r})
sys.path.insert(0, {str(PROJECT_DIR / 'tests')!r})
import server
from test_chatgpt_web import ScriptedBridge, state
server.BRIDGES['chatgpt'] = ScriptedBridge([
    state(),
    state(assistant_count=1, latest_text='bound final', user_count=1, latest_user_text='prompt'),
    state(assistant_count=1, latest_text='bound final', user_count=1, latest_user_text='prompt'),
    state(assistant_count=1, latest_text='bound final', user_count=1, latest_user_text='prompt'),
])
class IncompleteBridge:
    async def ask(self, prompt, *, progress=None, **kwargs):
        await progress(65, 'Latest visible response tail: ' + 'y' * 1200)
        return {{
            'ok': False, 'provider': 'gemini', 'status': 'timed_out',
            'response': 'prefix-' + 'y' * 1800 + '-suffix',
            'truncated': False, 'error_type': 'GenerationTimeoutError',
        }}
    async def close(self):
        pass
server.BRIDGES['gemini'] = IncompleteBridge()
server.mcp.run()
"""
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", harness],
            cwd=str(PROJECT_DIR),
            env=env,
        )
        progress: list[float] = []

        async def on_progress(value: float, total: float | None, message: str | None) -> None:
            progress.append(value)

        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "ask_chatgpt", {"prompt": "prompt"}, progress_callback=on_progress
                )
                self.assertFalse(result.isError)
                self.assertEqual(progress.count(100), 1)
                self.assertTrue(any(value < 100 for value in progress))
                self.assertEqual(result.structuredContent["status"], "completed")
                self.assertEqual(result.structuredContent["response"], "bound final")

                incomplete = await session.call_tool(
                    "ask_gemini", {"prompt": "prompt"}, progress_callback=on_progress
                )
                self.assertFalse(incomplete.isError)
                self.assertFalse(incomplete.structuredContent["ok"])
                self.assertEqual(incomplete.structuredContent["status"], "timed_out")
                self.assertEqual(incomplete.structuredContent["provider"], "gemini")
                self.assertEqual(incomplete.structuredContent["response"], "prefix-" + "y" * 1800 + "-suffix")

    async def test_server_lists_three_provider_tools(self) -> None:
        env = dict(os.environ)
        env["BROWSER_USE_CONFIG_DIR"] = "/tmp/mcp-chatbridge-test-browseruse-config"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_DIR / "server.py")],
            cwd=str(PROJECT_DIR),
            env=env,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.serverInfo.name, "MCP-ChatBridge")
                result = await session.list_tools()
                self.assertEqual(
                    {tool.name for tool in result.tools},
                    {"ask_chatgpt", "ask_gemini", "ask_claude"},
                )

                descriptions = {
                    tool.name: " ".join((tool.description or "").lower().split())
                    for tool in result.tools
                }
                for tool in result.tools:
                    props = tool.inputSchema["properties"]
                    self.assertEqual(props["action"]["default"], "ask")
                    expected_actions = {"ask", "status", "last_response", "new_chat", "close"}
                    if tool.name in ("ask_gemini", "ask_chatgpt"):
                        # Image generation (and attachments) live behind the action argument of the
                        # providers that support them; Claude Web has neither yet.
                        expected_actions = expected_actions | {"generate_image"}
                        self.assertEqual(props["save_dir"]["default"], "")
                        self.assertEqual(props["max_images"]["default"], 4)
                        self.assertIn("attach_images", props)
                    else:
                        self.assertNotIn("save_dir", props)
                        self.assertNotIn("max_images", props)
                        self.assertNotIn("attach_images", props)
                    self.assertEqual(set(props["action"]["enum"]), expected_actions)
                    self.assertEqual(props["timeout_seconds"]["default"], 600)
                    self.assertNotIn("strict_user_turn_text", props)
                    self.assertIn("last_response", descriptions[tool.name])
                    self.assertIn('status="completed"', descriptions[tool.name])

                self.assertIn("actual code/diff inline", descriptions["ask_chatgpt"])
                self.assertIn("without resending", descriptions["ask_chatgpt"])
                self.assertIn("manual completion", descriptions["ask_gemini"])
                self.assertIn("generate_image", descriptions["ask_gemini"])
                self.assertIn("generate_image", descriptions["ask_chatgpt"])
                self.assertIn("free chatgpt accounts can create only a few images per day", descriptions["ask_chatgpt"])
                self.assertNotIn("generate_image", descriptions["ask_claude"])
                self.assertIn("manual completion", descriptions["ask_claude"])

                for provider in ("chatgpt", "gemini", "claude"):
                    close_result = await session.call_tool(f"ask_{provider}", {"action": "close"})
                    self.assertFalse(close_result.isError)
                    self.assertEqual(
                        close_result.structuredContent,
                        {"ok": True, "provider": provider, "status": "closed"},
                    )


if __name__ == "__main__":
    unittest.main()
