"""版本履历领域服务：数据资产的多版本Schema快照、差异对比和回查能力。

维护跨时间点的数据资产版本快照，支持回溯和差异分析。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class VersionHistoryService(BaseModuleService):
    MODULE = "version_history"
    ENTITY = "版本快照"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["快照编号", "目标资产", "版本标签", "Schema摘要", "行数记录", "创建原因", "差异字段", "快照时间"]
    REQUIRED_FIELDS = ["快照编号", "目标资产", "版本标签"]
    STATUS_ORDER = ["当前版本", "历史版本", "已标注"]
    ACTION_RULES = {"创建快照": "历史版本", "标注版本": "已标注"}
    NEGATIVE_ACTIONS = []

    def create_version(self, asset_code: str, version_tag: str, reason: str, schema_summary: str = "",
                       row_count: str = "0", diff_fields: str = "") -> dict[str, Any]:
        """创建资产版本快照。"""
        snap_id = store.next_id(self.MODULE)
        snapshot = {
            "id": snap_id,
            "快照编号": f"VERS-{snap_id:04d}",
            "目标资产": asset_code,
            "版本标签": version_tag,
            "Schema摘要": schema_summary or f"资产{asset_code}快照",
            "行数记录": row_count,
            "创建原因": reason,
            "差异字段": diff_fields,
            "快照时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "历史版本",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(snapshot)

        # 同时使用 store 级的快照功能保存完整数据副本
        store_snapshot = store.create_snapshot(f"version-{version_tag}-{asset_code}")

        return {
            "version": snapshot,
            "store_snapshot": store_snapshot,
        }

    def annotate_version(self, snapshot_id: int, annotation: str) -> dict[str, Any] | None:
        """标注版本快照。"""
        entry, msg = self.run_action(snapshot_id, "标注版本")
        if entry:
            entry["标注"] = annotation
            entry["标注时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        return entry

    def get_versions_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有版本快照（按时间倒序）。"""
        versions = store.find_by_field(self.MODULE, "目标资产", asset_code)
        versions.sort(key=lambda v: v.get("快照时间", ""), reverse=True)
        return versions

    def get_current_version(self, asset_code: str) -> dict[str, Any] | None:
        """获取资产的当前版本快照。"""
        versions = self.get_versions_by_asset(asset_code)
        return versions[0] if versions else None