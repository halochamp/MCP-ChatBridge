"""ChatGPT provider adapter."""

from pathlib import Path

from common_bridge import (
    APPLE_SIGNIN_HOSTS,
    GOOGLE_SIGNIN_HOSTS,
    MICROSOFT_SIGNIN_HOSTS,
    CommonBrowserBridge,
    ProviderConfig,
)
from image_store import CHATGPT_IMAGE_HOST_ROOTS


class ChatGPTBridge(CommonBrowserBridge):
    CONFIG = ProviderConfig(
        key="chatgpt",
        name="ChatGPT",
        home_url="https://chatgpt.com/",
        profile_env="CODEX_CHATGPT_PROFILE_DIR",
        default_profile_dir=Path("~/.codex-chatgpt/browser-profile").expanduser(),
        # ChatGPT signs in through auth.openai.com, which can hand off to Google, Apple or Microsoft.
        # Those are authentication hosts only (never interactive roots), so a future sign-in can
        # finish inside the bridge-controlled window without the bridge touching those pages.
        allowed_domains=(
            "chatgpt.com",
            "*.chatgpt.com",
            "auth.openai.com",
            "*.openai.com",
            *GOOGLE_SIGNIN_HOSTS,
            *APPLE_SIGNIN_HOSTS,
            *MICROSOFT_SIGNIN_HOSTS,
        ),
        interactive_roots=("chatgpt.com",),
        auth_roots=("auth.openai.com", *GOOGLE_SIGNIN_HOSTS, *APPLE_SIGNIN_HOSTS, *MICROSOFT_SIGNIN_HOSTS),
        composer_selectors=(
            '#prompt-textarea',
            'textarea[data-testid="prompt-textarea"]',
            'textarea[name="prompt-textarea"]',
            'div[contenteditable="true"][data-virtualkeyboard="true"]',
        ),
        assistant_selectors=(
            '[data-message-author-role="assistant"]',
            '[data-role="assistant"]',
            '[data-message-author="assistant"]',
        ),
        user_selectors=(
            '[data-message-author-role="user"]',
            '[data-role="user"]',
            '[data-message-author="user"]',
        ),
        user_content_selectors=(
            '[data-testid="collapsible-user-message-content"]',
            '.whitespace-pre-wrap',
        ),
        send_selectors=(
            'button[data-testid="send-button"]',
            '#composer-submit-button',
            'button[aria-label="Send prompt"]',
            'button[aria-label="Send"]',
            'button[aria-label="ส่งข้อความ"]',
            'form:has(div[contenteditable="true"][data-virtualkeyboard="true"]) button[type="submit"]',
        ),
        stop_selectors=(
            'button[data-testid="stop-button"]',
            'button[aria-label="Stop generating"]',
            'button[aria-label="Stop response"]',
            'button[aria-label="หยุดสร้างคำตอบ"]',
        ),
        login_selectors=(
            'header a[href*="login"]',
            'header button[data-testid*="login"]',
            'nav a[href*="login"]',
            '[role="banner"] a[href*="login"]',
            'header a[href*="signup"]',
            'nav a[href*="signup"]',
        ),
        login_labels=("log in", "login", "sign up", "เข้าสู่ระบบ", "สมัคร"),
        challenge_selectors=(
            'iframe[src*="challenge"]',
            'iframe[src*="captcha"]',
            '#challenge-stage',
            '[id^="cf-chl"]',
            'form[action*="challenge"]',
        ),
        challenge_titles=("just a moment...", "attention required!"),
        conversation_path_prefixes=("/c",),
        # A fresh chat first moves to a provisional local route before the real conversation id.
        # Live trace (2026-10-31): / -> /c/local-chatgpt%3A<uuid> -> /c/<uuid>, same document. Without
        # the second prefix the submit check saw an unknown route and failed every ChatGPT request.
        provisional_path_prefixes=("/c/WEB:", "/c/local-chatgpt"),
        # Image generation: collected from the whole conversation page (see transcript mode below).
        # Transcript mode (user decision: the DOM changes often, read the whole chat, noise is fine).
        # Live 2026-10-31: ChatGPT no longer renders <article> or data-message-author-role, so the
        # per-turn counters were always 0 and every ask failed with "no observable lifecycle
        # acceptance signal" although the message was sent and answered.
        transcript_mode=True,
        transcript_root_selectors=("main", "body"),
        image_host_roots=CHATGPT_IMAGE_HOST_ROOTS,
        # Live (read-only look at a real generated image): the inline controls are only "Generated
        # image N" (opens the viewer), "Edit ..." and "Share ..."; there is no inline download button.
        # The image bytes fetched from the page are the full-size file (1678x937 for the test run), so
        # no download control is configured and nothing here is guessed.
        download_button_selectors=(),
        # The user judged 8s too short and asked for two minutes because the service is sometimes slow.
        submit_timeout_seconds=120.0,
        # Verified live: the page keeps four file inputs from load (all inside the composer form), so
        # there is no menu to open. Only the input WITHOUT an accept attribute attaches a file
        # (preview <img> blob: then data:, a progressbar "Uploading ...", a "Remove ..." button);
        # the image/* inputs did nothing (60s timeout). Ids are generated, so never match by id.
        attach_input_selectors=('input[type="file"]:not([accept])',),
        # Live: an immediate set was silently ignored twice; after a 3s settle it attached within 2s.
        attach_settle_seconds=3.0,
        attach_retry_seconds=6.0,
    )


__all__ = ["ChatGPTBridge"]
