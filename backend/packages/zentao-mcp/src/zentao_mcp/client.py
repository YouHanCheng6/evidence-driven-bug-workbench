"""Small ZenTao REST API v1 client for Bug reads and workflow notes."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import secrets
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

_READABLE_API_ROOTS = frozenset({"bugs", "products", "projects", "users"})
_CREDENTIAL_NAME_PATTERN = re.compile(r"token|password|cookie|authorization|secret|api[_-]?key", re.I)
# ZenTao's legacy HTML form and REST history can become consistent at different
# times. These delays retry only the idempotent history read; the write itself is
# never repeated. Total production wait is bounded to 5.2 seconds.
_NOTE_READBACK_DELAYS_SECONDS = (0.0, 0.4, 0.8, 1.5, 2.5)


class ZentaoError(Exception):
    """Base exception for errors that are safe to show to an MCP caller."""


class ZentaoConfigurationError(ZentaoError):
    """Required local configuration is missing or invalid."""


class ZentaoAuthenticationError(ZentaoError):
    """The configured account cannot read the requested resource."""


class ZentaoNotFoundError(ZentaoError):
    """The requested Bug does not exist or is not visible to this account."""


class ZentaoServiceError(ZentaoError):
    """ZenTao could not complete a request."""


class _TextExtractor(HTMLParser):
    """Convert ZenTao rich-text fields to compact plain text."""

    _BREAK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


class _EditableCommentExtractor(HTMLParser):
    """Read only the edit buttons ZenTao rendered for numbered history items."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._history_items: list[int | None] = []
        self.ordinals: set[int] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li":
            value = attributes.get("value") or ""
            self._history_items.append(int(value) if value.isdigit() else None)
        elif tag == "button" and self._history_items:
            if "btn-edit-comment" in (attributes.get("class") or "").split():
                ordinal = self._history_items[-1]
                if ordinal is not None:
                    self.ordinals.add(ordinal)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._history_items:
            self._history_items.pop()


class _InlineImageExtractor(HTMLParser):
    """Collect image references without changing the existing plain-text view."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = {key.lower(): value for key, value in attrs if value}
        src = values.get("src", "").strip()
        if src and not src.lower().startswith("data:"):
            self.images.append({"src": src, "alt": values.get("alt", "").strip()})


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _plain_note_text(value: Any) -> str:
    """Normalize authored note text without interpreting source code as HTML."""
    if not isinstance(value, str):
        return ""
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def _strip_note_markup(text: str) -> str:
    text = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|>|[-+*])\s+", "", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return re.sub(r"\s+", "", text)


def _note_form_value(comment: str) -> str:
    """Encode authored text for ZenTao's HTML-backed comment editor."""
    return escape(comment, quote=False)


def _bug_page_contains_note(page_html: str, comment: str) -> bool:
    expected = _strip_note_markup(_plain_note_text(comment))
    rendered_page = _strip_note_markup(_plain_text(page_html))
    return bool(expected) and expected in rendered_page


def _person_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("realname") or value.get("account")
    return str(name) if name else None


_TEXT_EXTENSIONS = {"csv", "json", "log", "md", "text", "txt", "xml", "yaml", "yml"}


def _media_type(name: str, fallback: str = "") -> str:
    extension = Path(name).suffix.lower().lstrip(".")
    if extension in _TEXT_EXTENSIONS:
        return "text/plain"
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or fallback or "application/octet-stream"


def _inline_image_assets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, str):
        return []
    parser = _InlineImageExtractor()
    parser.feed(value)
    parser.close()
    assets: list[dict[str, Any]] = []
    for index, image in enumerate(parser.images, start=1):
        source_name = Path(urlparse(image["src"]).path).name or f"inline-{index}.png"
        suffix = Path(source_name).suffix or ".png"
        name = f"{image['alt']}{suffix}" if image["alt"] else source_name
        assets.append(
            {
                "id": f"inline-{index}",
                "name": name,
                "url": image["src"],
                "source": "inline_image",
                "size": None,
                "media_type": _media_type(name, "image/png"),
            }
        )
    return assets


