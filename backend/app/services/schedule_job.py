"""调度治理领域服务：治理任务的定时调度编排、依赖检查和失败告警。

编排自动化治理任务：质量检查、合规扫描、生命周期流转的定时执行。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ScheduleJobService(BaseModuleService):
    MODULE = "schedule_job"
    ENTITY = "治理调度"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["作业编号", "治理类型", "目标资产", "Cron表达式", "依赖检查", "超时上限", "失败通知", "下次执行"]
    REQUIRED_FIELDS = ["作业编号", "治理类型", "目标资产"]
    STATUS_ORDER = ["待激活", "执行中", "已完成", "已停用"]
    ACTION_RULES = {"激活任务": "执行中", "立即执行": "已完成", "停用任务": "已停用"}
    NEGATIVE_ACTIONS = ["停用任务"]

    def schedule_task(self, asset_code: str, job_type: str, cron_expr: str,
                      timeout: str = "3600", notify_on_failure: str = "是") -> dict[str, Any]:
        """创建治理调度任务。"""
        job_id = store.next_id(self.MODULE)
        job = {
            "id": job_id,
            "作业编号": f"SCHE-{job_id:04d}",
            "治理类型": job_type,
            "目标资产": asset_code,
            "Cron表达式": cron_expr,
            "依赖检查": self._compute_dependencies(job_type, asset_code),
            "超时上限": timeout,
            "失败通知": notify_on_failure,
            "下次执行": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() + 3600)),
            "status": "待激活",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(job)
        return job

    def _compute_dependencies(self, job_type: str, asset_code: str) -> str:
        """计算调度作业的依赖。"""
        deps = []
        if job_type in ("质量检查", "quality_check"):
            rules = store.find_by_field("quality_rule", "目标资产", asset_code)
            if rules:
                deps.append(f"依赖{len(rules)}条质量规则")
        if job_type in ("合规扫描", "compliance_scan"):
            contracts = store.find_by_field("data_contract", "数据资产", asset_code)
            if contracts:
                deps.append(f"依赖{len(contracts)}个数据契约")
        if job_type in ("生命周期", "lifecycle"):
            policies = store.find_by_field("lifecycle_policy", "目标资产", asset_code)
            if policies:
                deps.append(f"依赖{len(policies)}条生命周期策略")
        return "; ".join(deps) if deps else "无"

    def execute_job(self, job_id: int) -> dict[str, Any]:
        """执行调度任务。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "error": "作业不存在"}

        job["status"] = "执行中"
        job["pending"] = True
        job["执行时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")

        job_type = job.get("治理类型", "")
        asset = job.get("目标资产", "")
        results = []

        if "质量" in job_type:
            # 触发质量检查
            from app.services import QUALITY_SERVICE
            metrics = {"完整性": 85.0, "准确性": 92.0, "一致性": 78.0}
            results = QUALITY_SERVICE.batch_check_assets(asset, metrics)
        elif "合规" in job_type:
            # 触发合规扫描
            from app.services import COMPLIANCE_SERVICE
            scan = COMPLIANCE_SERVICE.scan_asset(asset, "自动调度合规检查",
                                                  ["分类标签复核", "脱敏规则完备性", "审计异常"])
            results = [scan]
        elif "生命" in job_type:
            # 触发生命周期流转
            policies = store.find_by_field("lifecycle_policy", "目标资产", asset)
            from app.services import LIFECYCLE_SERVICE
            for policy in policies:
                result = LIFECYCLE_SERVICE.transition_stage(policy["id"], "活跃阶段")
                results.append(result)

        job["status"] = "已完成"
        job["pending"] = False
        job["results"] = results

        self.emit_event("job_completed", {
            "job_id": job_id,
            "job_type": job_type,
            "asset": asset,
            "results_count": len(results),
        })

        return {
            "ok": True,
            "job_id": job_id,
            "job_type": job_type,
            "results": results,
        }