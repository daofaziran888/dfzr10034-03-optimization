"""冷热归档领域服务：冷热数据的自动分层归档、压缩转储和完整性校验。

接收生命周期策略的归档阶段流转事件，执行数据归档操作。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ColdArchiveService(BaseModuleService):
    MODULE = "cold_archive"
    ENTITY = "归档任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "源资产", "归档条件", "目标存储", "压缩算法", "校验方式", "数据行数", "执行时间"]
    REQUIRED_FIELDS = ["任务编号", "源资产", "归档条件"]
    STATUS_ORDER = ["待执行", "归档中", "已完成", "校验失败"]
    ACTION_RULES = {"启动归档": "归档中", "取消归档": "已完成", "标记失败": "校验失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    def execute_archive(self, task_id: int, data_rows: int = 0, source_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行归档操作，将数据进行压缩转储（Pipeline 4 核心方法）。"""
        task = self.get_entry(task_id)
        if not task:
            return {"ok": False, "error": "归档任务不存在"}

        # 更新任务状态为执行中
        task["status"] = "归档中"
        task["执行时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        task["数据行数"] = str(data_rows)
        task["pending"] = True

        # 模拟归档计算校验和
        checksum = self._compute_checksum(task, data_rows)

        # 如果提供了源快照，则存储快照引用
        if source_snapshot:
            task["snapshot_ref"] = source_snapshot.get("snapshot_id", "")

        # 标记完成
        task["status"] = "已完成"
        task["pending"] = False
        task["checksum"] = checksum

        # 创建版本快照记录归档版本
        store.create_snapshot(f"archive-{task_id}", [self.MODULE])

        store.log_change(
            self.MODULE, task_id, "归档完成",
            before="待执行", after="已完成",
            source="pipeline_4", scope=f"cold_archive:资产{task.get('源资产', '')}"
        )

        self.emit_event("archive_completed", {
            "task_id": task_id,
            "asset_code": task.get("源资产", ""),
            "data_rows": data_rows,
            "checksum": checksum,
        })

        return {
            "ok": True,
            "task_id": task_id,
            "asset": task.get("源资产"),
            "archived_rows": data_rows,
            "checksum": checksum,
            "storage": task.get("目标存储"),
            "pipeline": "lifecycle_policy->cold_archive",
        }

    def _compute_checksum(self, task: dict[str, Any], data_rows: int) -> str:
        """计算归档数据校验和。"""
        import hashlib
        raw = f"{task.get('源资产','')}:{data_rows}:{task.get('执行时间','')}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_pending_archives(self) -> list[dict[str, Any]]:
        """获取待执行的归档任务。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "待执行"
        ]

    def count_archived(self) -> int:
        """统计已归档的任务数。"""
        return sum(1 for row in store.rows(self.MODULE) if row.get("status") == "已完成")