def _attachment_assets(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        raw_items = list(value.values())
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    assets: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("id") or item.get("fileID") or index)
        extension = str(item.get("extension") or "").strip().lstrip(".")
        name = str(item.get("title") or item.get("name") or item.get("filename") or f"attachment-{asset_id}").strip()
        if extension and not Path(name).suffix:
            name = f"{name}.{extension}"
        url = next(
            (str(item[key]).strip() for key in ("downloadURL", "downloadUrl", "webPath", "url", "pathname") if isinstance(item.get(key), str) and str(item[key]).strip()),
            f"file-download-{asset_id}.html",
        )
        raw_size = item.get("size")
        try:
            size = int(raw_size) if raw_size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        assets.append(
            {
                "id": asset_id,
                "name": name,
                "url": url,
                "source": "attachment",
                "size": size,
                "media_type": _media_type(name),
            }
        )
    return assets


def normalize_bug(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the Bug fields that are useful for triage, without returning secrets."""
    actions = payload.get("actions")
    history: list[dict[str, str]] = []
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            history.append(
                {
                    "id": str(action.get("id") or ""),
                    "action": str(action.get("action") or ""),
                    "actor": str(action.get("actor") or ""),
                    "date": str(action.get("date") or ""),
                    "comment": _plain_text(action.get("comment")),
                }
            )

    evidence_assets = _inline_image_assets(payload.get("steps"))
    evidence_assets.extend(_attachment_assets(payload.get("files") or payload.get("attachments")))

    return {
        "id": payload.get("id"),
        "title": _plain_text(payload.get("title")),
        "status": payload.get("status"),
        "resolution": payload.get("resolution"),
        "resolved_at": payload.get("resolvedDate"),
        "closed_at": payload.get("closedDate"),
        "severity": payload.get("severity"),
        "priority": payload.get("pri"),
        "type": payload.get("type"),
        "project": payload.get("projectName"),
        "product": payload.get("productName"),
        "module": _plain_text(payload.get("moduleTitle")),
        "assigned_to": _person_name(payload.get("assignedTo")),
        "opened_by": _person_name(payload.get("openedBy")),
        "opened_at": payload.get("openedDate"),
        "description": _plain_text(payload.get("steps")),
        "evidence_assets": evidence_assets,
        "history": history,
    }


class ZentaoClient:
    """HTTP client for Bug reads and the Workbench's one-note writeback."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        account: str | None = None,
        password: str | None = None,
        token_cache_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ZentaoConfigurationError("ZENTAO_URL must be a complete http(s) URL")
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._account = account.strip() if account else ""
        self._password = password.strip() if password else ""
        self._token_cache_path = token_cache_path
        self._transport = transport
        self._refresh_error = ""

    @classmethod
    def from_environment(cls) -> ZentaoClient:
        base_url = os.environ.get("ZENTAO_URL", "").strip()
        token = os.environ.get("ZENTAO_TOKEN", "").strip()
        account = os.environ.get("ZENTAO_ACCOUNT", "").strip()
        password = os.environ.get("ZENTAO_PASSWORD", "").strip()
        if not base_url:
            raise ZentaoConfigurationError("ZENTAO_URL is required")
        cache_path = Path.home() / ".deer-flow" / "zentao" / "token"
        cached_token = cls._read_cached_token(cache_path)
        if not (cached_token or token) and not (account and password):
            raise ZentaoConfigurationError("Configure ZENTAO_TOKEN or both ZENTAO_ACCOUNT and ZENTAO_PASSWORD")
        return cls(
            base_url,
            cached_token or token,
            account=account,
            password=password,
            token_cache_path=cache_path,
        )

    @staticmethod
    def _read_cached_token(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write_cached_token(self, token: str) -> None:
        """Persist a refreshed token privately without changing the user's .env."""
        if self._token_cache_path is None:
            return
        path = self._token_cache_path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(token, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        except OSError:
            # A cache failure must not expose a secret or prevent this request
            # from succeeding with the refreshed in-memory token.
            return

    async def _get_bug_response(self, bug_id: int, token: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                transport=self._transport,
                headers={"Token": token, "Accept": "application/json"},
            ) as client:
                return await client.get(f"{self._base_url}/api.php/v1/bugs/{bug_id}")
        except httpx.TimeoutException as exc:
            raise ZentaoServiceError("ZenTao request timed out") from exc
        except httpx.RequestError as exc:
            raise ZentaoServiceError("ZenTao service is unavailable") from exc

    async def _post_bug_note_response(
        self,
        bug_id: int,
        comment: str,
        *,
        client: httpx.AsyncClient,
    ) -> httpx.Response:
        """Post one Bug-detail note through ZenTao's visible form endpoint.

        ZenTao 18.4's ``添加备注`` dialog submits a URL-encoded form to
        ``action-comment-bug-{id}.html``. ``uid`` is a fresh per-submission
        form identifier and does not contain user or Bug data.
        """
        try:
            return await client.post(
                f"{self._base_url}/action-comment-bug-{bug_id}.html",
                data={"comment": _note_form_value(comment), "uid": secrets.token_hex(7)},
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Origin": self._base_url,
                    "Referer": f"{self._base_url}/bug-view-{bug_id}.html",
                },
            )
        except httpx.TimeoutException as exc:
            raise ZentaoServiceError("ZenTao note request timed out; do not retry automatically because the note may have been saved") from exc
        except httpx.RequestError as exc:
            raise ZentaoServiceError("ZenTao note service is unavailable; do not retry automatically because the note may have been saved") from exc

    @staticmethod
    def _looks_like_login_page(response: httpx.Response) -> bool:
        """Detect the silent HTML login response returned by ZenTao forms."""
        location = response.headers.get("location", "").lower()
        body = response.text.lower()
        return "user-login" in location or "user-login" in str(response.url).lower() or 'name="account"' in body

    async def _login_web_session(self, client: httpx.AsyncClient, bug_id: int) -> None:
        """Establish the browser session required by ZenTao's form actions.

        The REST Token can read Bug data but ZenTao 18.4's visible ``添加备注``
        endpoint authenticates with the browser's ``zentaosid`` session.  Do not
        reuse a caller's browser cookie: the service account logs in for this
        one request only and the client is closed immediately afterwards.
        """
        if not self._account or not self._password:
            raise ZentaoConfigurationError("ZenTao note write requires ZENTAO_ACCOUNT and ZENTAO_PASSWORD to create a web session")
        login_url = f"{self._base_url}/user-login.html"
        try:
            initial = await client.get(login_url, headers={"Accept": "text/html"})
            if initial.is_error:
                raise ZentaoAuthenticationError(f"ZenTao web login page returned HTTP {initial.status_code}")
            login = await client.post(
                login_url,
                data={"account": self._account, "password": self._password, "keepLogin": "on"},
                headers={"Accept": "text/html", "Origin": self._base_url, "Referer": login_url},
            )
            if login.is_error:
                raise ZentaoAuthenticationError(f"ZenTao web login returned HTTP {login.status_code}")
            # A successful login is confirmed by opening the target Bug page;
            # ZenTao often returns HTTP 200 for both success and a failed form.
            probe = await client.get(f"{self._base_url}/bug-view-{bug_id}.html", headers={"Accept": "text/html"})
        except httpx.TimeoutException as exc:
            raise ZentaoServiceError("ZenTao web login timed out") from exc
        except httpx.RequestError as exc:
            raise ZentaoServiceError("ZenTao web login service is unavailable") from exc
        if probe.is_error or self._looks_like_login_page(probe):
            raise ZentaoAuthenticationError("ZenTao web login failed; the service account cannot open this Bug page")

    @staticmethod
    def _extract_token(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        value = payload.get("token")
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("token")
            if isinstance(nested, str):
                return nested.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("token")
            if isinstance(nested, str):
                return nested.strip()
        return ""

    async def _refresh_token(self) -> bool:
        """Refresh only after an authentication failure; never expose secrets."""
        if not self._account or not self._password:
            self._refresh_error = "ZenTao token refresh requires ZENTAO_ACCOUNT and ZENTAO_PASSWORD"
            return False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api.php/v1/tokens",
                    json={"account": self._account, "password": self._password},
                )
        except httpx.TimeoutException as exc:
            self._refresh_error = "ZenTao token refresh timed out"
            raise ZentaoServiceError(self._refresh_error) from exc
        except httpx.RequestError as exc:
            self._refresh_error = "ZenTao token refresh service is unavailable"
            raise ZentaoServiceError(self._refresh_error) from exc

        if response.is_error:
            self._refresh_error = f"ZenTao token refresh failed with HTTP {response.status_code}"
            return False
        try:
            refreshed_token = self._extract_token(response.json())
        except ValueError:
            self._refresh_error = "ZenTao token refresh returned invalid JSON"
            return False
        if not refreshed_token:
            self._refresh_error = "ZenTao token refresh response did not contain a token"
            return False
        self._token = refreshed_token
        self._write_cached_token(refreshed_token)
        return True

    async def get_bug(self, bug_id: int) -> dict[str, Any]:
        if not isinstance(bug_id, int) or isinstance(bug_id, bool) or bug_id <= 0:
            raise ZentaoConfigurationError("bug_id must be a positive integer")

        if not self._token and not await self._refresh_token():
            raise ZentaoAuthenticationError(self._refresh_error or "ZenTao token is missing and could not be refreshed")

        response = await self._get_bug_response(bug_id, self._token)

        if response.status_code in {401, 403}:
            if not await self._refresh_token():
                raise ZentaoAuthenticationError(self._refresh_error or "ZenTao token is invalid or lacks permission for this Bug")
            response = await self._get_bug_response(bug_id, self._token)

        if response.status_code in {401, 403}:
            raise ZentaoAuthenticationError("ZenTao refreshed the token, but this account cannot read the requested Bug")
        if response.status_code == 404:
            raise ZentaoNotFoundError("ZenTao Bug was not found")
        if response.is_error:
            raise ZentaoServiceError(f"ZenTao returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ZentaoServiceError("ZenTao returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise ZentaoServiceError("ZenTao returned an unexpected response")
        if payload.get("error"):
            raise ZentaoNotFoundError("ZenTao Bug was not found or is not visible to this account")

        return normalize_bug(payload)

    async def _get_bug_for_note(self, bug_id: int, *, stage: str) -> dict[str, Any]:
        """Retry only idempotent Bug reads around one analysis-note write."""
        for attempt in range(3):
            try:
                return await self.get_bug(bug_id)
            except ZentaoServiceError as exc:
                cause = exc.__cause__
                if not isinstance(cause, httpx.RequestError):
                    raise
                if attempt == 2:
                    raise ZentaoServiceError(f"ZenTao Bug read {stage} failed after 3 attempts ({type(cause).__name__})") from exc
                await asyncio.sleep((0.25, 0.75)[attempt])
        raise AssertionError("unreachable Bug read retry state")

    async def _poll_bug_history(
        self,
        bug_id: int,
        *,
        stage: str,
        matches: Callable[[dict[str, Any]], bool],
    ) -> tuple[dict[str, Any], bool]:
        """Wait briefly for a successful form write to become REST-visible.

        ZenTao 18.4 accepts note writes through a browser form while exposing
        history through REST. Polling only the read side closes that eventual-
        consistency window without risking a duplicate form submission.
        """
        latest: dict[str, Any] = {}
        for attempt, delay in enumerate(_NOTE_READBACK_DELAYS_SECONDS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            latest = await self._get_bug_for_note(bug_id, stage=f"{stage}_{attempt}")
            if matches(latest):
                return latest, True
        return latest, False

    async def read_api(
        self,
        path: str,
        *,
        params: dict[str, str | int | float | bool] | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read one allowlisted REST-v1 resource without exposing credentials."""
        if len(path) > 256 or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path):
            raise ZentaoConfigurationError("请输入 REST-v1 相对资源路径，不接受 URL、查询串或路径跳转。")
        root = path.split("/", 2)[1].lower()
        if root not in _READABLE_API_ROOTS:
            raise ZentaoConfigurationError("只允许读取 bugs、products、projects 或 users 业务资源。")
        forbidden = {"tokens", "login", "logout", "delete", "remove", "close", "activate", "resolve", "edit", "create", "update", "password", "reset"}
        if forbidden.intersection(part.lower() for part in path.split("/")):
            raise ZentaoConfigurationError("连接只支持业务资源读取，不支持认证资源或写操作。")
        params = params or {}
        if fields is not None:
            if (
                not isinstance(fields, list)
                or len(fields) > 12
                or any(not isinstance(field, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,2}", field) or _CREDENTIAL_NAME_PATTERN.search(field) for field in fields)
            ):
                raise ZentaoConfigurationError("字段投影只接受最多 12 个普通业务字段名称。")
            fields = list(dict.fromkeys(["id", *fields]))
        if len(params) > 30:
            raise ZentaoConfigurationError("单次读取最多接受 30 个查询参数。")
        for key, value in params.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                raise ZentaoConfigurationError("查询参数名称无效。")
            if _CREDENTIAL_NAME_PATTERN.search(key):
                raise ZentaoConfigurationError("凭据由连接管理，不接受凭据查询参数。")
            if isinstance(value, (dict, list)) or value is None or len(str(value)) > 512:
                raise ZentaoConfigurationError("查询参数只接受较短的标量值。")
        if not self._token and not await self._refresh_token():
            return {"ok": False, "message": "禅道认证未成功，请检查现有连接凭据。"}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                headers={"Accept": "application/json"},
                transport=self._transport,
            ) as client:
                response = await client.get(f"{self._base_url}/api.php/v1{path}", params=params, headers={"Token": self._token})
                if response.status_code in {401, 403} and await self._refresh_token():
                    response = await client.get(f"{self._base_url}/api.php/v1{path}", params=params, headers={"Token": self._token})
        except httpx.TimeoutException as exc:
            raise ZentaoServiceError("ZenTao read request timed out") from exc
        except httpx.RequestError as exc:
            raise ZentaoServiceError("ZenTao read service is unavailable") from exc
        status = response.status_code
        if status in {401, 403}:
            return {"ok": False, "http_status": status, "message": "禅道认证失败或账号没有该资源的读取权限。"}
        if status == 404:
            return {"ok": False, "http_status": status, "message": "接口路径或资源不存在，或当前账号不可见。"}
        if status < 200 or status >= 300:
            return {"ok": False, "http_status": status, "message": "禅道未成功返回资源；此状态不能直接归因为权限不足。"}
        if len(response.content) > (2_000_000 if fields is not None else 128_000):
            return {"ok": False, "message": "响应超过单次读取范围，请减小 limit 后读取；没有返回被截断的统计结果。"}
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "message": "接口未返回 JSON，请核对路径和登录状态。"}
        if isinstance(payload, dict) and payload.get("error"):
            return {"ok": False, "http_status": status, "message": "禅道返回业务错误，未确认查询结果。"}

        if fields is not None and isinstance(payload, dict):
            # Preserve pagination and count metadata; project records only in
            # the four generic list resources, never manufacture a count.
            def projected_record(record: dict[str, Any]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for field in fields:
                    source: Any = record
                    for part in field.split("."):
                        if not isinstance(source, dict) or part not in source:
                            break
                        source = source[part]
                    else:
                        result[field] = source
                return result

            payload = {key: [projected_record(item) if isinstance(item, dict) else item for item in value] if key in _READABLE_API_ROOTS and isinstance(value, list) else value for key, value in payload.items()}

        secrets_to_mask = [value for value in (self._token, self._password) if value]

        def redact(value):
            if isinstance(value, dict):
                return {key: ("[redacted]" if _CREDENTIAL_NAME_PATTERN.search(str(key)) else redact(item)) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                for secret in secrets_to_mask:
                    value = value.replace(secret, "[redacted]")
            return value

        data = redact(payload)
        if len(str(data)) > 128_000:
            return {"ok": False, "message": "投影后的响应仍超过单次读取范围，请减小 limit。"}
        return {"ok": True, "http_status": status, "path": path, "query": redact(params), "fields": fields, "data": data}

    async def download_evidence_asset(self, bug_id: int, asset: dict[str, Any], *, max_bytes: int) -> tuple[bytes, str]:
        """Download one same-origin Bug asset through a short-lived web session."""
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ZentaoConfigurationError("max_bytes must be positive")
        raw_url = str(asset.get("url") or "").strip()
        if not raw_url:
            raise ZentaoConfigurationError("ZenTao evidence asset has no URL")
        target = urljoin(f"{self._base_url}/", raw_url)
        base = urlparse(self._base_url)
        parsed = urlparse(target)
        if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
            raise ZentaoConfigurationError("ZenTao evidence URL must use the configured ZenTao origin")
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), follow_redirects=False, transport=self._transport) as session:
            await self._login_web_session(session, bug_id)
            try:
                response = await session.get(target, headers={"Accept": "*/*", "Referer": f"{self._base_url}/bug-view-{bug_id}.html"})
                if response.is_redirect:
                    redirected = urljoin(target, response.headers.get("location", ""))
                    redirect_url = urlparse(redirected)
                    if redirect_url.scheme != base.scheme or redirect_url.netloc != base.netloc:
                        raise ZentaoConfigurationError("ZenTao evidence redirect left the configured origin")
                    response = await session.get(redirected, headers={"Accept": "*/*", "Referer": target})
            except httpx.TimeoutException as exc:
                raise ZentaoServiceError("ZenTao evidence download timed out") from exc
            except httpx.RequestError as exc:
                raise ZentaoServiceError("ZenTao evidence download is unavailable") from exc
        if response.is_redirect:
            raise ZentaoServiceError("ZenTao evidence download returned too many redirects")
        if response.is_error or self._looks_like_login_page(response):
            raise ZentaoServiceError(f"ZenTao evidence download returned HTTP {response.status_code}")
        if len(response.content) > max_bytes:
            raise ZentaoServiceError("ZenTao evidence file exceeds the configured size limit")
        content_type = response.headers.get("content-type", "").partition(";")[0].strip()
        return response.content, content_type or str(asset.get("media_type") or "application/octet-stream")

    async def add_bug_note(self, bug_id: int, comment: str) -> dict[str, bool]:
        """Add one visible Bug-detail note and verify it without another write.

        The endpoint is the same traditional form used by ZenTao's **添加备注**
        dialog. The same authenticated page is the first confirmation source;
        REST history is a fallback because ZenTao may publish it later. Every
        failed or ambiguous write fails closed so the workflow can never create
        a duplicate note by retrying.
        """
        if not isinstance(bug_id, int) or isinstance(bug_id, bool) or bug_id <= 0:
            raise ZentaoConfigurationError("bug_id must be a positive integer")
        if not isinstance(comment, str) or not comment.strip():
            raise ZentaoConfigurationError("ZenTao note must not be empty")
        if not self._token and not await self._refresh_token():
            raise ZentaoAuthenticationError(self._refresh_error or "ZenTao token is missing and could not be refreshed")

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=self._transport,
        ) as session:
            await self._login_web_session(session, bug_id)
            response = await self._post_bug_note_response(bug_id, comment, client=session)
            if response.status_code in {401, 403} or self._looks_like_login_page(response):
                raise ZentaoAuthenticationError("ZenTao rejected the web-session note write")
            if response.is_error:
                raise ZentaoServiceError(f"ZenTao note write returned HTTP {response.status_code}")

            page_verified = False
            try:
                page = await session.get(f"{self._base_url}/bug-view-{bug_id}.html")
                page_verified = not page.is_error and not self._looks_like_login_page(page) and _bug_page_contains_note(page.text, comment)
            except (httpx.TimeoutException, httpx.RequestError):
                # The idempotent REST read below remains available when the
                # browser page cannot be refreshed after a successful POST.
                pass

        if page_verified:
            return {"saved": True, "verified": True}

        # Never report success merely because a legacy HTML form returned 200.
        # If the page did not expose the note yet, retain the existing bounded
        # REST-history fallback. An ambiguous outcome still never repeats POST.
        normalized_comment = _plain_note_text(comment)
        try:
            _bug, verified = await self._poll_bug_history(
                bug_id,
                stage="after_create",
                matches=lambda bug: any(action.get("comment") == normalized_comment for action in bug.get("history", []) if isinstance(action, dict)),
            )
        except ZentaoError as exc:
            raise ZentaoServiceError("ZenTao accepted the note form but the write could not be verified; check Bug history before retrying") from exc
        if not verified:
            raise ZentaoServiceError(f"ZenTao did not confirm the note after {len(_NOTE_READBACK_DELAYS_SECONDS)} history reads; it was not reported as written")
        return {"saved": True, "verified": True}

    async def save_analysis_note(self, bug_id: int, comment: str) -> dict[str, Any]:
        """Rewrite the latest editable analysis backup, or reuse it without appending.

        The rendered edit button is the service account's capability signal.
        History is checked again immediately before the edit POST so a later
        human action makes the old backup read-only for this workflow.
        """
        if not isinstance(comment, str) or not comment.strip():
            raise ZentaoConfigurationError("ZenTao analysis note must not be empty")
        marker = "【自动分析备注｜待人工确认】"
        bug = await self._get_bug_for_note(bug_id, stage="before_write")
        history = bug.get("history") or []
        backups = [(index, action) for index, action in enumerate(history, start=1) if isinstance(action, dict) and action.get("action") == "commented" and marker in str(action.get("comment") or "")]
        if not backups:
            result = await self.add_bug_note(bug_id, comment)
            return {**result, "mode": "created", "note_content": comment}
        ordinal, backup = backups[-1]
        action_id = str(backup["id"])
        old_content = str(backup["comment"])
        if not action_id.isdigit():
            return {"saved": True, "verified": True, "mode": "reused", "reuse_reason": "edit_unavailable", "note_content": old_content}
        if _plain_note_text(comment) == old_content:
            return {"saved": True, "verified": True, "mode": "reused", "reuse_reason": "identical", "action_id": action_id, "note_content": old_content}

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False, transport=self._transport) as session:
            await self._login_web_session(session, bug_id)
            page = await session.get(f"{self._base_url}/bug-view-{bug_id}.html")
            if page.is_error or self._looks_like_login_page(page):
                raise ZentaoServiceError("ZenTao could not confirm the analysis backup edit control")
            editable = _EditableCommentExtractor()
            editable.feed(page.text)
            if ordinal not in editable.ordinals:
                return {"saved": True, "verified": True, "mode": "reused", "reuse_reason": "edit_unavailable", "action_id": action_id, "note_content": old_content}

            # The REST action ID, history position and existing content must
            # still match before using ZenTao's action-editComment form.
            current = await self._get_bug_for_note(bug_id, stage="before_edit")
            current_history = current.get("history") or []
            if len(current_history) != len(history) or ordinal > len(current_history):
                return {"saved": True, "verified": True, "mode": "reused", "reuse_reason": "newer_history", "action_id": action_id, "note_content": old_content}
            current_action = current_history[ordinal - 1]
            if current_action.get("id") != action_id or current_action.get("comment") != old_content:
                raise ZentaoServiceError("ZenTao analysis backup changed during edit preflight; no note was added")
            try:
                response = await session.post(
                    f"{self._base_url}/action-editComment-{action_id}.html",
                    # ZenTao's 修改备注 form uses lastComment, unlike 添加备注.
                    data={"lastComment": _note_form_value(comment), "uid": secrets.token_hex(7)},
                    headers={"Accept": "text/html", "Origin": self._base_url, "Referer": f"{self._base_url}/bug-view-{bug_id}.html"},
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise ZentaoServiceError("ZenTao analysis backup edit outcome is uncertain; read history before retrying") from exc
        if response.status_code in {401, 403} or self._looks_like_login_page(response):
            raise ZentaoAuthenticationError("ZenTao rejected the analysis backup edit")
        if response.is_error:
            raise ZentaoServiceError(f"ZenTao analysis backup edit returned HTTP {response.status_code}")
        normalized_comment = _plain_note_text(comment)
        confirmed, verified = await self._poll_bug_history(
            bug_id,
            stage="after_edit",
            matches=lambda bug: any(item.get("id") == action_id and item.get("comment") == normalized_comment for item in bug.get("history", []) if isinstance(item, dict)),
        )
        action = next((item for item in confirmed.get("history", []) if item.get("id") == action_id), None)
        if action is None:
            raise ZentaoServiceError("ZenTao analysis backup action disappeared during edit verification")
        if verified:
            return {"saved": True, "verified": True, "mode": "rewritten", "action_id": action_id, "note_content": comment}
        if action.get("comment") == old_content:
            # A rendered edit control is not a guarantee that ZenTao accepted
            # this action-edit POST. When readback proves the original backup
            # unchanged, keep it rather than treating a no-op edit as a reason
            # to append another automated note or rerun the investigation.
            reason = "newer_history" if len(confirmed.get("history") or []) > len(history) else "edit_unavailable"
            return {"saved": True, "verified": True, "mode": "reused", "reuse_reason": reason, "action_id": action_id, "note_content": old_content}
        raise ZentaoServiceError("ZenTao did not verify the edited analysis backup; no new note was submitted")
