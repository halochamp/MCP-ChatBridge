#!/usr/bin/env python3
"""MCP entry point for the MCP-ChatBridge multi-provider browser bridge."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator, Literal

os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "critical")
os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")

from mcp.server.fastmcp import Context, FastMCP

from common_bridge import ChatGPTWebError
from providers import ChatGPTBridge, ClaudeBridge, GeminiBridge


chatgpt_bridge = ChatGPTBridge()
gemini_bridge = GeminiBridge()
claude_bridge = ClaudeBridge()
BRIDGES = {
    "chatgpt": chatgpt_bridge,
    "gemini": gemini_bridge,
    "claude": claude_bridge,
}


@asynccontextmanager
async def app_lifespan(_: FastMCP) -> AsyncIterator[dict[str, object]]:
    try:
        yield {}
    finally:
        for bridge in BRIDGES.values():
            with suppress(Exception):
                await bridge.close()


mcp = FastMCP(
    "MCP-ChatBridge",
    instructions=(
        "Deterministic read-only advisor bridge to ChatGPT, Gemini, and Claude Web. "
        "Use the matching ask_* tool only when the user asks for it via ChatBridge or names the provider "
        "(plain 'via ChatBridge' means ask_gemini); a plain advisor request is not a ChatBridge request. "
        "Image generation uses ask_gemini action=generate_image. Each provider owns a separate visible Chrome profile. "
        "The first use may require manual sign-in in that provider's opened browser window."
    ),
    lifespan=app_lifespan,
)


Action = Literal["ask", "status", "last_response", "new_chat", "close"]
# Image generation is a Gemini-only lifecycle operation behind ask_gemini's action argument.
GeminiAction = Literal["ask", "status", "last_response", "new_chat", "close", "generate_image"]
# ChatGPT also generates images (free accounts have a small daily limit) but has no attachments yet.
ChatGPTAction = Literal["ask", "status", "last_response", "new_chat", "close", "generate_image"]
IMAGE_PROVIDERS = ("gemini", "chatgpt")


def _error_result(provider: str, exc: Exception) -> dict[str, object]:
    return {
        "ok": False,
        "provider": provider,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }


def _internal_error_result(provider: str) -> dict[str, object]:
    return {
        "ok": False,
        "provider": provider,
        "error": "Unexpected internal bridge failure.",
        "error_type": "InternalBridgeError",
    }


async def _run_provider(
    provider: str,
    *,
    action: str,
    prompt: str,
    new_chat: bool,
    timeout_seconds: int,
    ctx: Context | None,
    save_dir: str | None = None,
    max_images: int = 4,
    attach_images: list[str] | None = None,
) -> dict[str, object]:
    bridge = BRIDGES[provider]
    try:
        if action == "status":
            return await bridge.status()
        if action == "last_response":
            return await bridge.last_response()
        if action == "new_chat":
            return await bridge.new_chat()
        if action == "close":
            await bridge.close()
            return {"ok": True, "provider": provider, "status": "closed"}
        if action not in ("ask", "generate_image") or (action == "generate_image" and provider not in IMAGE_PROVIDERS):
            return {
                "ok": False,
                "provider": provider,
                "error": f"Unsupported action: {action}",
                "error_type": "InvalidActionError",
            }

        async def report(progress: float, message: str) -> None:
            if ctx is not None:
                await ctx.report_progress(progress, 100, message)

        if action == "generate_image":
            return await bridge.generate_image(
                prompt,
                save_dir=save_dir or None,
                max_images=max_images,
                new_chat=new_chat,
                timeout_seconds=timeout_seconds,
                progress=report,
                attach_images=attach_images or None,
            )
        if attach_images:
            return await bridge.ask(
                prompt,
                new_chat=new_chat,
                timeout_seconds=timeout_seconds,
                progress=report,
                attach_images=attach_images,
            )
        return await bridge.ask(
            prompt,
            new_chat=new_chat,
            timeout_seconds=timeout_seconds,
            progress=report,
        )
    except ChatGPTWebError as exc:
        return _error_result(provider, exc)
    except Exception:
        return _internal_error_result(provider)


@mcp.tool()
async def ask_chatgpt(
    prompt: str = "",
    action: ChatGPTAction = "ask",
    new_chat: bool = True,
    timeout_seconds: int = 600,
    save_dir: str = "",
    max_images: int = 4,
    attach_images: list[str] | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Consult ChatGPT Web through its deterministic dedicated browser profile.

    Normally use ``action="status"`` once to open/check the profile, then
    ``action="ask"`` with the complete prompt. Accept only ``ok=true`` with
    ``status="completed"`` as final advice. Progress tails are context only.
    After an explicit timeout, use ``action="last_response"`` without resending
    the prompt; after an ambiguous non-timeout failure, do not retry unless the
    user explicitly directs a new attempt. ``action="new_chat"`` opens a fresh
    conversation and ``action="close"`` closes only ChatGPT's browser session.

    For code review, include the actual code/diff inline. Paths and anchors are
    provenance only; do not ask the advisor to fetch omitted local code.

    ``action="generate_image"`` sends ``prompt`` (phrase it as an explicit image
    request) and saves the images ChatGPT produced to ``save_dir`` (absolute path
    or ``~/...``; default ``~/Pictures/MCP-ChatBridge/chatgpt``; must be outside
    this repository), at most ``max_images`` (1-8). Same result contract as
    ``ask_gemini``: ``ok=true`` returns ``images`` metadata, ``status="no_image"``
    means no image came back (for example the free daily limit); never resend
    automatically. Free ChatGPT accounts can create only a few images per day, so
    do not use this for casual tests, and prefer ``ask_gemini`` unless the user
    names ChatGPT.

    ``attach_images`` (``ask`` and ``generate_image``) attaches up to 4 local PNG,
    JPEG, or WEBP files (absolute or ``~/`` paths, 16 MB each) before the prompt
    is sent. The files are uploaded to OpenAI, and free accounts have a small
    daily upload limit: attach only images the user asked you to send. A bad path
    or failed upload stops the call before anything is submitted.
    """
    return await _run_provider(
        "chatgpt",
        action=action,
        prompt=prompt,
        new_chat=new_chat,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
        save_dir=save_dir,
        max_images=max_images,
        attach_images=attach_images,
    )


