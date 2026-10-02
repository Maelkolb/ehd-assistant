"""Request limits for a public deployment.

Per client: a sliding hour window, with a smaller allowance for the premium
model (beyond it the request is served by the standard model instead of being
refused). Globally: a daily request budget and a cap on simultaneous answers.

State lives in this process. Run one API process, or put a shared limiter in
front, otherwise every process grants the full allowance.
"""

from __future__ import annotations

import collections
import hashlib
import secrets
import threading
import time

from fastapi import Request

from alex.config import ModelSpec, Settings

HOUR = 3600


class Refusal(Exception):
    def __init__(self, messages: dict[str, str]):
        super().__init__(messages["en"])
        self.messages = messages


TOO_MANY = {
    "de": "Zu viele Anfragen – bitte später erneut versuchen.",
    "en": "Too many requests – please try again later.",
}
DAILY_SPENT = {
    "de": "Das Tageskontingent von Alex ist aufgebraucht – bitte morgen wiederkommen.",
    "en": "Alex's daily budget is used up – please come back tomorrow.",
}
BUSY = {
    "de": "Alex ist gerade ausgelastet – bitte gleich noch einmal versuchen.",
    "en": "Alex is busy right now – please try again in a moment.",
}


class RateLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.salt = secrets.token_bytes(16)
        self.requests: dict[str, collections.deque] = {}
        self.premium_requests: dict[str, collections.deque] = {}
        self.day = ""
        self.day_count = 0
        self.active = 0

    def client_key(self, request: Request) -> str:
        """The client address as seen by the outermost trusted proxy. With
        PROXY_HOPS=n the address is the n-th from the right of X-Forwarded-For;
        entries further left are supplied by the client and not trusted."""
        address = request.client.host if request.client else "unknown"
        hops = self.settings.proxy_hops
        forwarded = [
            part.strip()
            for part in request.headers.get("x-forwarded-for", "").split(",")
            if part.strip()
        ]
        if hops > 0 and len(forwarded) >= hops:
            address = forwarded[-hops]
        return hashlib.sha256(self.salt + address.encode()).hexdigest()[:16]

    @staticmethod
    def recent(log: dict[str, collections.deque], key: str, now: float) -> collections.deque:
        window = log.setdefault(key, collections.deque())
        while window and window[0] < now - HOUR:
            window.popleft()
        return window

    def admit(self, key: str, spec: ModelSpec) -> ModelSpec:
        """Count the request; return the model that will serve it."""
        now = time.time()
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.day_count = today, 0
            if self.day_count >= self.settings.daily_request_budget:
                raise Refusal(DAILY_SPENT)
            window = self.recent(self.requests, key, now)
            if len(window) >= self.settings.rate_limit_per_hour:
                raise Refusal(TOO_MANY)
            if self.active >= self.settings.max_concurrent_chats:
                raise Refusal(BUSY)
            if spec.tier == "premium":
                premium = self.recent(self.premium_requests, key, now)
                fallback = self.settings.fallback_for_premium
                if len(premium) >= self.settings.rate_limit_premium_per_hour and fallback:
                    spec = fallback
                else:
                    premium.append(now)
            window.append(now)
            self.day_count += 1
            self.active += 1
            if len(self.requests) > 20_000:
                self.forget_idle(now)
        return spec

    def release(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)

    def forget_idle(self, now: float) -> None:
        for log in (self.requests, self.premium_requests):
            for key in [
                key for key, window in log.items() if not window or window[-1] < now - HOUR
            ]:
                del log[key]
