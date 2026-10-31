"""Gemini provider adapter."""

from pathlib import Path

from common_bridge import GOOGLE_SIGNIN_HOSTS, CommonBrowserBridge, ProviderConfig


class GeminiBridge(CommonBrowserBridge):
    CONFIG = ProviderConfig(
        key="gemini",
        name="Gemini",
        home_url="https://gemini.google.com/app",
        profile_env="CODEX_GEMINI_PROFILE_DIR",
        default_profile_dir=Path("~/.codex-gemini/browser-profile").expanduser(),
        allowed_domains=("gemini.google.com", "*.gemini.google.com", *GOOGLE_SIGNIN_HOSTS),
        interactive_roots=("gemini.google.com",),
        auth_roots=GOOGLE_SIGNIN_HOSTS,
        composer_selectors=(
            'rich-textarea .ql-editor',
            'rich-textarea [contenteditable="true"]',
            'div.ql-editor[contenteditable="true"]',
            '[aria-label="Enter a prompt here"]',
            '[contenteditable="true"][role="textbox"]',
        ),
        assistant_selectors=(
            'model-response',
            '[data-test-id="model-response"]',
            '.model-response',
            '[data-message-author="assistant"]',
        ),
        user_selectors=(
            'user-query',
            '[data-test-id="user-query"]',
            '.user-query',
            '[data-message-author="user"]',
        ),
        user_content_selectors=(
            'p.query-text-line',
            '.query-text p.query-text-line',
            '[data-test-id="query-text"]',
        ),
        send_selectors=(
            'button[aria-label="Send message"]',
            'button[aria-label*="Send" i]',
            'button[mattooltip*="Send" i]',
            'button.send-button',
            '.send-button button',
            # Icon names are identical in every UI language (aria-labels are localized).
            'button:has(mat-icon[fonticon="arrow_upward"])',
            'button:has(mat-icon[data-mat-icon-name="arrow_upward"])',
        ),
        stop_selectors=(
            'button[aria-label="Stop response"]',
            'button[aria-label*="Stop response" i]',
            'button:has(mat-icon[fonticon="stop"])',
            'button:has(mat-icon[data-mat-icon-name="stop"])',
        ),
        login_selectors=(
            'a[href*="accounts.google.com"]',
            'a[href*="signin"]',
            'button[aria-label*="Sign in" i]',
        ),
        login_labels=("sign in", "เข้าสู่ระบบ"),
        challenge_selectors=(
            'iframe[src*="captcha"]',
            'iframe[src*="challenge"]',
            'form[action*="challenge"]',
            '[id*="captcha"]',
        ),
        challenge_titles=("verify it’s you", "verify it's you", "are you a robot?"),
        conversation_path_prefixes=("/app/",),
        assistant_content_selectors=(
            'structured-content-container.model-response-text',
            '.model-response-text',
        ),
        native_editor_input=True,
        submit_with_enter=True,
        submit_timeout_seconds=120.0,
        # Verified live: the "Upload & tools" button has the `plus` icon; the `image/*` file input
        # exists only after that menu opens; the generated image has a `download` icon button.
        attach_trigger_icons=("plus",),
        attach_input_selectors=('input[type="file"][accept*="image/*"]', 'input[type="file"]'),
        download_button_icons=("download",),
        # Transcript mode: the page DOM drifts, so read the whole conversation text instead of
        # counting per-turn nodes (user decision for ChatGPT, Gemini and Claude alike).
        transcript_mode=True,
        transcript_root_selectors=("main", "body"),
    )


__all__ = ["GeminiBridge"]
