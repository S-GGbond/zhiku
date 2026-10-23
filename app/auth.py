"""账号登录：单账号 + 密码，签名 Cookie 会话；API Token 仍保留给 Agent 用。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

ROUNDS = 200_000
COOKIE = "kb_session"
SESSION_TTL = 7 * 24 * 3600
MIN_PASSWORD = 6


class AuthError(ValueError):
    """账号相关错误，消息可直接展示给用户。"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash(password: str, salt: str, rounds: int = ROUNDS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds).hex()


class Auth:
    def __init__(self, db):
        self.db = db
        secret = db.get_setting("auth_secret")
        if not secret:
            secret = secrets.token_hex(32)
            db.set_setting("auth_secret", secret)
        self._secret = secret.encode("utf-8")

    # ---------- 账号 ----------
    def account(self) -> dict[str, Any] | None:
        data = self.db.get_setting("account")
        return data if isinstance(data, dict) and data.get("username") else None

    def initialized(self) -> bool:
        return self.account() is not None

    def username(self) -> str:
        return (self.account() or {}).get("username", "")

    def create_account(self, username: str, password: str) -> None:
        if self.initialized():
            raise AuthError("账号已存在，请直接登录")
        username = (username or "").strip()
        if not username:
            raise AuthError("用户名不能为空")
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"密码至少 {MIN_PASSWORD} 位")
        salt = secrets.token_hex(16)
        self.db.set_setting("account", {
            "username": username,
            "salt": salt,
            "hash": _hash(password, salt),
            "rounds": ROUNDS,
            "updated_at": time.time(),
        })

    def verify(self, username: str, password: str) -> bool:
        account = self.account()
        if not account:
            return False
        if not hmac.compare_digest(username or "", account["username"]):
            return False
        digest = _hash(password or "", account["salt"], int(account.get("rounds") or ROUNDS))
        return hmac.compare_digest(digest, account["hash"])

    def set_password(self, username: str, password: str) -> None:
        """本地重置密码（不需要原密码，供 CLI 使用），同时让所有旧会话失效。"""
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"密码至少 {MIN_PASSWORD} 位")
        salt = secrets.token_hex(16)
        self.db.set_setting("account", {
            "username": (username or "admin").strip(),
            "salt": salt,
            "hash": _hash(password, salt),
            "rounds": ROUNDS,
            "updated_at": time.time(),
        })

    def change_password(self, old: str, new: str) -> None:
        account = self.account()
        if not account:
            raise AuthError("还没有创建账号")
        if not self.verify(account["username"], old):
            raise AuthError("原密码不正确")
        if len(new or "") < MIN_PASSWORD:
            raise AuthError(f"新密码至少 {MIN_PASSWORD} 位")
        if new == old:
            raise AuthError("新密码不能与原密码相同")
        salt = secrets.token_hex(16)
        account.update({"salt": salt, "hash": _hash(new, salt), "rounds": ROUNDS, "updated_at": time.time()})
        self.db.set_setting("account", account)

    # ---------- 会话 ----------
    def _fingerprint(self) -> str:
        return (self.account() or {}).get("hash", "")[:16]

    def issue(self) -> str:
        """登录成功后签发 Cookie 值；改密后指纹变化，旧会话自动失效。"""
        payload = {"u": self.username(), "exp": time.time() + SESSION_TTL, "fp": self._fingerprint()}
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = _b64(hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).digest())
        return f"{raw}.{sig}"

    def check(self, token: str | None) -> str | None:
        """校验 Cookie，返回用户名；不合法返回 None。"""
        if not token or "." not in token:
            return None
        raw, _, sig = token.partition(".")
        expect = _b64(hmac.new(self._secret, raw.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            payload = json.loads(_unb64(raw))
        except (ValueError, json.JSONDecodeError):
            return None
        if float(payload.get("exp", 0)) < time.time():
            return None
        if not hmac.compare_digest(str(payload.get("fp", "")), self._fingerprint()):
            return None
        return str(payload.get("u") or "")
