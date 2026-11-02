"""Bounded Feishu REST calls. Neither provider payloads nor credentials appear in errors."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

DOMAIN = "https://open.feishu.cn"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class FeishuError(Exception):
    """A safe error code, without response text, URLs or request headers."""


def identifier(value: str) -> str:
    if not _ID.fullmatch(value):
        raise FeishuError("invalid_identifier")
    return value


class FeishuClient:
    def __init__(
        self, app_id: str, secret: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._app_id, self._secret = app_id, secret
        self._http = httpx.Client(
            base_url=DOMAIN, transport=transport, timeout=60, follow_redirects=False
        )
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        if not response.is_success:
            raise FeishuError(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise FeishuError("invalid_response") from None
        if not isinstance(body, dict):
            raise FeishuError("invalid_response")
        code = body.get("code")
        if code != 0:
            safe = str(code) if isinstance(code, int) else "invalid_response"
            raise FeishuError(safe)
        return body

    def _authorization(self) -> dict[str, str]:
        with self._lock:
            if time.monotonic() >= self._expires:
                body = self._body(
                    self._http.post(
                        "/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self._app_id, "app_secret": self._secret},
                    )
                )
                token, expiry = body.get("tenant_access_token"), body.get("expire")
                if not isinstance(token, str) or not isinstance(expiry, int) or expiry <= 0:
                    raise FeishuError("invalid_token_response")
                self._token = token
                self._expires = time.monotonic() + max(0, expiry - 60)
            return {"Authorization": f"Bearer {self._token}"}

    def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            body = self._body(
                self._http.request(method, path, headers=self._authorization(), **kwargs)
            )
        except httpx.HTTPError:
            raise FeishuError("transport_error") from None
        return body

    def identity(self) -> str:
        body = self.call("GET", "/open-apis/bot/v3/info")
        bot = body.get("bot") or {}
        return identifier(str(bot.get("open_id") or ""))

    def send(
        self, chat: str, root: str, kind: str, content: dict[str, Any], *, uuid: str = ""
    ) -> str:
        fields: dict[str, Any] = {
            "msg_type": kind,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if uuid:
            fields["uuid"] = uuid
        if root != "dm":
            fields["reply_in_thread"] = True
            path = f"/open-apis/im/v1/messages/{identifier(root)}/reply"
            body = self.call("POST", path, json=fields)
        else:
            fields["receive_id"] = identifier(chat)
            body = self.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=fields,
            )
        message = (body.get("data") or {}).get("message_id")
        return identifier(str(message or ""))

    def edit(self, message: str, text: str) -> None:
        self.call(
            "PUT",
            f"/open-apis/im/v1/messages/{identifier(message)}",
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )

    def react(self, message: str, emoji_type: str = "Get") -> None:
        self.call(
            "POST",
            f"/open-apis/im/v1/messages/{identifier(message)}/reactions",
            json={"reaction_type": {"emoji_type": emoji_type}},
        )

    def edit_card(self, message: str, card: dict[str, Any]) -> None:
        self.call(
            "PATCH",
            f"/open-apis/im/v1/messages/{identifier(message)}",
            json={"content": json.dumps(card, ensure_ascii=False)},
        )

    def download(self, message: str, key: str, kind: str) -> bytes:
        path = f"/open-apis/im/v1/messages/{identifier(message)}/resources/{identifier(key)}"
        try:
            with self._http.stream(
                "GET", path, params={"type": kind}, headers=self._authorization()
            ) as response:
                if response.status_code != 200:
                    raise FeishuError(f"download_http_{response.status_code}")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_DOWNLOAD_BYTES:
                        raise FeishuError("attachment_too_large")
                    content.extend(chunk)
                if (
                    "application/json" in response.headers.get("content-type", "")
                    and "content-disposition" not in response.headers
                ):
                    try:
                        body = json.loads(content)
                    except ValueError:
                        body = None
                    if (
                        isinstance(body, dict)
                        and isinstance(body.get("code"), int)
                        and body["code"] != 0
                        and "msg" in body
                    ):
                        raise FeishuError(f"resource_{body['code']}")
                return bytes(content)
        except httpx.HTTPError:
            raise FeishuError("transport_error") from None

    def upload(self, name: str, content: bytes) -> str:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise FeishuError("file_must_be_between_1_byte_and_30_MiB")
        body = self.call(
            "POST",
            "/open-apis/im/v1/files",
            data={"file_type": "stream", "file_name": name},
            files={"file": (name, content, "application/octet-stream")},
        )
        return identifier(str((body.get("data") or {}).get("file_key") or ""))

    def close(self) -> None:
        self._http.close()
