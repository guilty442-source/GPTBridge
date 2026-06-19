import asyncio
from typing import Any


def _raw_provider(provider: Any) -> Any:
    return getattr(provider, "_provider", provider)


def _page_is_open(page: Any) -> bool:
    try:
        return page is not None and not page.is_closed()
    except Exception:
        return False


async def _check_if_open(app: Any, provider_name: str, provider: Any) -> str:
    raw_provider = _raw_provider(provider)
    page = getattr(raw_provider, "page", None)
    session_page = getattr(app.session, f"{provider_name}_page", None)

    if not _page_is_open(page) and _page_is_open(session_page):
        raw_provider.page = session_page
        page = session_page

    if not _page_is_open(page):
        return "UNOPENED"

    status = await raw_provider.check_session_health()
    status_value = getattr(status, "value", str(status))
    if hasattr(app.session, "health_state"):
        app.session.health_state[provider_name] = status_value
    return status_value


async def monitor_provider_health(app: Any) -> None:
    while True:
        chatgpt_status = "UNOPENED"
        gemini_status = "UNOPENED"
        try:
            # Monitor only pages the user already opened. It must not create or restore tabs.
            if app.session and getattr(app.session, "is_initialized", False):
                if app.chatgpt:
                    chatgpt_status = await _check_if_open(app, "chatgpt", app.chatgpt)
                if app.gemini:
                    gemini_status = await _check_if_open(app, "gemini", app.gemini)

            if app.history_manager:
                app.history_manager.record(
                    f"Provider monitor: ChatGPT={chatgpt_status}, Gemini={gemini_status}"
                )
        except Exception as monitor_error:
            if app.history_manager:
                app.history_manager.record(f"Provider monitor error: {monitor_error}")
        await asyncio.sleep(30)
