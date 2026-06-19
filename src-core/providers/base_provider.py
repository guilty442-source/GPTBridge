from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

from playwright.async_api import Page

from managers.browser_session import BrowserSessionManager


class SessionStatusEnum(str, Enum):
    UNOPENED = "UNOPENED"
    CLOSED = "CLOSED"
    AUTHENTICATED = "AUTHENTICATED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    RATE_LIMITED = "RATE_LIMITED"
    WARNING = "WARNING"
    ERROR = "ERROR"


class BaseAIProvider(ABC):
    def __init__(
        self,
        provider_name: str,
        session_manager: BrowserSessionManager
    ) -> None:
        self.provider_name = provider_name
        self.session_manager = session_manager
        self.page: Optional[Page] = None
        self.current_account = ""
        self.last_dispatch_error = ""

    @abstractmethod
    async def initialize_session(self) -> bool:
        ...

    @abstractmethod
    async def check_session_health(self) -> SessionStatusEnum:
        ...

    @abstractmethod
    async def dispatch_prompt(self, prompt_text: str) -> bool:
        ...

    @abstractmethod
    async def capture_response(self) -> str:
        ...

    @abstractmethod
    async def _capture_response_impl(self) -> str:
        ...

    async def wait_until_idle(self, timeout_seconds: int = 120, log_reporter: Any = None) -> bool:
        """Wait until the AI response text stops changing (stable for 3s or halted for 15s)."""
        if self.page is None:
            return False
            
        last_length = -1
        stable_count = 0
        max_length = -1
        last_increase_sec = 0
        
        provider_name = self.provider_name.capitalize()
        
        for sec in range(timeout_seconds):
            try:
                # 呼叫子類別實作的具體抓取方法
                text = await self._capture_response_impl()
                current_length = len(text)
                
                if sec > 0 and sec % 5 == 0:
                    print(f"[{provider_name}] Generating... current length: {current_length} chars")

                if current_length > max_length:
                    max_length = current_length
                    last_increase_sec = sec

                if current_length > 0 and current_length == last_length:
                    stable_count += 1
                    if stable_count >= 3:
                        return True
                else:
                    stable_count = 0
                    last_length = current_length

                if sec - last_increase_sec >= 15:
                    msg = f"[{provider_name}] wait_until_idle forced stop: no length increase for 15s"
                    print(msg)
                    if log_reporter:
                        try:
                            await log_reporter(f"[Developer] {msg}")
                        except Exception:
                            pass
                    return True

            except Exception:
                pass
            await self.page.wait_for_timeout(1000)
            
        print(f"[{provider_name}] wait_until_idle timeout ({timeout_seconds}s)")
        return False

    @abstractmethod
    async def recover(self) -> bool:
        ...

    async def detect_account_label(self) -> str:
        return self.current_account