@mcp.tool()
async def ask_gemini(
    prompt: str = "",
    action: GeminiAction = "ask",
    new_chat: bool = True,
    timeout_seconds: int = 600,
    save_dir: str = "",
    max_images: int = 4,
    attach_images: list[str] | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Consult Gemini Web through its deterministic dedicated browser profile.

    Normally use ``action="status"`` once to open/check the profile, then
    ``action="ask"`` with the complete prompt. Accept only ``ok=true`` with
    ``status="completed"`` as final advice. After an explicit timeout, use
    ``action="last_response"`` without resending. Human verification or login
    gates require manual completion in the opened browser. ``new_chat`` and all
    response ownership checks use the same fail-closed lifecycle as ChatGPT.

    ``action="generate_image"`` sends ``prompt`` (phrase it as an explicit image
    request) and saves the images Gemini produced to ``save_dir`` (absolute path
    or ``~/...``; default ``~/Pictures/MCP-ChatBridge/gemini``; must be outside this
    repository). Up to ``max_images`` (1-8) are saved. ``ok=true`` returns
    ``images`` with path/mime/bytes/sha256 and never the image bytes. Without an
    image the result is ``ok=false`` with ``status="no_image"`` and the text
    response; do not resend automatically. Images are saved at full size when the
    page's download control works (``source="full_download"``), otherwise at the
    displayed size.

    ``attach_images`` (``ask`` and ``generate_image``) attaches up to 4 local PNG,
    JPEG, or WEBP files (absolute or ``~/`` paths, 16 MB each) before the prompt is
    sent, so Gemini can describe or edit them. The files are uploaded to Google:
    attach only images the user asked you to send. A bad path or failed upload
    stops the call before anything is submitted.
    """
    return await _run_provider(
        "gemini",
        action=action,
        prompt=prompt,
        new_chat=new_chat,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
        save_dir=save_dir,
        max_images=max_images,
        attach_images=attach_images,
    )


@mcp.tool()
async def ask_claude(
    prompt: str = "",
    action: Action = "ask",
    new_chat: bool = True,
    timeout_seconds: int = 600,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Consult Claude Web through its deterministic dedicated browser profile.

    Normally use ``action="status"`` once to open/check the profile, then
    ``action="ask"`` with the complete prompt. Accept only ``ok=true`` with
    ``status="completed"`` as final advice. After an explicit timeout, use
    ``action="last_response"`` without resending. Human verification or login
    gates require manual completion in the opened browser. ``new_chat`` and all
    response ownership checks use the same fail-closed lifecycle as ChatGPT.
    """
    return await _run_provider(
        "claude",
        action=action,
        prompt=prompt,
        new_chat=new_chat,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
