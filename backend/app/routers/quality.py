"""质量越界专用接口：观测登记、阈值版本、越界区间和工单回写。

通用资源路由只支持「按模块增删改查」，而质检需要带上测点、观测时间、班次和
报文编号，因此单独开一组接口。必须在通用路由之前注册，避免
``/api/quality_rule/versions`` 被 ``/{module}/{entry_id}`` 抢走。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import QUALITY_SERVICE
from app.services import breach_ledger as ledger

router = APIRouter(prefix="/api/quality_rule", tags=["质量越界"])


class CheckPayload(BaseModel):
    value: float
    point_code: str | None = None
    observed_at: str | None = None
    shift: str | None = None
    message_id: str | int | None = None
    observation_id: str | int | None = None
    operator: str = ""
    inspector: str | None = None


class ThresholdPayload(BaseModel):
    threshold_low: Any = Field(default=...)
    threshold_high: Any = Field(default=...)
    reason: str = ""
    operator: str = ""


class RetestPayload(BaseModel):
    value: float
    observed_at: str | None = None
    shift: str | None = None
    message_id: str | int | None = None
    observation_id: str | int | None = None
    operator: str = ""


class ConclusionPayload(BaseModel):
    conclusion: str
    operator: str = ""
    close: bool = False


def _rule_or_404(rule_id: int) -> dict[str, Any]:
    rule = QUALITY_SERVICE.get_entry(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {rule_id} 不存在或已归档")
    return rule


def _interval_or_404(interval_id: int) -> dict[str, Any]:
    interval = ledger.get_interval(interval_id)
    if interval is None:
        raise HTTPException(status_code=404, detail=f"越界区间 {interval_id} 不存在")
    return interval


@router.get("/versions/{rule_id}")
def get_rule_versions(rule_id: int) -> dict[str, Any]:
    _rule_or_404(rule_id)
    return QUALITY_SERVICE.get_rule_versions(rule_id)


@router.post("/{rule_id}/threshold")
def revise_threshold(rule_id: int, payload: ThresholdPayload) -> dict[str, Any]:
    _rule_or_404(rule_id)
    result = QUALITY_SERVICE.revise_threshold(
        rule_id,
        threshold_low=payload.threshold_low,
        threshold_high=payload.threshold_high,
        reason=payload.reason,
        operator=payload.operator,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "阈值调整失败"))
    return result


@router.post("/{rule_id}/checks")
def run_check(rule_id: int, payload: CheckPayload) -> dict[str, Any]:
    _rule_or_404(rule_id)
    return QUALITY_SERVICE.execute_check(
        rule_id,
        payload.value,
        point_code=payload.point_code,
        observed_at=payload.observed_at,
        shift=payload.shift,
        message_id=payload.message_id,
        observation_id=payload.observation_id,
        operator=payload.operator,
        inspector=payload.inspector,
    )


@router.get("/observations")
def list_observations(
    rule_id: int | None = None,
    rule_code: str | None = None,
    interval_id: int | None = None,
    point_code: str | None = None,
    breached_only: bool = False,
    version: str | None = None,
) -> dict[str, Any]:
    items = ledger.list_observations(
        rule_id=rule_id,
        rule_code=rule_code,
        interval_id=interval_id,
        point_code=point_code,
        breached_only=breached_only,
        version=version,
    )
    return {"items": items, "total": len(items)}


@router.get("/intervals")
def list_intervals(
    rule_id: int | None = None,
    rule_code: str | None = None,
    status: str | None = None,
    pending: bool = False,
    issue_id: int | None = None,
) -> dict[str, Any]:
    items = ledger.list_intervals(
        rule_id=rule_id,
        rule_code=rule_code,
        status=status,
        pending_only=pending,
        issue_id=issue_id,
    )
    return {"items": items, "total": len(items)}


@router.get("/intervals/{interval_id}")
def get_interval(interval_id: int) -> dict[str, Any]:
    _interval_or_404(interval_id)
    detail = QUALITY_SERVICE.get_interval_detail(interval_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"越界区间 {interval_id} 不存在")
    return detail


@router.post("/intervals/{interval_id}/retests")
def record_retest(interval_id: int, payload: RetestPayload) -> dict[str, Any]:
    _interval_or_404(interval_id)
    result = QUALITY_SERVICE.record_retest(
        interval_id,
        payload.value,
        observed_at=payload.observed_at,
        shift=payload.shift,
        message_id=payload.message_id,
        observation_id=payload.observation_id,
        operator=payload.operator,
    )
    return result


@router.post("/intervals/{interval_id}/conclusion")
def write_conclusion(interval_id: int, payload: ConclusionPayload) -> dict[str, Any]:
    _interval_or_404(interval_id)
    result = QUALITY_SERVICE.write_conclusion(
        interval_id,
        payload.conclusion,
        operator=payload.operator,
        close=payload.close,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "结论回写失败"))
    return result


@router.get("/issues/{issue_id}")
def get_issue_quality(issue_id: int) -> dict[str, Any]:
    detail = QUALITY_SERVICE.get_issue_quality_detail(issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"治理问题 {issue_id} 不存在")
    return detail
