"""Gemini client used by the planner (translation only).

Proxy: GEMINI_PROXY in the .env file wins (e.g. http://127.0.0.1:10808, or "none" for direct).
Without it a local v2rayN proxy on 127.0.0.1:10808 is used when it is running, else direct.
"""
import os
import socket

from google import genai
from google.genai import types  # noqa: F401  (the planner reads ai_service.types)

_LOCAL_PROXY = "http://127.0.0.1:10808"


def gemini_proxy() -> str:
    configured = str(os.getenv("GEMINI_PROXY") or "").strip()
    if configured:
        return "" if configured.lower() in {"none", "off", "direct", "0"} else configured
    try:
        with socket.create_connection(("127.0.0.1", 10808), timeout=0.2):
            return _LOCAL_PROXY
    except OSError:
        return ""


class AIService:
    def __init__(self):
        self.api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not self.api_key:
            try:
                from app.services.key_rotation import collect_gemini_key_pool
                pool = collect_gemini_key_pool()
                if pool:
                    self.api_key = pool[0][1]
            except Exception:
                pass
        if not self.api_key:
            raise ValueError("⚠️ GEMINI_API_KEY မတွေ့ပါ! .env ဖိုင်ကို စစ်ဆေးပါ။")
        self.proxy = gemini_proxy()
        if self.proxy:
            try:
                http_options = types.HttpOptions(
                    client_args={"proxy": self.proxy},
                    async_client_args={"proxy": self.proxy},
                )
                self.client = genai.Client(api_key=self.api_key, http_options=http_options)
            except Exception:
                self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = genai.Client(api_key=self.api_key)
        self.text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.6-flash")

    def _enable_proxy(self):
        # Thread-safe no-op: proxy is bound per client via HttpOptions instead of global os.environ mutation.
        return None

    def _disable_proxy(self):
        return None
