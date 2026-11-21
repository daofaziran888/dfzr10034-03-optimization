"""模块服务基类：共享查询、登记、动作和统计骨架，业务差异留在各模块文件。

提供跨模块数据管道的基础能力：管线编排、审计追踪、影响范围计算。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    OPERATION_TERM = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": store.next_id(self.MODULE)}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        rows.append(entry)
        self.after_create(entry, values)
        return entry, []

    def after_create(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """登记后的领域补齐钩子，默认不做任何事。"""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        entry["updated_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")

        # 记录变更
        store.log_change(
            self.MODULE, entry_id, action,
            before=old_status, after=target,
            source="action", scope=self.MODULE
        )
        self.after_action(entry, action, old_status)
        return entry, f"{self.ENTITY}已{action}"

    def after_action(self, entry: dict[str, Any], action: str, old_status: Any) -> None:
        """动作后的领域联动钩子，默认不做任何事。"""

    def domain_snapshot(self) -> dict[str, Any]:
        """输出模块级数据质量快照，供跨模块消费和题位源码锚点使用。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
            "snapshot_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 管线编排 ──────────────────────────────────────────────

    def pipeline_context(self) -> dict[str, Any]:
        """获取当前模块的管道上下文。"""
        ctx = store.get_pipeline_state(f"pipeline:{self.MODULE}") or {}
        return ctx

    def update_pipeline_context(self, updates: dict[str, Any]) -> None:
        """更新管道上下文。"""
        ctx = self.pipeline_context()
        ctx.update(updates)
        store.set_pipeline_state(f"pipeline:{self.MODULE}", ctx)

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """向其他模块发出事件通知。"""
        event = {
            "source": self.MODULE,
            "type": event_type,
            "payload": payload,
            "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        events = store.get_pipeline_state("events") or {"events": []}
        events.setdefault("events", []).append(event)
        store.set_pipeline_state("events", events)
        return event

    def consume_events(self, event_types: list[str] | None = None) -> list[dict[str, Any]]:
        """消费事件队列。"""
        state = store.get_pipeline_state("events") or {"events": []}
        all_events = state.get("events", [])
        if event_types is None:
            return all_events
        return [e for e in all_events if e.get("type") in event_types]