"""数据分级领域服务：敏感数据自动识别、分级标注和人工复核。

接收数据画像的分析结果，对敏感字段进行分级标注。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ClassificationService(BaseModuleService):
    MODULE = "classification"
    ENTITY = "分级标签"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标签编号", "目标字段", "敏感等级", "识别规则", "识别置信度", "人工复核", "复核人", "生效日期"]
    REQUIRED_FIELDS = ["标签编号", "目标字段", "敏感等级"]
    STATUS_ORDER = ["待复核", "已确认", "误识别", "已降级"]
    ACTION_RULES = {"确认分级": "已确认", "标记误识别": "误识别", "申请降级": "已降级"}
    NEGATIVE_ACTIONS = ["标记误识别"]

    def classify_field(self, asset_code: str, field_name: str, level: str, rule: str, confidence: str = "0.90") -> dict[str, Any]:
        """为指定字段创建分级标签。"""
        label = {
            "id": store.next_id(self.MODULE),
            "标签编号": f"CLAS-{asset_code}-{field_name}",
            "目标字段": f"{asset_code}.{field_name}",
            "敏感等级": level,
            "识别规则": rule,
            "识别置信度": confidence,
            "人工复核": "否",
            "复核人": "",
            "生效日期": _time.strftime("%Y-%m-%d"),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(label)
        return label

    def get_sensitive_fields(self, asset_code: str) -> list[dict[str, Any]]:
        """查询资产的所有敏感字段分级标签。"""
        prefix = f"{asset_code}."
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("目标字段", "")).startswith(prefix)
        ]

    def confirm_classification(self, label_id: int) -> dict[str, Any] | None:
        """人工确认分级标签。"""
        entry, msg = self.run_action(label_id, "确认分级")
        if entry:
            entry["人工复核"] = "是"
            entry["复核人"] = "合规管理员"
            store.log_change(
                self.MODULE, label_id, "确认分级",
                before="待复核", after="已确认",
                source="manual_review", scope="classification"
            )
        return entry

    def get_all_confirmed(self) -> list[dict[str, Any]]:
        """获取所有已确认的高敏感字段。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已确认"
        ]