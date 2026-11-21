"""问题追踪领域服务：数据治理问题的发现、指派、修复和闭环验证管理。

接收质量规则检查阻断、影响分析和合规扫描等过程的自动化问题创建。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services import breach_ledger as ledger
from app.services.base import BaseModuleService
from app.store import store


class IssueTrackService(BaseModuleService):
    MODULE = "issue_track"
    ENTITY = "治理问题"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["问题编号", "关联资产", "问题类型", "严重级别", "指派处理", "当前状态", "修复方案", "截止日期"]
    REQUIRED_FIELDS = ["问题编号", "关联资产", "问题类型"]
    STATUS_ORDER = ["待确认", "处理中", "已修复", "已验证"]
    ACTION_RULES = {"确认问题": "处理中", "指派处理": "处理中", "验证修复": "已验证"}
    NEGATIVE_ACTIONS = []

    def create_issue(self, asset_code: str, issue_type: str, severity: str,
                     assignee: str, solution: str, deadline_days: int = 7) -> dict[str, Any]:
        """创建治理问题。"""
        issue_id = store.next_id(self.MODULE)
        issue = {
            "id": issue_id,
            "问题编号": f"ISSU-{issue_id:04d}",
            "关联资产": asset_code,
            "问题类型": issue_type,
            "严重级别": severity,
            "指派处理": assignee,
            "当前状态": "待确认",
            "修复方案": solution,
            "截止日期": _time.strftime("%Y-%m-%d", _time.localtime(_time.time() + 86400 * deadline_days)),
            "status": "待确认",
            "pending": True,
            "abnormal": severity == "严重",
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(issue)

        # 发出事件通知
        self.emit_event("issue_created", {
            "issue_id": issue_id,
            "asset_code": asset_code,
            "issue_type": issue_type,
            "severity": severity,
            "assignee": assignee,
        })

        return issue

    def fix_issue(self, issue_id: int, fix_description: str) -> dict[str, Any] | None:
        """标记问题为已修复。

        质量越界工单的修复记录只追加到处理记录里，不覆盖首次越界值和复测信息；
        同一区间在验证前再次越界时，下一次质检会把工单重新打开。
        """
        issue = self.get_entry(issue_id)
        if not issue:
            return None

        old_status = issue.get("status", "")
        issue["status"] = "已修复"
        issue["pending"] = False
        issue["修复方案"] = f"{issue.get('修复方案', '')} | 实际修复: {fix_description}"
        issue["fixed_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        issue.setdefault("处理记录", []).append({
            "时间": issue["fixed_at"],
            "记录": f"处理人标记修复: {fix_description}",
            "类型": "修复",
        })

        interval = self._quality_interval(issue)
        if interval is not None:
            interval["修复状态"] = "已修复"
            interval["修复时间"] = issue["fixed_at"]
            interval["修复说明"] = fix_description
            # 修复后仍要复测确认，区间保持待复测，责任人不因再次越界而改变。
            if ledger.interval_is_open(interval):
                issue["复测负责人"] = interval.get("复测负责人") or issue.get("指派处理", "")

        store.log_change(
            self.MODULE, issue_id, "修复问题",
            before=old_status, after="已修复",
            source="fix", scope="issue_track"
        )

        self.emit_event("issue_fixed", {
            "issue_id": issue_id,
            "asset_code": issue.get("关联资产"),
            "interval_id": issue.get("区间ID"),
        })

        return issue

    def verify_issue(self, issue_id: int, verified_by: str) -> dict[str, Any] | None:
        """验证修复完成；质量越界工单验证后对应的区间不再接受新的越界归并。"""
        issue = self.get_entry(issue_id)
        if not issue:
            return None

        issue["status"] = "已验证"
        issue["pending"] = False
        issue["verified_by"] = verified_by
        issue["verified_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        issue.setdefault("处理记录", []).append({
            "时间": issue["verified_at"],
            "记录": f"复测验证通过: {verified_by}",
            "类型": "验证",
        })

        interval = self._quality_interval(issue)
        if interval is not None:
            # 验证通过说明最新一次复测已回到阈值内。
            ledger.close_interval(interval, status="已复测通过", operator=verified_by)
            if interval.get("最新复测值") in (None, ""):
                interval["最新复测值"] = issue.get("最新复测值", "")
                interval["最新复测时间"] = issue.get("最新复测时间", "")
                interval["最新复测结论"] = "已验证"
            issue["复测负责人"] = interval.get("复测负责人") or issue.get("指派处理", "")

        self.emit_event("issue_verified", {
            "issue_id": issue_id,
            "asset_code": issue.get("关联资产"),
            "interval_id": issue.get("区间ID"),
        })

        return issue

    def get_quality_detail(self, issue_id: int) -> dict[str, Any] | None:
        """取回工单对应的测点、首次越界值和最新一次复测。"""
        from app.services import QUALITY_SERVICE

        return QUALITY_SERVICE.get_issue_quality_detail(issue_id)

    def after_action(self, entry: dict[str, Any], action: str, old_status: Any) -> None:
        """通用动作接口与领域方法保持同一套越界区间联动。"""
        if action == "验证修复" and entry.get("status") == "已验证":
            interval = self._quality_interval(entry)
            if interval is not None and str(interval.get("状态")) not in ledger.CLOSED_INTERVAL_STATUSES:
                ledger.close_interval(
                    interval,
                    status="已复测通过",
                    operator=str(entry.get("指派处理") or ""),
                )
                interval["最新复测结论"] = "已验证"
                interval["pending"] = False
            entry["pending"] = False
            entry["abnormal"] = False

    def _quality_interval(self, issue: dict[str, Any]) -> dict[str, Any] | None:
        if issue.get("区间ID"):
            interval = ledger.get_interval(int(issue["区间ID"]))
            if interval is not None:
                return interval
        if issue.get("区间编号"):
            matches = [
                row for row in store.rows(ledger.INTERVAL_MODULE)
                if row.get("区间编号") == issue["区间编号"]
            ]
            if matches:
                return matches[0]
        return None

    def get_issues_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有治理问题。"""
        return store.find_by_field(self.MODULE, "关联资产", asset_code)

    def get_open_issues(self) -> list[dict[str, Any]]:
        """获取所有未关闭问题。"""
        return [
            r for r in store.rows(self.MODULE)
            if r.get("status") in ("待确认", "处理中")
        ]

    def get_issues_by_severity(self, severity: str) -> list[dict[str, Any]]:
        """按严重级别获取问题。"""
        return store.find_by_field(self.MODULE, "严重级别", severity)

    def get_issues_by_assignee(self, assignee: str) -> list[dict[str, Any]]:
        """按指派人获取问题。"""
        return store.find_by_field(self.MODULE, "指派处理", assignee)

    def issue_statistics(self) -> dict[str, Any]:
        """问题统计概览。"""
        all_issues = store.rows(self.MODULE)
        by_status = store.count_by_status(self.MODULE)
        by_severity = store.count_by_field(self.MODULE, "严重级别")

        return {
            "total": len(all_issues),
            "open": sum(1 for r in all_issues if r.get("status") in ("待确认", "处理中")),
            "resolved": sum(1 for r in all_issues if r.get("status") in ("已修复", "已验证")),
            "by_status": by_status,
            "by_severity": by_severity,
            "pipeline": "quality_rule->issue_track",
        }