"""差异报告领域服务：数据资产的结构化差异对比、版本间变更汇总和差异评估。

接收变更捕获的事件自动生成差异报告。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DiffReportService(BaseModuleService):
    MODULE = "diff_report"
    ENTITY = "差异记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "比较对象", "比较维度", "差异字段", "源值", "目标值", "差异等级", "报告时间"]
    REQUIRED_FIELDS = ["记录编号", "比较对象", "比较维度"]
    STATUS_ORDER = ["待生成", "已生成", "已确认", "已忽略"]
    ACTION_RULES = {"生成报告": "已生成", "确认差异": "已确认", "忽略差异": "已忽略"}
    NEGATIVE_ACTIONS = ["忽略差异"]

    def generate_report(self, subject: str, dimension: str, field: str, from_val: Any,
                        to_val: Any, level: str = "修改") -> dict[str, Any]:
        """生成差异报告。"""
        report_id = store.next_id(self.MODULE)
        report = {
            "id": report_id,
            "记录编号": f"DIFF-{report_id:04d}",
            "比较对象": subject,
            "比较维度": dimension,
            "差异字段": field,
            "源值": str(from_val),
            "目标值": str(to_val),
            "差异等级": level,
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(report)

        self.emit_event("diff_generated", {
            "report_id": report_id,
            "subject": subject,
            "level": level,
        })

        return report

    def confirm_diff(self, report_id: int) -> dict[str, Any] | None:
        """确认差异。"""
        return self.run_action(report_id, "确认差异")[0]

    def get_reports_by_target(self, target: str) -> list[dict[str, Any]]:
        """获取指定对象的差异报告。"""
        return store.find_by_field(self.MODULE, "比较对象", target)