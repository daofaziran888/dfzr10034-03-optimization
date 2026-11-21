"""质量规则领域服务：完整性、准确性、一致性和及时性规则定义与阻断策略。

Pipeline 3: Quality rule definition → monitoring → breach interval → issue tracking

越界处理约定（见 ``app/services/breach_ledger.py``）：

* 同一规则、同一规则版本的测点观测按观测顺序归并成一条越界区间；
* 工单未验证前，修复后再次越界继续并入原区间，复测责任人不变；
* 工单验证后，新越界才另起区间和调查工单；
* 同一测点重复提交的观测只计一次，换过阈值的观测各自带版本号。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services import breach_ledger as ledger
from app.services.base import BaseModuleService
from app.store import store


class QualityRuleService(BaseModuleService):
    MODULE = "quality_rule"
    ENTITY = "质量检查"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "目标资产", "检查类型", "阈值下限", "阈值上限", "阻断级别", "采样比例", "最近检查"]
    REQUIRED_FIELDS = ["规则编号", "目标资产", "检查类型"]
    STATUS_ORDER = ["草稿", "监控中", "已阻断", "已暂停"]
    ACTION_RULES = {"启用监控": "监控中", "确认阻断": "已阻断", "暂停规则": "已暂停"}
    NEGATIVE_ACTIONS = ["确认阻断"]

    def define_rule(self, asset_code: str, check_type: str, threshold_low: Any, threshold_high: Any,
                    severity: str, *, point_code: str | None = None,
                    inspector: str = "数据质量管理员",
                    rule_code: str | None = None) -> dict[str, Any]:
        """定义质量规则（Pipeline 3 入口）。

        ``point_code`` 是测点编号，未提供时按目标资产归口；``inspector`` 是首次
        越界工单的处置人，也是后续复测负责人。
        """
        rule_id = store.next_id(self.MODULE)
        low_text = ledger.format_number(threshold_low)
        high_text = ledger.format_number(threshold_high)
        rule = {
            "id": rule_id,
            "规则编号": rule_code or f"QUAL-{rule_id:04d}",
            "目标资产": asset_code,
            "测点编号": point_code or asset_code,
            "检查类型": check_type,
            "阈值下限": threshold_low,
            "阈值上限": threshold_high,
            "阈值下限文本": low_text,
            "阈值上限文本": high_text,
            "阻断级别": severity,
            "采样比例": "100",
            "最近检查": "",
            "规则版本": "1",
            "阈值签名": ledger.threshold_signature(threshold_low, threshold_high),
            "版本历史": [{
                "版本": "1",
                "阈值下限": threshold_low,
                "阈值上限": threshold_high,
                "生效时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "变更原因": "初始版本",
            }],
            "处置人": inspector,
            "复测负责人": inspector,
            "status": "监控中",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(rule)
        return rule

    def after_create(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """通用登记接口创建规则时补齐版本、测点和处置人。"""
        ledger.ensure_rule_version(entry)
        entry["测点编号"] = (
            str(values.get("测点编号") or "").strip()
            or entry.get("测点编号")
            or entry.get("目标资产", "")
        )
        inspector = str(values.get("处置人") or values.get("复测负责人") or "").strip()
        entry["处置人"] = inspector or "数据质量管理员"
        entry["复测负责人"] = entry["处置人"]
        entry["阈值下限文本"] = ledger.format_number(entry.get("阈值下限"))
        entry["阈值上限文本"] = ledger.format_number(entry.get("阈值上限"))

    # ── 阈值与规则版本 ────────────────────────────────────────

    def revise_threshold(self, rule_id: int, *, threshold_low: Any = ...,
                         threshold_high: Any = ..., reason: str = "",
                         operator: str = "") -> dict[str, Any]:
        """调整阈值：生成新规则版本，旧版本区间继续保留原版本号。"""
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "error": "规则不存在"}
        change = ledger.revise_rule_threshold(
            rule, threshold_low, threshold_high, reason=reason, operator=operator)
        rule["阈值下限文本"] = ledger.format_number(rule.get("阈值下限"))
        rule["阈值上限文本"] = ledger.format_number(rule.get("阈值上限"))
        return {"ok": True, "rule_id": rule_id, **change}

    def get_rule_versions(self, rule_id: int) -> dict[str, Any]:
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "error": "规则不存在"}
        ledger.ensure_rule_version(rule)
        intervals = store.find_by_field(ledger.INTERVAL_MODULE, "规则ID", str(rule_id))
        by_version: dict[str, list[dict[str, Any]]] = {}
        for interval in intervals:
            by_version.setdefault(str(interval.get("规则版本", "")), []).append(interval)
        return {
            "ok": True,
            "rule_id": rule_id,
            "规则编号": rule.get("规则编号", ""),
            "当前版本": ledger.rule_version(rule),
            "版本历史": rule.get("版本历史", []),
            "区间版本": {
                version: [
                    {"区间编号": item.get("区间编号"), "状态": item.get("状态")}
                    for item in items
                ]
                for version, items in by_version.items()
            },
        }

    # ── 观测与越界区间 ────────────────────────────────────────

    def execute_check(self, rule_id: int, current_value: float, *,
                      point_code: str | None = None,
                      observed_at: str | None = None,
                      shift: str | None = None,
                      message_id: Any = None,
                      observation_id: Any = None,
                      operator: str = "",
                      inspector: str | None = None) -> dict[str, Any]:
        """执行质量检查；越界观测按规则版本归并到越界区间并联动工单。"""
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "error": "规则不存在"}

        try:
            value = float(current_value)
        except (TypeError, ValueError):
            return {"ok": False, "error": "观测值不是有效数字"}

        ledger.ensure_rule_version(rule)
        version = ledger.rule_version(rule)
        low, high, _, _ = ledger.rule_bounds(rule)
        breached, breach_detail = ledger.evaluate(rule, value)
        point = str(point_code or ledger.default_point_code(rule))

        check_time = observed_at or _time.strftime("%Y-%m-%d %H:%M:%S")
        rule["最近检查"] = check_time
        rule["最近观测值"] = value

        observation, duplicate = ledger.record_observation(
            rule,
            point_code=point,
            value=value,
            observed_at=observed_at,
            shift=shift,
            message_id=message_id,
            observation_id=observation_id,
            operator=operator,
            observed_at_explicit=observed_at is not None,
        )

        result: dict[str, Any] = {
            "ok": True,
            "rule_id": rule_id,
            "rule_version": version,
            "point_code": point,
            "current_value": value,
            "threshold_low": low if low != float("-inf") else None,
            "threshold_high": high if high != float("inf") else None,
            "breached": breached,
            "breach_detail": breach_detail,
            "check_time": check_time,
            "observation_id": observation["id"],
            "observation_no": observation.get("观测编号"),
            "duplicate": duplicate,
            "counted": not duplicate,
        }
        if duplicate:
            result["deduplicated"] = True
            result["duplicate_of"] = observation.get("观测编号")
            result["message"] = (
                f"测点{point}的观测值{ledger.format_number(value)}已在{observation.get('观测时间')}记录，"
                f"重复提交不重复计为越界"
            )

        if breached:
            self._handle_breach(rule, observation, point, inspector or rule.get("处置人")
                                or "数据质量管理员", result)
        else:
            self._handle_in_range(rule, observation, point, version, result)

        return result

    def _handle_breach(self, rule: dict[str, Any], observation: dict[str, Any], point: str,
                       inspector: str, result: dict[str, Any]) -> None:
        if observation.get("区间ID"):
            interval = ledger.get_interval(int(observation["区间ID"]))
        else:
            interval = ledger.find_open_interval(rule, observation.get("规则版本"))

        created_interval = False
        if interval is None:
            interval = ledger.create_interval(rule, point_code=point, inspector=inspector)
            created_interval = True

        if not observation.get("是否越界"):
            ledger.append_breach(interval, observation)
        elif int(observation.get("区间ID") or 0) != int(interval["id"]):
            # 重复提交已有观测：只校正指向，不重复累加越界明细。
            observation["区间ID"] = interval["id"]
            observation["区间编号"] = interval["区间编号"]

        issue = ledger.issue_for_interval(interval)
        issue_reopened = False
        issue_created = False
        if issue is None:
            issue = self._create_quality_issue(rule, interval, observation, inspector)
            issue_created = True
        elif not result.get("duplicate"):
            # 重复投递的老观测不重复打扰处理人。
            issue_reopened = self._sync_issue_with_breach(issue, rule, interval, observation)

        interval["工单ID"] = issue["id"]
        interval["工单编号"] = issue.get("问题编号", "")
        interval["处理人"] = issue.get("指派处理") or inspector
        interval["复测负责人"] = issue.get("指派处理") or inspector
        interval["updated_at"] = ledger.now()

        rule["status"] = "已阻断"
        rule["abnormal"] = True
        rule["pending"] = True

        result.update({
            "interval_id": interval["id"],
            "interval_no": interval["区间编号"],
            "interval_status": interval.get("状态"),
            "interval_created": created_interval,
            "interval_first_value": interval.get("首次越界值"),
            "interval_latest_value": interval.get("最新越界值"),
            "breach_points": [item.get("测点编号") for item in interval.get("观测明细", [])],
            "issue_created": issue_created,
            "issue_reopened": issue_reopened,
            "issue": issue,
        })

        if not result.get("duplicate"):
            store.log_change(
                self.MODULE, int(rule["id"]), "质量检查阻断",
                before="监控中", after="已阻断",
                source="pipeline_3", scope="quality_rule->quality_breach_interval->issue_track",
            )
            self.emit_event("quality_breached", {
                "rule_id": rule["id"],
                "rule_version": observation.get("规则版本"),
                "asset_code": rule.get("目标资产"),
                "point_code": point,
                "interval_id": interval["id"],
                "issue_id": issue["id"],
                "value": observation.get("观测值"),
            })

    def _handle_in_range(self, rule: dict[str, Any], observation: dict[str, Any], point: str,
                         version: str, result: dict[str, Any]) -> None:
        interval = ledger.find_open_interval(rule, version)
        if interval is None:
            result["retest_recorded"] = False
            return
        if observation.get("区间ID") and int(observation["区间ID"]) != int(interval["id"]):
            result["retest_recorded"] = False
            return
        if not observation.get("复测已记录"):
            conclusion = "回落到阈值内，待确认复测结论"
            ledger.record_retest(interval, observation, conclusion=conclusion)
            observation["复测已记录"] = True
            observation["复测结论"] = conclusion
        result.update({
            "retest_recorded": not result.get("duplicate"),
            "interval_id": interval["id"],
            "interval_no": interval["区间编号"],
            "interval_status": interval.get("状态"),
            "latest_retest_value": interval.get("最新复测值"),
            "latest_retest_time": interval.get("最新复测时间"),
            "issue_id": interval.get("工单ID"),
        })
        if not result.get("duplicate"):
            issue = ledger.issue_for_interval(interval)
            if issue is not None:
                self._append_issue_note(
                    issue,
                    f"复测值{ledger.format_number(observation.get('观测值'))}已回到阈值内"
                    f"（{observation.get('观测时间')}，{observation.get('班次') or '未标注班次'}）",
                )
            self.emit_event("quality_retest_recorded", {
                "rule_id": rule["id"],
                "rule_version": rule.get("规则版本"),
                "interval_id": interval["id"],
                "value": observation.get("观测值"),
            })

    def record_retest(self, interval_id: int, value: float, *,
                      observed_at: str | None = None,
                      shift: str | None = None,
                      message_id: Any = None,
                      observation_id: Any = None,
                      operator: str = "") -> dict[str, Any]:
        """显式登记一次复测，自动定位区间对应的规则和测点。"""
        interval = ledger.get_interval(interval_id)
        if interval is None:
            return {"ok": False, "error": "越界区间不存在"}
        rule = self.get_entry(int(interval.get("规则ID", 0)))
        if rule is None:
            return {"ok": False, "error": "区间关联的质量规则不存在"}
        result = self.execute_check(
            int(rule["id"]), value,
            point_code=str(interval.get("测点编号") or ledger.default_point_code(rule)),
            observed_at=observed_at,
            shift=shift,
            message_id=message_id,
            observation_id=observation_id,
            operator=operator,
        )
        if result.get("ok") and not result.get("breached") and result.get("interval_id") != interval_id:
            # 规则版本已切换等情况下，把复测值补记到指定区间。
            observation = store.find(ledger.OBSERVATION_MODULE, int(result["observation_id"]))
            if observation is not None and not observation.get("复测已记录"):
                ledger.record_retest(
                    interval, observation,
                    conclusion="回落到阈值内，待确认复测结论",
                )
                observation["复测已记录"] = True
                result["interval_id"] = interval_id
                result["latest_retest_value"] = interval.get("最新复测值")
        return result

    def get_open_intervals(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        return ledger.list_intervals(rule_id=rule_id, pending_only=True)

    def get_interval_detail(self, interval_id: int) -> dict[str, Any] | None:
        interval = ledger.get_interval(interval_id)
        if interval is None:
            return None
        rule = self.get_entry(int(interval.get("规则ID", 0)))
        issue = ledger.issue_for_interval(interval)
        observations = ledger.list_observations(interval_id=interval_id)
        return {
            **interval,
            "rule_version": interval.get("规则版本"),
            "区间明细": interval.get("观测明细", []),
            "观测记录": observations,
            "首次越界值": interval.get("首次越界值"),
            "最新越界值": interval.get("最新越界值"),
            "最新复测值": interval.get("最新复测值"),
            "最新复测时间": interval.get("最新复测时间"),
            "工单": issue,
            "规则": {
                "规则编号": rule.get("规则编号") if rule else "",
                "检查类型": rule.get("检查类型") if rule else "",
                "当前版本": ledger.rule_version(rule) if rule else "",
                "阈值下限": rule.get("阈值下限") if rule else interval.get("阈值下限"),
                "阈值上限": rule.get("阈值上限") if rule else interval.get("阈值上限"),
            },
        }

    def write_conclusion(self, interval_id: int, conclusion: str, *,
                         operator: str = "", close: bool = False) -> dict[str, Any]:
        """把质检结论回写到工单待办，保留原有修复记录并追加结论。"""
        interval = ledger.get_interval(interval_id)
        if interval is None:
            return {"ok": False, "error": "越界区间不存在"}
        issue = ledger.issue_for_interval(interval)
        if issue is None:
            return {"ok": False, "error": "区间尚未关联工单"}

        self._append_issue_note(
            issue,
            f"质检结论: {conclusion}",
            extra={
                "最新复测值": interval.get("最新复测值"),
                "最新复测时间": interval.get("最新复测时间"),
                "区间编号": interval.get("区间编号"),
                "规则版本": interval.get("规则版本"),
            },
        )
        interval["质检结论"] = conclusion
        interval["结论回写时间"] = ledger.now()
        interval["结论回写人"] = operator
        if interval.get("最新复测值") not in (None, ""):
            interval["最新复测结论"] = conclusion

        if close:
            final_status = "已复测通过" if "通过" in conclusion or "正常" in conclusion else "已关闭"
            ledger.close_interval(interval, status=final_status, operator=operator)
            issue["status"] = "已验证"
            issue["当前状态"] = "已验证"
            issue["pending"] = False
            issue["abnormal"] = False
            issue["verified_by"] = operator or issue.get("verified_by", "")
            issue["verified_at"] = ledger.now()
            issue.setdefault("处理记录", []).append({
                "时间": issue["verified_at"],
                "记录": f"按质检结论验证: {conclusion}",
                "类型": "验证",
            })
            store.log_change(
                "issue_track", int(issue["id"]), "质量结论验证",
                before="已修复", after="已验证",
                source="quality_rule", scope="quality_rule->quality_breach_interval->issue_track",
            )
        else:
            issue["最新复测值"] = interval.get("最新复测值")
            issue["最新复测时间"] = interval.get("最新复测时间")
            issue["最新复测结论"] = interval.get("最新复测结论") or conclusion
            issue["pending"] = True

        return {
            "ok": True,
            "interval": interval,
            "issue": issue,
            "closed": close,
            "message": f"质检结论已回写工单{issue.get('问题编号', '')}",
        }

    def get_issue_quality_detail(self, issue_id: int) -> dict[str, Any] | None:
        """按工单取回关联区间、首次越界值和最新一次复测。"""
        issue = store.find("issue_track", issue_id)
        if issue is None:
            return None
        intervals = ledger.list_intervals(issue_id=issue_id)
        if not intervals and issue.get("区间ID"):
            interval = ledger.get_interval(int(issue["区间ID"]))
            if interval is not None:
                intervals = [interval]
        if not intervals and issue.get("区间编号"):
            intervals = [
                row for row in store.rows(ledger.INTERVAL_MODULE)
                if row.get("区间编号") == issue["区间编号"]
            ]

        summaries = []
        for interval in intervals:
            summaries.append({
                "区间ID": interval.get("id"),
                "区间编号": interval.get("区间编号"),
                "规则编号": interval.get("规则编号"),
                "规则版本": interval.get("规则版本"),
                "测点编号": interval.get("测点编号"),
                "状态": interval.get("状态"),
                "首次越界值": interval.get("首次越界值"),
                "首次越界时间": interval.get("首次越界时间"),
                "最新越界值": interval.get("最新越界值"),
                "最新越界时间": interval.get("最新越界时间"),
                "越界次数": interval.get("越界次数"),
                "越界测点数": interval.get("越界测点数"),
                "越界测点": sorted(ledger._point_codes(interval)),
                "越界明细": interval.get("观测明细", []),
                "最新复测值": interval.get("最新复测值"),
                "最新复测时间": interval.get("最新复测时间"),
                "最新复测结论": interval.get("最新复测结论"),
                "复测负责人": interval.get("复测负责人"),
                "仍待复测": ledger.interval_is_open(interval),
            })
        first = summaries[0] if summaries else None
        points: list[str] = []
        for summary in summaries:
            for point in summary["越界测点"]:
                if point not in points:
                    points.append(point)
        return {
            "工单ID": issue.get("id"),
            "工单编号": issue.get("问题编号"),
            "关联资产": issue.get("关联资产"),
            "测点编号": "、".join(points) or issue.get("测点编号"),
            "越界测点": points,
            "工单状态": issue.get("status"),
            "处理人": issue.get("指派处理"),
            "首次越界值": first["首次越界值"] if first else issue.get("首次越界值"),
            "最新复测值": first["最新复测值"] if first else issue.get("最新复测值"),
            "最新复测时间": first["最新复测时间"] if first else issue.get("最新复测时间"),
            "最新复测结论": first["最新复测结论"] if first else issue.get("最新复测结论"),
            "修复方案": issue.get("修复方案", ""),
            "修复时间": issue.get("fixed_at", ""),
            "区间": summaries,
            "仍待复测区间数": sum(1 for item in summaries if item["仍待复测"]),
        }

    # ── 工单写入 ──────────────────────────────────────────────

    def _create_quality_issue(self, rule: dict[str, Any], interval: dict[str, Any],
                              observation: dict[str, Any], inspector: str) -> dict[str, Any]:
        """按现有 issue_track 数据流生成工单，并带上区间与测点索引。"""
        issue_id = store.next_id("issue_track")
        severity_map = {"阻断": "严重", "警告": "需关注", "提示": "一般"}
        first_value = interval.get("首次越界值")
        issue = {
            "id": issue_id,
            "问题编号": f"ISSU-{issue_id:04d}",
            "关联资产": rule.get("目标资产", ""),
            "问题类型": f"质量{rule.get('检查类型', '检查')}异常",
            "严重级别": severity_map.get(rule.get("阻断级别", "提示"), "一般"),
            "指派处理": inspector,
            "当前状态": "待处理",
            "修复方案": (
                f"规则{rule.get('规则编号', '')}(v{interval.get('规则版本')})"
                f"在测点{observation.get('测点编号')}首次越界: "
                f"{ledger.format_number(first_value)}"
                f"（阈值上限{ledger.format_number(rule.get('阈值上限'))}）"
            ),
            "截止日期": _time.strftime("%Y-%m-%d", _time.localtime(_time.time() + 86400 * 7)),
            "status": "待确认",
            "pending": True,
            "abnormal": True,
            "规则ID": rule.get("id"),
            "规则编号": rule.get("规则编号", ""),
            "规则版本": interval.get("规则版本"),
            "测点编号": observation.get("测点编号"),
            "区间ID": interval.get("id"),
            "区间编号": interval.get("区间编号"),
            "首次越界值": first_value,
            "首次越界时间": interval.get("首次越界时间"),
            "最新越界值": interval.get("最新越界值"),
            "复测负责人": inspector,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("issue_track").append(issue)
        interval["工单ID"] = issue_id
        interval["工单编号"] = issue["问题编号"]
        return issue

    def _sync_issue_with_breach(self, issue: dict[str, Any], rule: dict[str, Any],
                                interval: dict[str, Any], observation: dict[str, Any]) -> bool:
        """把新增越界同步到既有工单；已修复的工单会被重新打开。"""
        reopened = False
        note = (
            f"测点{observation.get('测点编号')}在{observation.get('观测时间')}"
            f"（{observation.get('班次') or '未标注班次'}）再次越界"
            f"{ledger.format_number(observation.get('观测值'))}，已并入区间{interval.get('区间编号')}"
        )
        if issue.get("status") == "已修复":
            note += "，修复后复检仍越界，重新打开"
            issue["status"] = "处理中"
            issue["当前状态"] = "处理中"
            issue["pending"] = True
            issue["abnormal"] = True
            issue["重新打开时间"] = ledger.now()
            reopened = True
        elif issue.get("status") == "待确认":
            issue["当前状态"] = "待处理"

        self._append_issue_note(issue, note)
        issue["规则版本"] = interval.get("规则版本")
        issue["区间ID"] = interval.get("id")
        issue["区间编号"] = interval.get("区间编号")
        issue["最新越界值"] = interval.get("最新越界值")
        issue["最新越界时间"] = interval.get("最新越界时间")
        issue["越界次数"] = interval.get("越界次数")
        issue["越界测点"] = sorted(ledger._point_codes(interval))
        if rule.get("处置人") and not issue.get("指派处理"):
            issue["指派处理"] = rule["处置人"]
        return reopened

    def _append_issue_note(self, issue: dict[str, Any], note: str,
                           extra: dict[str, Any] | None = None) -> None:
        """追加记录，不覆盖处理人已经写下的修复方案和修复时间。"""
        current = str(issue.get("修复方案", "") or "").strip()
        appended = f"{current} | {note}" if current and current != "-" else note
        issue["修复方案"] = appended
        issue.setdefault("处理记录", []).append({
            "时间": ledger.now(),
            "记录": note,
        })
        if extra:
            for key, value in extra.items():
                if value is not None:
                    issue[key] = value
        issue["updated_at"] = ledger.now()

    # ── 批量检查 ──────────────────────────────────────────────

    def batch_check_assets(self, asset_code: str, metrics: dict[str, float]) -> list[dict[str, Any]]:
        """对资产批量执行所有关联质量规则检查。"""
        results = []
        rules = store.find_by_field(self.MODULE, "目标资产", asset_code)
        for rule in rules:
            metric_key = rule.get("检查类型", "")
            value = metrics.get(metric_key, 0)
            result = self.execute_check(rule["id"], value)
            results.append(result)
        return results

    def get_rules_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有质量规则。"""
        return store.find_by_field(self.MODULE, "目标资产", asset_code)
