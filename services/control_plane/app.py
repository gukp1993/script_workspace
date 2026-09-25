"""控制面 FastAPI 应用装配（CTL-001）。

``create_app(config)`` 把以下组件装配为一个仅回环暴露的本地应用：

- 防火墙中间件（令牌 / Origin / Host，CTL-001/002）；
- 项目与领域对象 CRUD（CTL-003）；
- 会话生命周期（CTL-005/006）；
- 事件 REST 兜底与 WebSocket（CTL-007）；
- 视觉资产（CTL-004）；
- 设置（CTL-008）；
- 启动恢复（CTL-009 基础版，在 lifespan 中执行）。

路由注册顺序有讲究：``/projects/{pid}/assets`` 专用路由必须先于
``/projects/{pid}/{kind}`` 通配路由注册，否则 assets 会被当作对象种类。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from domain_model.errors import DomainValidationError

from control_plane import __version__
from control_plane.assets import AssetStore
from control_plane.config import ControlPlaneConfig
from control_plane.errors import ControlPlaneError
from control_plane.events import EventBroker, router as events_router
from control_plane.recovery import recover_interrupted_sessions
from control_plane.security import mount_firewall
from control_plane.sessions import SessionManager
from control_plane.settings import SettingsManager
from control_plane.storage import KIND_PARSERS, ProjectStore


class SessionCreateBody(BaseModel):
    """创建会话请求体（CTL-005）。"""

    project_id: str
    target_id: str
    mode: str


class GateConfirmBody(BaseModel):
    """人工闸门确认请求体（CTL-005）。"""

    operator: str


class ProjectCreateBody(BaseModel):
    """创建项目请求体（CTL-003）。"""

    project_id: str
    name: str
    description: str = ""
    notes: str = ""


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    """装配控制面应用；config 缺省时按当前目录 + 随机令牌构造。"""
    if config is None:
        config = ControlPlaneConfig(project_root=Path.cwd())
    assert config.token is not None  # config 构造时已保证

    broker = EventBroker(buffer_size=config.event_buffer_size)
    store = ProjectStore(config.project_root / "projects")
    sessions = SessionManager(config.project_root / "sessions", broker=broker, store=store)
    assets = AssetStore(config.project_root, store=store, broker=broker, max_bytes=config.asset_max_bytes)
    settings = SettingsManager(config.project_root, store=store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # CTL-009 基础版：启动时把上次遗留的非终态会话标记为 interrupted
        recovered = recover_interrupted_sessions(app.state.sessions)
        app.state.broker.publish("control_plane_started", {"recovered_sessions": recovered})
        yield

    app = FastAPI(
        title="VAW Control Plane",
        version=__version__,
        lifespan=lifespan,
        # 本地控制面：关闭文档端点，收敛攻击面（CTL-001）
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.broker = broker
    app.state.store = store
    app.state.sessions = sessions
    app.state.assets = assets
    app.state.settings = settings

    # CTL-001/002：令牌 + Origin/Host 防线（仅 http 作用域；WS 在端点内自校验）
    mount_firewall(app, config)

    # ------------------------------------------------------------------
    # 异常 -> 统一响应体
    # ------------------------------------------------------------------

    @app.exception_handler(ControlPlaneError)
    async def _control_plane_error_handler(request: Request, exc: ControlPlaneError) -> JSONResponse:
        detail: dict[str, Any] = {"error": exc.code, "message": exc.message}
        if exc.issues:
            detail["issues"] = [asdict(issue) for issue in exc.issues]
        if exc.extra:
            detail.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(DomainValidationError)
    async def _domain_error_handler(request: Request, exc: DomainValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "error": "validation_failed",
                    "message": "领域对象校验失败",
                    "issues": [asdict(issue) for issue in exc.issues],
                }
            },
        )

    # ------------------------------------------------------------------
    # 健康检查（CTL-001：需令牌，与全部 /api/v1 一致）
    # ------------------------------------------------------------------

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "projects": store.project_count(),
            "sessions": sessions.count(),
        }

    # ------------------------------------------------------------------
    # 设置（CTL-008）
    # ------------------------------------------------------------------

    @app.get("/api/v1/settings")
    async def get_settings() -> dict[str, Any]:
        return settings.get()

    @app.put("/api/v1/settings")
    async def put_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return settings.update(payload)

    # ------------------------------------------------------------------
    # 项目（CTL-003）
    # ------------------------------------------------------------------

    @app.post("/api/v1/projects", status_code=201)
    async def create_project(payload: ProjectCreateBody) -> dict[str, Any]:
        return store.create_project(payload.project_id, payload.name, payload.description, payload.notes)

    @app.get("/api/v1/projects")
    async def list_projects() -> dict[str, Any]:
        projects = store.list_projects()
        return {"projects": projects, "count": len(projects)}

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        return store.get_project(project_id)

    # ------------------------------------------------------------------
    # 视觉资产（CTL-004）——必须在 {kind} 通配路由之前注册
    # ------------------------------------------------------------------

    @app.get("/api/v1/projects/{project_id}/assets")
    async def list_assets(project_id: str) -> dict[str, Any]:
        return assets.list_assets(project_id)

    @app.post("/api/v1/projects/{project_id}/assets", status_code=201)
    async def upload_asset(
        project_id: str,
        request: Request,
        path: str = Query(..., description="项目内逻辑相对路径"),
        kind: str = Query("template", description="template 或 mask"),
        asset_id: str | None = Query(None, description="可选显式 ID，缺省由文件名派生"),
    ) -> dict[str, Any]:
        content = await request.body()
        return assets.upload(project_id, path, content, kind=kind, asset_id=asset_id)

    @app.get("/api/v1/projects/{project_id}/assets/{asset_id}")
    async def get_asset(project_id: str, asset_id: str) -> dict[str, Any]:
        return assets.get_asset(project_id, asset_id)

    @app.get("/api/v1/projects/{project_id}/assets/{asset_id}/content")
    async def get_asset_content(project_id: str, asset_id: str) -> Response:
        data, media_type = assets.read_content(project_id, asset_id)
        return Response(content=data, media_type=media_type)

    # ------------------------------------------------------------------
    # 轨迹/帧/回放报告（M3 E11：UI-014/015 数据源）——必须在 {kind} 通配路由
    # 之前注册，否则 traces / replay-reports 会被当作对象种类
    # ------------------------------------------------------------------
    from control_plane.traces import register_trace_routes  # 局部导入：保持装配顺序清晰

    register_trace_routes(app, config)

    # ------------------------------------------------------------------
    # 领域对象 CRUD（CTL-003）
    # ------------------------------------------------------------------

    @app.get("/api/v1/projects/{project_id}/{kind}")
    async def list_objects(project_id: str, kind: str) -> dict[str, Any]:
        objects = store.list_objects(project_id, kind)
        return {"project_id": project_id, "kind": kind, "objects": objects, "count": len(objects)}

    @app.post("/api/v1/projects/{project_id}/{kind}", status_code=201)
    async def create_object(project_id: str, kind: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return store.create_object(project_id, kind, payload)

    @app.get("/api/v1/projects/{project_id}/{kind}/{object_id}")
    async def get_object(project_id: str, kind: str, object_id: str) -> dict[str, Any]:
        return store.get_object(project_id, kind, object_id)

    @app.put("/api/v1/projects/{project_id}/{kind}/{object_id}")
    async def update_object(
        project_id: str,
        kind: str,
        object_id: str,
        payload: dict[str, Any] = Body(...),
        version: int | None = Query(None, description="乐观锁期望版本（也可放 body.meta.version）"),
    ) -> dict[str, Any]:
        return store.update_object(project_id, kind, object_id, payload, expected_version=version)

    # ------------------------------------------------------------------
    # 会话（CTL-005/006）
    # ------------------------------------------------------------------

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(payload: SessionCreateBody) -> dict[str, Any]:
        return sessions.create(payload.project_id, payload.target_id, payload.mode)

    @app.get("/api/v1/sessions")
    async def list_sessions() -> dict[str, Any]:
        records = sessions.list_sessions()
        return {"sessions": records, "count": len(records)}

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return sessions.get(session_id)

    @app.post("/api/v1/sessions/{session_id}/confirm")
    async def confirm_session(session_id: str, payload: GateConfirmBody) -> dict[str, Any]:
        return sessions.confirm(session_id, payload.operator)

    @app.post("/api/v1/sessions/{session_id}/start")
    async def start_session(session_id: str) -> dict[str, Any]:
        return sessions.start(session_id)

    @app.post("/api/v1/sessions/{session_id}/pause")
    async def pause_session(session_id: str) -> dict[str, Any]:
        return sessions.pause(session_id)

    @app.post("/api/v1/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        return sessions.resume(session_id)

    @app.post("/api/v1/sessions/{session_id}/stop")
    async def stop_session(session_id: str) -> dict[str, Any]:
        return sessions.stop(session_id)

    # ------------------------------------------------------------------
    # 事件（CTL-007：REST 兜底 + WebSocket）
    # ------------------------------------------------------------------
    app.include_router(events_router)

    # M1 预览端点（UI-004 基础版：主屏 JPEG 抓帧；M2 换目标窗口采集）
    from control_plane.preview import register_preview_routes  # 局部导入：保持装配顺序清晰

    register_preview_routes(app, config)

    return app


#: 供文档/测试引用的对象种类集合
KNOWN_KINDS: frozenset[str] = frozenset(KIND_PARSERS)
