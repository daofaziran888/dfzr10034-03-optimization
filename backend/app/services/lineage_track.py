"""血缘追踪领域服务：端到端数据血缘关系的自动推导、影响层级计算和变更通知。

Pipeline 7: Lineage tracking → impact analysis → governance scoring
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class LineageTrackService(BaseModuleService):
    MODULE = "lineage_track"
    ENTITY = "血缘关系"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["关系编号", "上游资产", "下游资产", "关联字段", "转换逻辑", "影响层级", "自动推导", "最后更新"]
    REQUIRED_FIELDS = ["关系编号", "上游资产", "下游资产"]
    STATUS_ORDER = ["自动生成", "已确认", "待验证", "已废弃"]
    ACTION_RULES = {"确认关系": "已确认", "提交验证": "待验证", "废弃关系": "已废弃"}
    NEGATIVE_ACTIONS = ["废弃关系"]

    def derive_lineage(self, upstream_asset: str, downstream_asset: str, join_fields: str,
                       transform_logic: str, auto_derived: bool = True) -> dict[str, Any]:
        """自动推导血缘关系并触发影响分析和治理评分（Pipeline 7 入口）。"""
        lineage_id = store.next_id(self.MODULE)
        lineage = {
            "id": lineage_id,
            "关系编号": f"LINE-{lineage_id:04d}",
            "上游资产": upstream_asset,
            "下游资产": downstream_asset,
            "关联字段": join_fields,
            "转换逻辑": transform_logic,
            "影响层级": str(self._compute_impact_level(upstream_asset)),
            "自动推导": "是" if auto_derived else "否",
            "最后更新": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "自动生成" if auto_derived else "已确认",
            "pending": not auto_derived,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(lineage)

        # Pipeline 7: 触发影响分析
        impact = self._trigger_impact_analysis(lineage)
        # Pipeline 7: 触发治理评分
        score = self._trigger_governance_scoring(lineage)

        self.emit_event("lineage_derived", {
            "lineage_id": lineage_id,
            "upstream": upstream_asset,
            "downstream": downstream_asset,
            "impact_report_id": impact.get("report_id") if impact else None,
        })

        return {
            "lineage": lineage,
            "impact_analysis": impact,
            "governance_scoring": score,
            "pipeline": "lineage_track->impact_analysis->govern_score",
        }

    def _compute_impact_level(self, asset_code: str) -> int:
        """计算资产的影响层级（基于下游链长度）。"""
        downstream = self.get_downstream_chain(asset_code)
        return len(downstream) + 1

    def _trigger_impact_analysis(self, lineage: dict[str, Any]) -> dict[str, Any]:
        """触发影响分析报告生成。"""
        report_id = store.next_id("impact_analysis")
        report = {
            "id": report_id,
            "报告编号": f"IMPA-LINE-{report_id:04d}",
            "变更资产": lineage["上游资产"],
            "影响下游": lineage["下游资产"],
            "影响类型": "血缘变更",
            "严重级别": "需关注" if int(lineage.get("影响层级", "1")) >= 3 else "一般",
            "影响行数": "0",
            "补救建议": f"上游{lineage['上游资产']}的变更将通过关联字段{lineage['关联字段']}影响下游{lineage['下游资产']}",
            "生成时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": True,
            "abnormal": False,
            "lineage_id": lineage["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("impact_analysis").append(report)
        return {"report_id": report_id}

    def _trigger_governance_scoring(self, lineage: dict[str, Any]) -> dict[str, Any]:
        """触发治理评分计算。"""
        score_id = store.next_id("govern_score")
        score = {
            "id": score_id,
            "任务编号": f"GOVE-LINE-{score_id:04d}",
            "评估对象": lineage["下游资产"],
            "评分维度": "血缘完备性",
            "当前分数": "85",
            "改进基线": "确认血缘并填写转换逻辑",
            "评估周期": "按需",
            "预警级别": "正常",
            "整改状态": "待评估",
            "status": "待评估",
            "pending": True,
            "abnormal": False,
            "lineage_id": lineage["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("govern_score").append(score)
        return {"score_id": score_id, "dimension": "血缘完备性"}

    def get_upstream_assets(self, asset_code: str) -> list[str]:
        """获取上游资产列表。"""
        upstream = []
        for row in store.rows(self.MODULE):
            if row.get("下游资产") == asset_code:
                us = row.get("上游资产", "")
                if us and us not in upstream:
                    upstream.append(us)
        return upstream

    def get_downstream_chain(self, asset_code: str) -> list[str]:
        """获取下游资产链（递归）。"""
        visited = set()
        chain = []

        def dfs(current):
            if current in visited:
                return
            visited.add(current)
            for row in store.rows(self.MODULE):
                if row.get("上游资产") == current:
                    ds = row.get("下游资产", "")
                    if ds and ds not in visited:
                        chain.append(ds)
                        dfs(ds)

        dfs(asset_code)
        return chain

    def get_full_lineage(self, asset_code: str) -> dict[str, Any]:
        """获取资产的完整血缘关系。"""
        return {
            "asset": asset_code,
            "upstream": self.get_upstream_assets(asset_code),
            "downstream": self.get_downstream_chain(asset_code),
            "direct_relations": [
                {
                    "relation_id": row["关系编号"],
                    "upstream": row["上游资产"],
                    "downstream": row["下游资产"],
                    "join_fields": row["关联字段"],
                    "transform": row["转换逻辑"],
                }
                for row in store.rows(self.MODULE)
                if row.get("上游资产") == asset_code or row.get("下游资产") == asset_code
            ],
        }