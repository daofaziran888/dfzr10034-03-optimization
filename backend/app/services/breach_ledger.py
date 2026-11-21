"""质量越界区间台账：观测去重、规则版本和越界区间归并的共享领域逻辑。

质检的执行链路是 ``quality_rule -> quality_breach_interval -> issue_track``：

* 一次观测先落到 ``quality_observation``，用测点、观测值和观测时间做幂等键，
  同一测点重复提交（同一份报文被再次投递）只保留第一次；
* 只有越界观测才进入 ``quality_breach_interval``。同一规则、同一规则版本的
  观测按到达顺序归并到一个区间，区间记录首次/最新越界值和峰值；
* 区间首次建立时生成一张治理工单（``issue_track``）。工单未验证前再次越界
  继续并入原区间，工单验证后新的越界才另起一条调查；
* 阈值变更会让规则进入新版本，新版本的观测不会和旧版本区间混在一起。

这里刻意只依赖 ``store``，不反向 import 质量规则或问题追踪服务，避免循环依赖。
"""
from __future__ import annotations

import math
import time as _time
from typing import Any

from app.store import store

INTERVAL_MODULE = "quality_breach_interval"
OBSERVATION_MODULE = "quality_observation"

# 工单验证/区间关闭之后，新越界才会另起一条区间。
CLOSED_INTERVAL_STATUSES = {"已关闭", "已验证", "已复测通过", "已归档"}
OPEN_INTERVAL_STATUS = "待复测"

# 这些检查类型只关心上限；保留「一致性」的原语义（超过上限即越界）。
UPPER_BOUND_CHECK_TYPES = {
    "上限", "上限检查", "最高值", "最大值", "温度", "温度上限",
    "一致性", "consistency", "max", "upper", "ceiling",
}
# 完整性/准确性沿用原来的「低于下限」语义。
LOWER_BOUND_CHECK_TYPES = {
    "下限", "下限检查", "最低值", "最小值",
    "完整性", "completeness", "准确性", "accuracy", "min", "lower", "floor",
}


def _now() -> str:
    return _time.strftime("%Y-%m-%d %H:%M:%S")


def now() -> str:
    """当前时间戳，供其他领域服务复用同一格式。"""
    return _now()


