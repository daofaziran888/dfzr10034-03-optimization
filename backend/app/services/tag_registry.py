"""标签体系领域服务：业务标签分类体系、层级结构和标签传播规则。

Pipeline 9: Tag registration → asset association → tag propagation
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TagRegistryService(BaseModuleService):
    MODULE = "tag_registry"
    ENTITY = "业务标签"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["标签编号", "标签名称", "标签层级", "父标签", "关联资产数", "标签说明", "创建人", "最后更新"]
    REQUIRED_FIELDS = ["标签编号", "标签名称", "标签层级"]
    STATUS_ORDER = ["草稿", "已发布", "已合并", "已废弃"]
    ACTION_RULES = {"发布标签": "已发布", "合并标签": "已合并", "废弃标签": "已废弃"}
    NEGATIVE_ACTIONS = ["废弃标签"]

    def register_tag(self, name: str, level: str, parent: str, description: str, creator: str) -> dict[str, Any]:
        """注册业务标签（Pipeline 9 入口）。"""
        tag_id = store.next_id(self.MODULE)
        tag = {
            "id": tag_id,
            "标签编号": f"TAG-{tag_id:04d}",
            "标签名称": name,
            "标签层级": level,
            "父标签": parent,
            "关联资产数": "0",
            "标签说明": description,
            "创建人": creator,
            "最后更新": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已发布",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(tag)
        return tag

    def associate_with_asset(self, tag_id: int, asset_code: str) -> dict[str, Any]:
        """将标签关联到资产（Pipeline 9 核心方法）。"""
        tag = self.get_entry(tag_id)
        if not tag:
            return {"ok": False, "error": "标签不存在"}

        # 更新标签的关联资产数
        current_count = int(tag.get("关联资产数", "0"))
        tag["关联资产数"] = str(current_count + 1)
        tag["最后更新"] = _time.strftime("%Y-%m-%d %H:%M:%S")

        # 创建关联记录
        association = {
            "tag_id": tag_id,
            "tag_name": tag["标签名称"],
            "asset_code": asset_code,
            "associated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Pipeline 9: 标签传播 — 通过血缘关系传播到下游资产
        propagated = self._propagate_tags(asset_code, tag)

        self.emit_event("tag_associated", {
            "tag_id": tag_id,
            "tag_name": tag["标签名称"],
            "asset_code": asset_code,
            "propagated_count": len(propagated),
        })

        return {
            "ok": True,
            "association": association,
            "tag": tag,
            "propagated_to": propagated,
            "pipeline": "tag_registry->asset_registry->lineage_track",
        }

    def _propagate_tags(self, asset_code: str, tag: dict[str, Any]) -> list[str]:
        """通过血缘关系将标签传播到下游资产。"""
        propagated = []
        # 查找下游资产
        for row in store.rows("lineage_track"):
            if row.get("上游资产") == asset_code:
                ds = row.get("下游资产", "")
                if ds and ds not in propagated:
                    propagated.append(ds)

                    # 自动为下游资产创建标签关联
                    self._auto_associate_tag(tag, ds)

        # 递归传播（限制层级防止无限循环）
        visited = {asset_code}
        visited.update(propagated)
        additional = []
        for ds in list(propagated):
            for row in store.rows("lineage_track"):
                if row.get("上游资产") == ds:
                    ds2 = row.get("下游资产", "")
                    if ds2 and ds2 not in visited:
                        additional.append(ds2)
                        visited.add(ds2)
                        self._auto_associate_tag(tag, ds2)

        propagated.extend(additional)
        return propagated

    def _auto_associate_tag(self, tag: dict[str, Any], asset_code: str) -> None:
        """自动为资产关联标签（传播）。"""
        # 查找资产是否存在
        asset = None
        for row in store.rows("asset_registry"):
            if row.get("资产编号") == asset_code:
                asset = row
                break

        if asset:
            # 记录在治理评分中
            score_id = store.next_id("govern_score")
            score = {
                "id": score_id,
                "任务编号": f"GOVE-TAG-{score_id:04d}",
                "评估对象": asset_code,
                "评分维度": "标签覆盖",
                "当前分数": "90",
                "改进基线": f"已通过血缘传播获得标签 {tag.get('标签名称','')}",
                "评估周期": "自动",
                "预警级别": "正常",
                "整改状态": "已出分",
                "status": "已出分",
                "pending": False,
                "abnormal": False,
                "propagated_tag_id": tag["id"],
                "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            store.rows("govern_score").append(score)

    def get_tags_by_level(self, level: str) -> list[dict[str, Any]]:
        """按层级获取标签。"""
        return store.find_by_field(self.MODULE, "标签层级", level)

    def get_tags_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产关联的所有标签。"""
        results = []
        for tag in store.rows(self.MODULE):
            # 通过关联资产数字段检查
            if asset_code in (tag.get("关联资产列表", "")):
                results.append(tag)
        return results

    def merge_tags(self, source_id: int, target_id: int) -> dict[str, Any]:
        """合并两个标签。"""
        source = self.get_entry(source_id)
        target = self.get_entry(target_id)
        if not source or not target:
            return {"ok": False, "error": "标签不存在"}

        # 合并关联资产数
        src_count = int(source.get("关联资产数", "0"))
        tgt_count = int(target.get("关联资产数", "0"))
        target["关联资产数"] = str(src_count + tgt_count)
        target["标签说明"] = f"{target.get('标签说明', '')} (合并自: {source.get('标签名称', '')})"

        # 废弃源标签
        source["status"] = "已废弃"
        source["pending"] = False

        store.log_change(
            self.MODULE, source_id, "合并标签",
            before="已发布", after="已废弃",
            source="tag_merge", scope="tag_registry"
        )

        self.emit_event("tags_merged", {
            "source_id": source_id,
            "target_id": target_id,
            "source_name": source.get("标签名称"),
            "target_name": target.get("标签名称"),
        })

        return {"ok": True, "source": source_id, "target": target_id}