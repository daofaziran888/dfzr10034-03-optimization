"""数据契约领域服务：数据生产者与消费者之间的Schema契约、SLA承诺和违约管理。

Pipeline 5: Data contract → schema validation → compliance scanning
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataContractService(BaseModuleService):
    MODULE = "data_contract"
    ENTITY = "契约定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["契约编号", "数据资产", "Schema约束", "SLA定义", "更新频率", "负责人", "消费方", "生效时间"]
    REQUIRED_FIELDS = ["契约编号", "数据资产", "Schema约束"]
    STATUS_ORDER = ["草稿", "已签署", "已违约", "已终止"]
    ACTION_RULES = {"签署契约": "已签署", "标记违约": "已违约", "终止契约": "已终止"}
    NEGATIVE_ACTIONS = ["标记违约", "终止契约"]

    def define_contract(self, asset_code: str, schema_constraints: dict[str, Any],
                        sla: str, frequency: str, provider: str, consumer: str) -> dict[str, Any]:
        """定义数据契约（Pipeline 5 入口）。"""
        contract_id = store.next_id(self.MODULE)
        contract = {
            "id": contract_id,
            "契约编号": f"CONT-{contract_id:04d}",
            "数据资产": asset_code,
            "Schema约束": schema_constraints.get("summary", str(schema_constraints)),
            "SLA定义": sla,
            "更新频率": frequency,
            "负责人": provider,
            "消费方": consumer,
            "生效时间": _time.strftime("%Y-%m-%d"),
            "status": "已签署",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(contract)

        # Pipeline 5: 签署契约后触发合规扫描
        scan_result = self._trigger_compliance_scan(contract, schema_constraints)

        store.log_change(
            self.MODULE, contract_id, "签署数据契约",
            before=None, after=f"契约资产{asset_code}, SLA:{sla}",
            source="pipeline_5", scope="data_contract->compliance_scan"
        )

        self.emit_event("contract_signed", {
            "contract_id": contract_id,
            "asset_code": asset_code,
            "provider": provider,
            "consumer": consumer,
        })

        return {
            "contract": contract,
            "compliance_scan": scan_result,
            "pipeline": "data_contract->compliance_scan",
        }

    def _trigger_compliance_scan(self, contract: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        """触发合规扫描。"""
        scan_id = store.next_id("compliance_scan")
        scan = {
            "id": scan_id,
            "任务编号": f"COMP-CONT-{scan_id:04d}",
            "目标资产": contract["数据资产"],
            "合规条款": f"契约合规校验 (contract #{contract['id']})",
            "检查规则": f"验证Schema约束: {contract.get('Schema约束', '')}",
            "扫描范围": "全量",
            "违规发现": "",
            "修复意见": "",
            "扫描时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已通过",
            "pending": False,
            "abnormal": False,
            "contract_id": contract["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("compliance_scan").append(scan)
        return {"scan_id": scan_id, "passed": True}

    def validate_schema(self, contract_id: int, actual_schema: dict[str, Any]) -> dict[str, Any]:
        """验证实际Schema是否符合契约（Pipeline 5 核心验证方法）。"""
        contract = self.get_entry(contract_id)
        if not contract:
            return {"ok": False, "error": "契约不存在"}

        # 比较实际Schema与契约约定的Schema约束
        expected = str(contract.get("Schema约束", ""))
        actual = str(actual_schema.get("summary", str(actual_schema)))
        valid = expected == actual

        if not valid:
            # 标记违约
            contract["status"] = "已违约"
            contract["abnormal"] = True

            # 生成合规扫描告警
            scan_id = store.next_id("compliance_scan")
            scan = {
                "id": scan_id,
                "任务编号": f"COMP-FAIL-{scan_id:04d}",
                "目标资产": contract["数据资产"],
                "合规条款": "Schema契约违规",
                "检查规则": f"实际Schema({actual})不符合契约({expected})",
                "扫描范围": "全量",
                "违规发现": f"资产{contract['数据资产']}的Schema已偏离契约定义",
                "修复意见": "需重新协商契约或回滚Schema变更",
                "扫描时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "存在违规",
                "pending": True,
                "abnormal": True,
                "contract_id": contract_id,
                "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            store.rows("compliance_scan").append(scan)

            # 生成治理问题
            issue_id = store.next_id("issue_track")
            issue = {
                "id": issue_id,
                "问题编号": f"ISSU-CONT-{issue_id:04d}",
                "关联资产": contract["数据资产"],
                "问题类型": "契约违约",
                "严重级别": "严重",
                "指派处理": contract.get("负责人", ""),
                "当前状态": "待处理",
                "修复方案": f"实际Schema({actual})不符合契约({expected})",
                "截止日期": _time.strftime("%Y-%m-%d", _time.localtime(_time.time() + 86400 * 3)),
                "status": "待确认",
                "pending": True,
                "abnormal": True,
                "contract_id": contract_id,
                "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            store.rows("issue_track").append(issue)

            self.emit_event("contract_violated", {
                "contract_id": contract_id,
                "asset_code": contract["数据资产"],
                "issue_id": issue_id,
            })

            return {
                "ok": False,
                "valid": False,
                "contract_id": contract_id,
                "expected": expected,
                "actual": actual,
                "issue_created": True,
                "issue_id": issue_id,
                "pipeline": "data_contract->compliance_scan->issue_track",
            }

        return {"ok": True, "valid": True, "contract_id": contract_id}

    def get_contracts_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有契约。"""
        return store.find_by_field(self.MODULE, "数据资产", asset_code)