def as_float(value: Any, default: float | None = None) -> float | None:
    """把配置里常见的空串、None 和字符串数字统一成 float。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "无"}:
        return default
    try:
        number = float(text)
    except (TypeError, ValueError):
        return default
    if math.isnan(number):
        return default
    return number


def format_number(value: Any) -> str:
    """稳定输出配置值，避免 8.5 / '8.5' / 8.50 被当成不同阈值。"""
    number = as_float(value, None)
    if number is None:
        return "" if value is None else str(value).strip()
    if number.is_integer():
        return str(int(number))
    return format(number, ".12g")


def threshold_signature(low: Any, high: Any) -> str:
    return f"{format_number(low)}|{format_number(high)}"


def rule_bounds(rule: dict[str, Any]) -> tuple[float, float, Any, Any]:
    """返回 (生效下限, 生效上限, 原始下限, 原始上限)。

    只配置上限（例如温度上限 8.5）时，下限用负无穷；只配置下限时上限用正无穷。
    """
    raw_low = rule.get("阈值下限", "")
    raw_high = rule.get("阈值上限", "")
    low = as_float(raw_low, None)
    high = as_float(raw_high, None)
    effective_low = low if low is not None else float("-inf")
    effective_high = high if high is not None else float("inf")
    return effective_low, effective_high, raw_low, raw_high


def rule_version(rule: dict[str, Any]) -> str:
    return str(rule.get("规则版本") or "1")


def ensure_rule_version(rule: dict[str, Any]) -> str:
    """确保规则版本和当前阈值一致。

    即使调用方绕过 ``revise_threshold`` 直接改了阈值字段，执行检查时也能发现
    阈值签名变化并自动递增版本，旧区间继续保留旧版本号。
    """
    raw_low = rule.get("阈值下限", "")
    raw_high = rule.get("阈值上限", "")
    signature = threshold_signature(raw_low, raw_high)
    history = rule.get("版本历史")
    if not isinstance(history, list):
        history = []
        rule["版本历史"] = history

    version = rule_version(rule)
    if not history:
        history.append({
            "版本": version,
            "阈值下限": raw_low,
            "阈值上限": raw_high,
            "生效时间": rule.get("created_at") or _now(),
            "变更原因": "初始版本",
        })
        rule["规则版本"] = version
        rule["阈值签名"] = signature
        return version

    last = history[-1]
    last_signature = threshold_signature(last.get("阈值下限"), last.get("阈值上限"))
    if rule.get("阈值签名") == signature and last_signature == signature:
        return version

    try:
        next_version = str(int(version) + 1)
    except (TypeError, ValueError):
        next_version = str(len(history) + 1)
    rule["规则版本"] = next_version
    rule["阈值签名"] = signature
    history.append({
        "版本": next_version,
        "阈值下限": raw_low,
        "阈值上限": raw_high,
        "生效时间": _now(),
        "变更原因": "阈值变更",
    })
    return next_version


def revise_rule_threshold(
    rule: dict[str, Any],
    threshold_low: Any = ...,
    threshold_high: Any = ...,
    reason: str = "",
    operator: str = "",
) -> dict[str, Any]:
    """修改阈值并生成新的规则版本。返回版本变更摘要。"""
    old_version = rule_version(rule)
    old_low, old_high = rule.get("阈值下限", ""), rule.get("阈值上限", "")
    if threshold_low is not ...:
        rule["阈值下限"] = threshold_low
    if threshold_high is not ...:
        rule["阈值上限"] = threshold_high

    new_low, new_high = rule.get("阈值下限", ""), rule.get("阈值上限", "")
    if threshold_signature(new_low, new_high) == threshold_signature(old_low, old_high):
        ensure_rule_version(rule)
        return {
            "changed": False,
            "old_version": old_version,
            "new_version": rule_version(rule),
            "old_threshold_low": old_low,
            "old_threshold_high": old_high,
            "threshold_low": new_low,
            "threshold_high": new_high,
        }

    # 直接追加新版本，ensure_rule_version 之后会以最新签名作为版本边界。
    try:
        next_version = str(int(old_version) + 1)
    except (TypeError, ValueError):
        next_version = str(len(rule.get("版本历史") or []) + 1)
    rule["规则版本"] = next_version
    rule["阈值签名"] = threshold_signature(new_low, new_high)
    rule.setdefault("版本历史", []).append({
        "版本": next_version,
        "阈值下限": new_low,
        "阈值上限": new_high,
        "生效时间": _now(),
        "变更原因": reason or "阈值变更",
        "操作人": operator,
    })
    rule["updated_at"] = _now()
    store.log_change(
        "quality_rule", int(rule.get("id", 0)), "阈值变更",
        before=f"v{old_version} [{old_low}, {old_high}]",
        after=f"v{next_version} [{new_low}, {new_high}]",
        source="quality_rule", scope="quality_rule->quality_breach_interval",
    )
    return {
        "changed": True,
        "old_version": old_version,
        "new_version": next_version,
        "old_threshold_low": old_low,
        "old_threshold_high": old_high,
        "threshold_low": new_low,
        "threshold_high": new_high,
    }


def evaluate(rule: dict[str, Any], value: float) -> tuple[bool, str]:
    """按检查类型判断是否越界，并返回可读的越界说明。"""
    low, high, _, _ = rule_bounds(rule)
    check_type = str(rule.get("检查类型", "")).strip()
    low_text = format_number(rule.get("阈值下限"))
    high_text = format_number(rule.get("阈值上限"))
    value_text = format_number(value)
    if check_type in UPPER_BOUND_CHECK_TYPES:
        breached = value > high
        if breached:
            return True, f"当前值{value_text}超过上限{high_text}"
        return False, f"当前值{value_text}未超过上限{high_text}"
    if check_type in LOWER_BOUND_CHECK_TYPES:
        breached = value < low
        if breached:
            return True, f"当前值{value_text}低于阈值{low_text}"
        return False, f"当前值{value_text}未低于阈值{low_text}"

    breached = value < low or value > high
    if breached:
        return True, f"当前值{value_text}不在[{low_text}, {high_text}]范围内"
    return False, f"当前值{value_text}在[{low_text}, {high_text}]范围内"


def _values_equal(left: Any, right: Any) -> bool:
    a, b = as_float(left, None), as_float(right, None)
    if a is not None and b is not None:
        return abs(a - b) < 1e-9
    return str(left) == str(right)


def find_observation(
    rule: dict[str, Any],
    *,
    point_code: str,
    value: float,
    observed_at: Any = None,
    observation_id: Any = None,
    message_id: Any = None,
    version: str | None = None,
    observed_at_explicit: bool = False,
) -> dict[str, Any] | None:
    """查找同版本、同测点、同值的既有观测。

    判重优先级：
    1. 外部记录编号相同，直接视为同一笔观测；
    2. 同一份报文再次投递（报文编号 + 测点 + 观测值相同），不论班次；
    3. 同测点同值：显式给出了不同观测时间时视为两次真实读数，
       没有显式时间（重复提交）时只算一次。
    """
    target_version = str(version if version is not None else rule_version(rule))
    rule_id = int(rule.get("id", 0))
    for obs in store.rows(OBSERVATION_MODULE):
        if int(obs.get("规则ID", 0) or 0) != rule_id:
            continue
        if str(obs.get("规则版本", "")) != target_version:
            continue
        if observation_id and str(obs.get("外部记录编号", "")) == str(observation_id):
            return obs
        same_point = str(obs.get("测点编号", "")) == str(point_code)
        same_value = _values_equal(obs.get("观测值"), value)
        if message_id and same_point and same_value:
            if str(obs.get("报文编号", "")) == str(message_id):
                return obs
        if not (same_point and same_value):
            continue
        existing_at = obs.get("观测时间")
        explicitly_timed = bool(obs.get("观测时间显式"))
        if observed_at_explicit and explicitly_timed and observed_at and existing_at:
            if str(observed_at) != str(existing_at):
                continue
        return obs
    return None


def record_observation(
    rule: dict[str, Any],
    *,
    point_code: str,
    value: float,
    observed_at: Any = None,
    shift: str | None = None,
    message_id: Any = None,
    observation_id: Any = None,
    operator: str = "",
    observed_at_explicit: bool = False,
) -> tuple[dict[str, Any], bool]:
    """写入一条观测；若是重复提交则返回既有记录，第二个返回值为 True。"""
    version = rule_version(rule)
    duplicate = find_observation(
        rule,
        point_code=point_code,
        value=value,
        observed_at=observed_at,
        observation_id=observation_id,
        message_id=message_id,
        version=version,
        observed_at_explicit=observed_at_explicit,
    )
    if duplicate is not None:
        return duplicate, True

    rows = store.rows(OBSERVATION_MODULE)
    obs_id = store.next_id(OBSERVATION_MODULE)
    same_rule = [
        row for row in rows
        if int(row.get("规则ID", 0) or 0) == int(rule.get("id", 0))
    ]
    sequence = max((int(row.get("观测顺序", 0) or 0) for row in same_rule), default=0) + 1
    observation = {
        "id": obs_id,
        "观测编号": f"QOBS-{obs_id:04d}",
        "规则ID": int(rule.get("id", 0)),
        "规则编号": rule.get("规则编号", ""),
        "规则版本": version,
        "测点编号": str(point_code),
        "目标资产": rule.get("目标资产", ""),
        "观测值": value,
        "观测时间": observed_at or _now(),
        "观测时间显式": bool(observed_at_explicit),
        "班次": shift or "",
        "报文编号": message_id or "",
        "外部记录编号": observation_id or "",
        "观测顺序": sequence,
        "是否越界": False,
        "区间编号": "",
        "区间ID": None,
        "操作人": operator,
        "created_at": _now(),
    }
    rows.append(observation)
    return observation, False


def interval_is_open(interval: dict[str, Any]) -> bool:
    if str(interval.get("状态") or interval.get("status") or "") in CLOSED_INTERVAL_STATUSES:
        return False
    issue_id = interval.get("工单ID")
    if issue_id:
        issue = store.find("issue_track", int(issue_id))
        if issue and str(issue.get("status", "")) in {"已验证", "已关闭"}:
            return False
    return True


def find_open_interval(rule: dict[str, Any], version: str | None = None) -> dict[str, Any] | None:
    """定位同一规则、同一版本仍待复测的区间。"""
    target_version = str(version if version is not None else rule_version(rule))
    rule_id = int(rule.get("id", 0))
    for interval in reversed(store.rows(INTERVAL_MODULE)):
        if int(interval.get("规则ID", 0) or 0) != rule_id:
            continue
        if str(interval.get("规则版本", "")) != target_version:
            continue
        if interval_is_open(interval):
            return interval
    return None


def create_interval(
    rule: dict[str, Any],
    *,
    point_code: str,
    inspector: str = "",
) -> dict[str, Any]:
    low, high, raw_low, raw_high = rule_bounds(rule)
    interval_id = store.next_id(INTERVAL_MODULE)
    interval = {
        "id": interval_id,
        "区间编号": f"QBRI-{interval_id:04d}",
        "规则ID": int(rule.get("id", 0)),
        "规则编号": rule.get("规则编号", ""),
        "规则版本": rule_version(rule),
        "测点编号": str(point_code),
        "目标资产": rule.get("目标资产", ""),
        "检查类型": rule.get("检查类型", ""),
        "阈值下限": raw_low,
        "阈值上限": raw_high,
        "生效下限": low,
        "生效上限": high,
        "首次越界值": None,
        "首次越界时间": "",
        "最新越界值": None,
        "最新越界时间": "",
        "峰值": None,
        "越界次数": 0,
        "越界测点数": 0,
        "观测明细": [],
        "复测记录": [],
        "最新复测值": "",
        "最新复测时间": "",
        "最新复测结论": "",
        "状态": OPEN_INTERVAL_STATUS,
        "status": OPEN_INTERVAL_STATUS,
        "pending": True,
        "abnormal": True,
        "工单ID": None,
        "工单编号": "",
        "处理人": inspector,
        "复测负责人": inspector,
        "修复状态": "未修复",
        "修复时间": "",
        "关闭时间": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    store.rows(INTERVAL_MODULE).append(interval)
    return interval


def _point_codes(interval: dict[str, Any]) -> set[str]:
    return {
        str(item.get("测点编号", ""))
        for item in interval.get("观测明细", [])
        if str(item.get("测点编号", ""))
    }


def append_breach(interval: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """把一条越界观测按顺序并入区间，并同步首次/最新/峰值。"""
    detail = {
        "序号": len(interval.get("观测明细", [])) + 1,
        "观测编号": observation.get("观测编号", ""),
        "测点编号": observation.get("测点编号", ""),
        "观测值": observation.get("观测值"),
        "观测时间": observation.get("观测时间", ""),
        "班次": observation.get("班次", ""),
        "报文编号": observation.get("报文编号", ""),
        "规则版本": observation.get("规则版本", ""),
    }
    interval.setdefault("观测明细", []).append(detail)
    value = observation.get("观测值")
    interval["越界次数"] = len(interval["观测明细"])
    interval["越界测点数"] = len(_point_codes(interval))
    if interval.get("首次越界值") is None:
        interval["首次越界值"] = value
        interval["首次越界时间"] = observation.get("观测时间", "")
    interval["最新越界值"] = value
    interval["最新越界时间"] = observation.get("观测时间", "")
    peak = as_float(interval.get("峰值"), None)
    current = as_float(value, None)
    if current is not None and (peak is None or abs(current) > abs(peak)):
        interval["峰值"] = value
    interval["状态"] = OPEN_INTERVAL_STATUS
    interval["status"] = OPEN_INTERVAL_STATUS
    interval["pending"] = True
    interval["abnormal"] = True
    interval["updated_at"] = _now()
    observation["是否越界"] = True
    observation["区间ID"] = interval["id"]
    observation["区间编号"] = interval["区间编号"]
    return interval


def record_retest(
    interval: dict[str, Any],
    observation: dict[str, Any],
    *,
    conclusion: str,
) -> dict[str, Any]:
    """记录一次复测值；复测不会自动关闭区间，仍由处理人确认。"""
    record = {
        "序号": len(interval.get("复测记录", [])) + 1,
        "观测编号": observation.get("观测编号", ""),
        "复测值": observation.get("观测值"),
        "复测时间": observation.get("观测时间", ""),
        "班次": observation.get("班次", ""),
        "报文编号": observation.get("报文编号", ""),
        "复测结论": conclusion,
        "规则版本": observation.get("规则版本", ""),
    }
    interval.setdefault("复测记录", []).append(record)
    interval["最新复测值"] = observation.get("观测值")
    interval["最新复测时间"] = observation.get("观测时间", "")
    interval["最新复测结论"] = conclusion
    interval["观测明细"] = list(interval.get("观测明细", []))
    interval["updated_at"] = _now()
    observation["区间ID"] = interval["id"]
    observation["区间编号"] = interval["区间编号"]
    return interval


def close_interval(interval: dict[str, Any], *, status: str = "已关闭", operator: str = "") -> dict[str, Any]:
    interval["状态"] = status
    interval["status"] = status
    interval["pending"] = False
    interval["abnormal"] = False
    interval["关闭时间"] = _now()
    interval["关闭人"] = operator
    interval["updated_at"] = _now()
    return interval


def get_interval(interval_id: int) -> dict[str, Any] | None:
    return store.find(INTERVAL_MODULE, int(interval_id))


def list_intervals(
    *,
    rule_id: int | None = None,
    rule_code: str | None = None,
    status: str | None = None,
    pending_only: bool = False,
    issue_id: int | None = None,
) -> list[dict[str, Any]]:
    results = list(store.rows(INTERVAL_MODULE))
    if rule_id is not None:
        results = [row for row in results if int(row.get("规则ID", 0) or 0) == int(rule_id)]
    if rule_code:
        results = [row for row in results if str(row.get("规则编号", "")) == str(rule_code)]
    if status:
        results = [
            row for row in results
            if str(row.get("状态") or row.get("status")) == str(status)
        ]
    if pending_only:
        results = [row for row in results if interval_is_open(row)]
    if issue_id is not None:
        results = [row for row in results if int(row.get("工单ID", 0) or 0) == int(issue_id)]
    return results


def list_observations(
    *,
    rule_id: int | None = None,
    rule_code: str | None = None,
    interval_id: int | None = None,
    point_code: str | None = None,
    breached_only: bool = False,
    version: str | None = None,
) -> list[dict[str, Any]]:
    results = list(store.rows(OBSERVATION_MODULE))
    if rule_id is not None:
        results = [row for row in results if int(row.get("规则ID", 0) or 0) == int(rule_id)]
    if rule_code:
        results = [row for row in results if str(row.get("规则编号", "")) == str(rule_code)]
    if interval_id is not None:
        results = [row for row in results if int(row.get("区间ID", 0) or 0) == int(interval_id)]
    if point_code:
        results = [row for row in results if str(row.get("测点编号", "")) == str(point_code)]
    if version is not None:
        results = [row for row in results if str(row.get("规则版本", "")) == str(version)]
    if breached_only:
        results = [row for row in results if row.get("是否越界")]
    results.sort(key=lambda row: (int(row.get("观测顺序", 0) or 0), int(row.get("id", 0) or 0)))
    return results


def issue_for_interval(interval: dict[str, Any]) -> dict[str, Any] | None:
    issue_id = interval.get("工单ID")
    if not issue_id:
        return None
    return store.find("issue_track", int(issue_id))


def default_point_code(rule: dict[str, Any]) -> str:
    """规则没有单列测点编号时，用规则所属资产作为默认测点。"""
    return str(rule.get("测点编号") or rule.get("目标资产") or rule.get("规则编号") or "默认测点")
