"""脱敏引擎领域服务：字段级脱敏算法的配置、可逆性控制和密钥版本管理。

接收分级标签，对敏感字段应用脱敏规则。
"""
from __future__ import annotations

import hashlib
import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataAnonymService(BaseModuleService):
    MODULE = "data_anonym"
    ENTITY = "脱敏规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "目标字段", "脱敏算法", "保留位数", "替换字符", "适用场景", "可逆标志", "密钥版本"]
    REQUIRED_FIELDS = ["规则编号", "目标字段", "脱敏算法"]
    STATUS_ORDER = ["草稿", "已审查", "已生效", "已停用"]
    ACTION_RULES = {"提交审查": "已审查", "启效规则": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def apply_anonymization(self, rule_id: int, value: str) -> dict[str, Any]:
        """应用脱敏规则处理数据值。"""
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "error": "脱敏规则不存在", "result": value}

        algorithm = rule.get("脱敏算法", "mask")
        preserve = int(rule.get("保留位数", "0") or 0)
        replace_char = rule.get("替换字符", "*")
        original = value

        if algorithm == "mask":
            # 掩码脱敏：保留前preserve位，其余替换
            if len(value) <= preserve:
                result = value
            else:
                result = value[:preserve] + replace_char * (len(value) - preserve)
        elif algorithm == "hash":
            # 哈希脱敏：SHA-256后取前16位
            result = hashlib.sha256(value.encode()).hexdigest()[:16]
        elif algorithm == "truncate":
            # 截断脱敏
            result = value[:preserve] if preserve > 0 else ""
        elif algorithm == "replace":
            # 替换脱敏
            result = replace_char * len(value)
        elif algorithm == "reversible":
            # 可逆脱敏（带密钥）
            key_version = rule.get("密钥版本", "v1")
            key_material = f"{rule.get('规则编号','')}:{key_version}"
            result = self._reversible_encode(value, key_material)
        else:
            result = value

        # 记录审计日志
        self._log_anonymization(rule, original, result)

        return {
            "ok": True,
            "rule_id": rule_id,
            "algorithm": algorithm,
            "original_length": len(original),
            "result": result,
        }

    def _reversible_encode(self, value: str, key: str) -> str:
        """可逆编码（带密钥）。"""
        key_hash = hashlib.sha256(key.encode()).digest()
        result = []
        for i, ch in enumerate(value):
            kb = key_hash[i % len(key_hash)]
            result.append(chr(ord(ch) ^ kb % 256))
        return ''.join(result)

    def _log_anonymization(self, rule: dict[str, Any], original: str, result: str) -> None:
        """将脱敏操作记录到操作日志表（Pipeline 8）。"""
        log_id = store.next_id("operation_log")
        log_entry = {
            "id": log_id,
            "记录编号": f"OPER-ANON-{log_id:04d}",
            "目标资产": rule.get("目标字段", ""),
            "操作人": "脱敏引擎",
            "操作类型": f"脱敏-{rule.get('脱敏算法', '')}",
            "操作时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "来源节点": "localhost",
            "处理结果": "成功" if result != original else "跳过",
            "异常结论": "自动",
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("operation_log").append(log_entry)

    def anonymize_dataset(self, asset_code: str, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """对整个数据集应用脱敏规则，基于分级标签。"""
        # 获取所有已生效的脱敏规则
        anonym_rules = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已生效"
        ]

        # 获取该资产的分级标签
        classifications = [
            c for c in store.rows("classification")
            if c.get("status") == "已确认" and str(c.get("目标字段", "")).startswith(f"{asset_code}.")
        ]

        result_records = []
        anonymized_fields: dict[str, list[str]] = {}
        for record in records:
            new_record = dict(record)
            for cls in classifications:
                field_path = cls.get("目标字段", "")
                parts = field_path.split(".")
                if len(parts) < 2:
                    continue
                field_name = parts[-1]

                # 找到匹配的脱敏规则
                matching_rules = [r for r in anonym_rules if r.get("目标字段", "") == field_path]
                if matching_rules:
                    rule = matching_rules[0]
                    original = str(new_record.get(field_name, ""))
                    result = self.apply_anonymization(rule["id"], original)
                    new_record[field_name] = result["result"]
                    anonymized_fields.setdefault(rule["规则编号"], []).append(field_name)

            result_records.append(new_record)

        self.emit_event("anonymization_completed", {
            "asset_code": asset_code,
            "records_count": len(records),
            "fields_anonymized": sum(len(v) for v in anonymized_fields.values()),
        })

        return result_records, anonymized_fields