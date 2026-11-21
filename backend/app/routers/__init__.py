"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

质量越界专用路由排在通用资源路由之前，防止 ``/api/quality_rule/{entry_id}``
抢先匹配到 ``versions``、``intervals`` 这类固定路径。
"""
from __future__ import annotations

from app.routers import quality, resource

ROUTERS = [quality, resource]
