"""操作日志领域服务：数据资产的操作记录、异常行为检测和合规联动。

Pipeline 8: Operation log → anomaly detection → compliance reporting
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class OperationLogService(BaseModuleService):
    MODULE = "operation_log"
    ENTITY = "操作记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "目标资产", "操作人", "操作类型", "操作时间", "来源节点", "处理结果", "异常结论"]
    REQUIRED_FIELDS = ["记录编号", "目标资产", "操作人"]
    STATUS_ORDER = ["已记录", "待确认", "已异常", "已核实"]
    ACTION_RULES = {"提交确认": "待确认", "标记异常": "已异常", "确认核实": "已核实"}
    NEGATIVE_ACTIONS = ["标记异常"]

    def record_action(self, asset_code: str, operator: str, action: str, source_node: str, result: str = "成功") -> dict[str, Any]:
        """记录一次数据操作（Pipeline 8 入口方法）。"""
        log_id = store.next_id(self.MODULE)
        record = {
            "id": log_id,
            "记录编号": f"OPER-{log_id:04d}",
            "目标资产": asset_code,
            "操作人": operator,
            "操作类型": action,
            "操作时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "来源节点": source_node,
            "处理结果": result,
            "异常结论": "待确认",
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(record)

        # 自动异常检测
        anomaly = self.detect_anomaly(record)
        if anomaly:
            record["status"] = "已异常"
            record["异常结论"] = anomaly["reason"]
            record["pending"] = True
            record["anomaly"] = anomaly

            # Pipeline 8: 异常检测 → 合规联动
            self._generate_compliance_alert(record, anomaly)
            self.emit_event("anomaly_detected", {
                "log_id": log_id,
                "asset_code": asset_code,
                "operator": operator,
                "anomaly": anomaly,
            })

        return record

    def detect_anomaly(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """检测异常操作行为。"""
        anomalies = []
        asset = record.get("目标资产", "")
        operator = record.get("操作人", "")
        action = record.get("操作类型", "")
        source = record.get("来源节点", "")

        # 检测规则1: 高危操作
        high_risk_actions = ["删除", "清空", "drop", "truncate", "delete", "注销"]
        if any(ra in action.lower() for ra in high_risk_actions):
            anomalies.append({"type": "高危操作", "detail": f"操作{action}属于高危类型"})

        # 检测规则2: 非工作时间操作（22:00-06:00）
        current_hour = int(_time.strftime("%H"))
        if current_hour >= 22 or current_hour < 6:
            anomalies.append({"type": "异常时间", "detail": f"在{current_hour}:00非工作时间操作"})

        # 检测规则3: 频繁操作同一资产
        recent = store.find_by_field(self.MODULE, "目标资产", asset)
        recent_same_operator = [
            r for r in recent
            if r.get("操作人") == operator and r.get("id") != record.get("id")
        ]
        if len(recent_same_operator) >= 3:
            anomalies.append({"type": "频繁操作", "detail": f"操作人{operator}短时间内频繁操作同一资产{asset}"})

        # 检测规则4: 非授权人员操作高敏感数据
        high_sensitive_assets = [
            c for c in store.rows("classification")
            if c.get("status") == "已确认" and c.get("敏感等级") == "高"
        ]
        high_sensitive_codes = {c.get("目标字段", "").split(".")[0] for c in high_sensitive_assets}
        if asset in high_sensitive_codes and operator != "admin":
            anomalies.append({"type": "敏感数据操作", "detail": f"非管理员{operator}操作了高敏感资产{asset}"})

        if anomalies:
            return {
                "detected": True,
                "anomalies": anomalies,
                "reason": "; ".join(a["detail"] for a in anomalies),
                "severity": "高" if len(anomalies) >= 2 else "中",
            }
        return None

    def _generate_compliance_alert(self, record: dict[str, Any], anomaly: dict[str, Any]) -> None:
        """生成合规扫描告警（Pipeline 8）。"""
        scan_id = store.next_id("compliance_scan")
        scan = {
            "id": scan_id,
            "任务编号": f"COMP-ALERT-{scan_id:04d}",
            "目标资产": record.get("目标资产", ""),
            "合规条款": "数据操作安全合规",
            "检查规则": f"自动触发的异常行为检测: {anomaly.get('reason', '')}",
            "扫描范围": "操作日志",
            "违规发现": f"操作人{record.get('操作人','')}在{record.get('操作时间','')}发生异常操作",
            "修复意见": "建议立即核实操作行为并评估数据安全风险",
            "扫描时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "存在违规",
            "pending": True,
            "abnormal": True,
            "triggered_by": "operation_log",
            "log_record_id": record["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("compliance_scan").append(scan)

    def get_operator_trail(self, operator: str) -> list[dict[str, Any]]:
        """获取指定操作人的操作轨迹记录。"""
        return store.find_by_field(self.MODULE, "操作人", operator)

    def get_asset_log(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有操作记录。"""
        return store.find_by_field(self.MODULE, "目标资产", asset_code)

    def get_anomalies(self) -> list[dict[str, Any]]:
        """获取所有异常操作记录。"""
        return [r for r in store.rows(self.MODULE) if r.get("status") == "已异常"]

    def generate_compliance_report(self) -> dict[str, Any]:
        """生成合规操作报告（Pipeline 8 汇总输出）。"""
        all_records = store.rows(self.MODULE)
        anomalies = self.get_anomalies()

        by_action = store.count_by_field(self.MODULE, "操作类型")
        by_operator = store.count_by_field(self.MODULE, "操作人")

        report = {
            "total_operations": len(all_records),
            "anomaly_count": len(anomalies),
            "anomaly_rate": round(len(anomalies) / max(len(all_records), 1), 4),
            "operations_by_action": by_action,
            "operations_by_operator": by_operator,
            "anomalies": anomalies,
            "generated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "pipeline": "operation_log->compliance_scan",
        }

        self.emit_event("compliance_report_generated", report)

        return report