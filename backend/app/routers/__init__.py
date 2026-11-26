"""业务路由汇总：专用协同路由先注册（含固定路径），通用资源路由兜底。"""
from __future__ import annotations

from app.routers import dispatch, resource

ROUTERS = [dispatch, resource]
