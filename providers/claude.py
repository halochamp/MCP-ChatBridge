"""Claude provider adapter."""

from pathlib import Path

from common_bridge import GOOGLE_SIGNIN_HOSTS, CommonBrowserBridge, ProviderConfig


class ClaudeBridge(CommonBrowserBridge):
    CONFIG = ProviderConfig(
        key="claude",
        name="Claude",
        home_url="https://claude.ai/new",
        profile_env="CODEX_CLAUDE_PROFILE_DIR",
        default_profile_dir=Path("~/.codex-claude/browser-profile").expanduser(),
        # Google sign-in is an approved AUTHENTICATION host only (same pattern as Gemini): the page may
        # open there so a sign-in that uses Google can finish inside the bridge-controlled window, but
        # it is not an interactive root, so the bridge never mutates the page and reports a login gate.
        # Without it the sign-in window was blocked and kept reopening (seen live on 2026-10-31).
        allowed_domains=("claude.ai", "*.claude.ai", "auth.anthropic.com", *GOOGLE_SIGNIN_HOSTS),
        interactive_roots=("claude.ai",),
        auth_roots=("auth.anthropic.com", *GOOGLE_SIGNIN_HOSTS),
        composer_selectors=(
            'div.ProseMirror[contenteditable="true"]',
            '[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-placeholder]',
        ),
        assistant_selectors=(
            '[data-testid="assistant-message"]',
            '[data-testid^="assistant-message-"]',
            '[data-is-streaming]',
        ),
        user_selectors=(
            '[data-testid="user-message"]',
            '[data-testid^="user-message-"]',
        ),
        user_content_selectors=(
            'p.whitespace-pre-wrap',
            '.font-user-message p.whitespace-pre-wrap',
        ),
        send_selectors=(
            'button[data-testid="send-button"]',
            'button[aria-label="Send message"]',
            'button[aria-label*="Send message" i]',
            'button[aria-label="Send" i]',
            'form:has(div.ProseMirror[contenteditable="true"]) button[type="submit"]',
        ),
        stop_selectors=(
            '[data-is-streaming="true"]',
            'button[aria-label="Stop response"]',
            'button[aria-label="Stop generating"]',
        ),
        login_selectors=(
            'a[href*="login"]',
            'button[aria-label*="Log in" i]',
            'button[aria-label*="Sign in" i]',
        ),
        login_labels=("log in", "login", "sign in", "เข้าสู่ระบบ"),
        challenge_selectors=(
            'iframe[src*="captcha"]',
            'iframe[src*="challenge"]',
            '#challenge-stage',
            '[id^="cf-chl"]',
            'form[action*="challenge"]',
        ),
        challenge_titles=("just a moment...", "attention required!"),
        conversation_path_prefixes=("/chat/",),
        assistant_content_selectors=(
            '.font-claude-response',
            '.font-claude-response .prose',
            '.prose',
        ),
        native_editor_input=True,
        submit_with_enter=True,
        # Transcript mode: the page DOM drifts, so read the whole conversation text instead of
        # counting per-turn nodes (user decision for ChatGPT, Gemini and Claude alike).
        transcript_mode=True,
        transcript_root_selectors=("main", "body"),
    )


__all__ = ["ClaudeBridge"]
