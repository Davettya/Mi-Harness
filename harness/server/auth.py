from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from harness.core import HarnessError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthService:
    """Only hashes enter storage. Raw pairing and session credentials never enter logs."""

    def __init__(self, store: Any, owner_id: str = "local", session_ttl: int = 86400):
        self.store, self.owner_id, self.session_ttl = store, owner_id, session_ttl
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def issue_ticket(self, ttl_seconds: int = 300) -> str:
        ticket = secrets.token_urlsafe(24)
        self.store.put("auth_tickets", digest(ticket), {
            "owner_id": self.owner_id, "expires_at": time.time() + ttl_seconds, "consumed": False,
        })
        return ticket

    def _admit(self, peer: str):
        now = time.time()
        attempts = self._attempts[peer]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HarnessError("PAIRING_RATE_LIMIT", "配对尝试过于频繁，请稍后重试", 429)
        attempts.append(now)
        return now

    def _create_session(self, owner_id: str, now: float, source: str) -> tuple[str, str]:
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.put(
            "auth_sessions",
            digest(session),
            {
                "owner_id": owner_id,
                "csrf_hash": digest(csrf),
                "expires_at": now + self.session_ttl,
                "revoked": False,
                "source": source,
            },
        )
        return session, csrf

    def exchange(self, ticket: str, peer: str) -> tuple[str, str]:
        now = self._admit(peer)
        with self.store.transaction():
            record = self.store.get("auth_tickets", digest(ticket))
            if not record or record["consumed"] or record["expires_at"] <= now:
                raise HarnessError("PAIRING_INVALID", "配对口令无效、已使用或已过期", 401)
            self.store.put("auth_tickets", digest(ticket), {**record, "consumed": True})
            return self._create_session(record["owner_id"], now, "ticket_exchange")

    def exchange_local(self, peer: str) -> tuple[str, str]:
        """Create a session only for a same-origin request received from loopback."""
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise HarnessError(
                "LOCAL_PAIRING_DENIED",
                "自动连接只允许本机回环地址",
                403,
            )
        now = self._admit(peer)
        with self.store.transaction():
            return self._create_session(self.owner_id, now, "loopback_auto")

    def authenticate(self, token: str | None) -> dict:
        record = self.store.get("auth_sessions", digest(token)) if token else None
        if not record or record.get("revoked") or record["expires_at"] <= time.time():
            raise HarnessError("AUTH_REQUIRED", "本机会话不存在或已过期", 401)
        return record

    @staticmethod
    def check_csrf(record: dict, token: str | None):
        if not token or not hmac.compare_digest(record["csrf_hash"], digest(token)):
            raise HarnessError("CSRF_REJECTED", "请求校验失败，请刷新工作台后重试", 403)

    def logout(self, token: str):
        with self.store.transaction():
            record = self.authenticate(token)
            self.store.put("auth_sessions", digest(token), {**record, "revoked": True})
