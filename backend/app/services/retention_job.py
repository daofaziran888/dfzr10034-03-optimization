"""保留清理领域服务：过期数据的合规保留、安全清理和预演确认。

接收生命周期策略的销毁阶段流转事件，执行数据保留清理操作。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RetentionJobService(BaseModuleService):
    MODULE = "retention_job"
    ENTITY = "清理计划"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["计划编号", "目标资产", "保留天数", "清理条件", "预演模式", "清理行数", "备份路径", "下次执行"]
    REQUIRED_FIELDS = ["计划编号", "目标资产", "保留天数"]
    STATUS_ORDER = ["草稿", "执行中", "已清理", "跳过"]
    ACTION_RULES = {"预演清理": "执行中", "执行清理": "已清理", "跳过清理": "跳过"}
    NEGATIVE_ACTIONS = []

    def dry_run_cleanup(self, job_id: int, data_rows: int) -> dict[str, Any]:
        """预演清理：不实际删除，只统计将要清理的行数（Pipeline 4 方法）。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}

        job["预演模式"] = "是"
        job["清理行数"] = str(data_rows)
        job["status"] = "执行中"
        job["pending"] = True

        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "will_clean": data_rows,
            "mode": "dry_run",
        }

    def execute_cleanup(self, job_id: int, create_backup: bool = True) -> dict[str, Any]:
        """执行清理操作并创建备份（Pipeline 4 核心方法）。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}

        job["status"] = "已清理"
        job["pending"] = False
        job["清理行数"] = str(int(job.get("清理行数", "0")))

        # 创建清理快照
        if create_backup:
            snap = store.create_snapshot(f"pre-cleanup-{job_id}", [self.MODULE])
            job["备份路径"] = f"backup/clenaup-snap-{snap['snapshot_id']}/"

        store.log_change(
            self.MODULE, job_id, "执行清理",
            before="执行中", after="已清理",
            source="pipeline_4", scope=f"retention_job:资产{job.get('目标资产', '')}"
        )

        self.emit_event("cleanup_completed", {
            "job_id": job_id,
            "asset_code": job.get("目标资产", ""),
            "backup_path": job.get("备份路径", ""),
        })

        return {
            "ok": True,
            "job_id": job_id,
            "asset": job.get("目标资产"),
            "cleaned": int(job.get("清理行数", "0")),
            "backup_path": job.get("备份路径"),
            "pipeline": "lifecycle_policy->retention_job",
        }

    def skip_cleanup(self, job_id: int, reason: str) -> dict[str, Any]:
        """跳过清理（保留数据）。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "清理计划不存在"}
        job["status"] = "跳过"
        job["pending"] = False
        job["清理条件"] = f"已跳过：{reason}"
        store.log_change(
            self.MODULE, job_id, "跳过清理",
            before="执行中", after="跳过",
            source="manual_skip", scope=f"retention_job:资产{job.get('目标资产', '')}"
        )
        return {"ok": True, "job_id": job_id, "reason": reason}