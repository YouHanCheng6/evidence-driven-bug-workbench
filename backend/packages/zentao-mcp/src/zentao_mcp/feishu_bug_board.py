"""Optional Feishu Bitable projection of a completed ZenTao daily snapshot."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx


class BoardError(RuntimeError):
    """A Bitable request or snapshot invariant failed."""


_FIELDS = ("Bug编号", "负责人", "产品", "分类", "配合端", "禅道链接", "在当前范围", "同步时间")


class FeishuBugBoard:
    def __init__(self, app_id: str, app_secret: str, app_token: str = "", table_id: str = "") -> None:
        self.app_id, self.app_secret = app_id, app_secret
        self.app_token, self.table_id = app_token, table_id

    @classmethod
    def from_environment(cls) -> FeishuBugBoard | None:
        values = [os.getenv(key, "").strip() for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BUG_BOARD_APP_TOKEN", "FEISHU_BUG_BOARD_TABLE_ID")]
        if not any(values):
            return None
        if not all(values):
            raise BoardError("飞书 Bug 看板配置不完整")
        return cls(*values)

    async def _request(self, method: str, path: str, *, payload: dict | None = None, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(base_url="https://open.feishu.cn", timeout=20.0) as client:
            auth = await client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                },
            )
            auth.raise_for_status()
            token_body = auth.json()
            if token_body.get("code") != 0 or not token_body.get("tenant_access_token"):
                raise BoardError(f"飞书授权失败：{token_body.get('code')} {token_body.get('msg', '')}")
            response = await client.request(
                method,
                f"/open-apis/{path}",
                json=payload,
                params=params,
                headers={
                    "Authorization": f"Bearer {token_body['tenant_access_token']}",
                },
            )
            body = response.json()
            if response.is_error or body.get("code") != 0:
                raise BoardError(f"飞书看板接口失败：{body.get('code', response.status_code)} {body.get('msg', '')}")
            return body.get("data") or {}

    def _records_path(self) -> str:
        return f"bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"

    async def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = await self._request("GET", self._records_path(), params=params)
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            next_page = data.get("page_token")
            if not next_page or next_page == page_token:
                raise BoardError("飞书记录分页不完整，已停止同步")
            page_token = next_page

    async def create_records(self, records: list[dict]) -> None:
        for start in range(0, len(records), 500):
            await self._request("POST", self._records_path() + "/batch_create", payload={"records": records[start : start + 500]})

    async def update_records(self, records: list[dict]) -> None:
        for start in range(0, len(records), 500):
            await self._request("POST", self._records_path() + "/batch_update", payload={"records": records[start : start + 500]})

    async def create_app(self, name: str) -> dict:
        data = await self._request("POST", "bitable/v1/apps", payload={"name": name})
        app = data.get("app") or data
        if not app.get("app_token"):
            raise BoardError("飞书创建成功但没有返回表格标识")
        return app

    async def create_table(self, name: str) -> dict:
        return await self._request(
            "POST",
            f"bitable/v1/apps/{self.app_token}/tables",
            payload={
                "table": {"name": name, "default_view_name": "当前 Bug", "fields": [{"field_name": field, "type": 1} for field in _FIELDS]},
            },
        )

    async def share_read_only(self, chat_id: str) -> dict:
        return await self._request(
            "POST",
            f"drive/v1/permissions/{self.app_token}/members",
            params={
                "type": "bitable",
                "need_notification": "false",
            },
            payload={"member_type": "openchat", "member_id": chat_id, "perm": "view"},
        )


async def sync_snapshot(
    board: FeishuBugBoard,
    rows: list[dict[str, Any]],
    *,
    zentao_url: str,
    now: str | None = None,
) -> None:
    """Upsert by Bug ID; retain disappeared rows as out-of-scope history."""
    stamp = now or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    existing: dict[str, dict] = {}
    for record in await board.list_records():
        fields = record.get("fields") or {}
        bug_id = str(fields.get("Bug编号") or "").strip()
        if not bug_id or not record.get("record_id"):
            raise BoardError("飞书看板存在缺少 Bug 编号或记录 ID 的行，已停止同步")
        if bug_id in existing:
            raise BoardError(f"飞书看板 Bug #{bug_id} 重复，已停止同步")
        existing[bug_id] = record

    incoming: set[str] = set()
    created, updated = [], []
    for row in rows:
        bug_id = str(int(row["bug_id"]))
        if bug_id in incoming:
            raise BoardError(f"日报快照 Bug #{bug_id} 重复，已停止同步")
        incoming.add(bug_id)
        fields = {
            "Bug编号": bug_id,
            "负责人": row["owner"],
            "产品": row["product"],
            "分类": row["category"],
            "配合端": "、".join(row["cooperation"]) or "无",
            "禅道链接": f"{zentao_url.rstrip('/')}/bug-view-{bug_id}.html",
            "在当前范围": "是",
            "同步时间": stamp,
        }
        if bug_id in existing:
            updated.append({"record_id": existing[bug_id]["record_id"], "fields": fields})
        else:
            created.append({"fields": fields})
    for bug_id, record in existing.items():
        if bug_id not in incoming and (record.get("fields") or {}).get("在当前范围") == "是":
            updated.append({"record_id": record["record_id"], "fields": {"在当前范围": "否"}})
    if created:
        await board.create_records(created)
    if updated:
        await board.update_records(updated)
