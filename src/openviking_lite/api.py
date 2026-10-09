# SPDX-License-Identifier: AGPL-3.0
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import Settings
from .models import ModelError
from .service import Service
from .store import ConflictError, canonical_uri


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceBody(Body):
    to: str
    text: str = Field(min_length=1, max_length=5000000)
    summarize: bool = True


class FindBody(Body):
    query: str = Field(min_length=1, max_length=2000)
    target_uri: str = "viking://"
    limit: int = Field(default=10, ge=1, le=100)
    score_threshold: float = Field(default=-1, ge=-1, le=1)
    level: Literal[0, 1, 2] | None = None


class EditBody(Body):
    text: str = Field(min_length=1, max_length=5000000)
    summarize: bool = False
    expected_updated_at: str | None = Field(default=None, min_length=1, max_length=64)


class AskBody(Body):
    question: str = Field(min_length=1, max_length=2000)
    target_uri: str = "viking://"
    limit: int = Field(default=5, ge=1, le=20)


class SessionBody(Body):
    user_id: str = "default"
    external_id: str | None = Field(default=None, min_length=1, max_length=256)


class MessageBody(Body):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=100000)
    message_id: str | None = Field(default=None, min_length=1, max_length=256)


def ok(result):
    return {"status": "ok", "result": result}


def create_app(settings: Settings, *, service: Service | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        owned = service is None
        app.state.service = service or Service(settings)
        try:
            yield
        finally:
            if owned:
                app.state.service.close()

    def authorize(request: Request):
        if settings.server_api_key:
            supplied = request.headers.get("X-API-Key", "")
            if not supplied:
                authorization = request.headers.get("Authorization", "")
                supplied = authorization.removeprefix("Bearer ")
            if not secrets.compare_digest(supplied.encode(), settings.server_api_key.encode()):
                raise HTTPException(401, "Invalid API key")

    def get_service(request: Request) -> Service:
        return request.app.state.service

    service_dependency = Depends(get_service)

    app = FastAPI(
        title="OpenViking Lite — Text Memory",
        version=__version__,
        lifespan=lifespan,
    )
    api = APIRouter(dependencies=[Depends(authorize)])
    web = Path(__file__).parent / "web"
    app.mount("/ui", StaticFiles(directory=web, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def web_ui():
        return FileResponse(web / "index.html")

    @app.middleware("http")
    async def web_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(ValueError)
    async def invalid_argument(request, exc):
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse(
            {"status": "error", "error": "URI or session not found"}, status_code=404
        )

    @app.exception_handler(ConflictError)
    async def conflict(request, exc):
        return JSONResponse(
            {"status": "error", "error": str(exc), "code": exc.code}, status_code=409
        )

    @app.exception_handler(ModelError)
    async def model_failure(request, exc):
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)

    @api.get("/health")
    def health():
        return {"status": "ok", "mode": "text-only"}

    @api.get("/api/v1/system/status")
    def status(svc: Service = service_dependency):
        return ok(
            {
                "mode": "text-only",
                "version": __version__,
                "embedding_model": settings.embedding.model,
                "llm_model": settings.llm.model,
                **svc.store.stats(),
            }
        )

    @api.post("/api/v1/resources")
    def add(body: ResourceBody, svc: Service = service_dependency):
        return ok(svc.add(body.to, body.text, summarize=body.summarize))

    @api.post("/api/v1/search/find")
    @api.post("/api/v1/search/search")
    def find(body: FindBody, svc: Service = service_dependency):
        return ok(svc.find(**body.model_dump()))

    @api.post("/api/v1/ask")
    def ask(body: AskBody, svc: Service = service_dependency):
        return ok(svc.ask(**body.model_dump()))

    @api.get("/api/v1/content/read")
    def read(
        uri: str, level: Annotated[int, Query(ge=0, le=2)] = 2, svc: Service = service_dependency
    ):
        return ok(svc.read(uri, level))

    @api.get("/api/v1/content/document")
    def document(uri: str, svc: Service = service_dependency):
        return ok(svc.store.read(canonical_uri(uri)))

    @api.patch("/api/v1/content/document")
    def edit_document(uri: str, body: EditBody, svc: Service = service_dependency):
        return ok(
            svc.update(
                uri,
                body.text,
                summarize=body.summarize,
                expected_updated_at=body.expected_updated_at,
            )
        )

    @api.get("/api/v1/fs/ls")
    def ls(uri: str = "viking://", svc: Service = service_dependency):
        return ok(svc.store.ls(canonical_uri(uri, root=True)))

    @api.delete("/api/v1/fs")
    def rm(uri: str, recursive: bool = False, svc: Service = service_dependency):
        return ok({"deleted": svc.store.delete(canonical_uri(uri, root=True), recursive)})

    @api.get("/api/v1/sessions")
    def sessions(
        user_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        offset: Annotated[int, Query(ge=0)] = 0,
        svc: Service = service_dependency,
    ):
        return ok(svc.store.list_sessions(user_id, limit, offset))

    @api.post("/api/v1/sessions")
    def session_create(body: SessionBody, svc: Service = service_dependency):
        return ok(svc.create_session(body.user_id, body.external_id))

    @api.get("/api/v1/sessions/{session_id}")
    def session_get(session_id: str, svc: Service = service_dependency):
        return ok(svc.store.session(session_id))

    @api.post("/api/v1/sessions/{session_id}/messages")
    def message_add(session_id: str, body: MessageBody, svc: Service = service_dependency):
        return ok(svc.append_message(session_id, body.role, body.content, body.message_id))

    @api.post("/api/v1/sessions/{session_id}/commit")
    def session_commit(session_id: str, svc: Service = service_dependency):
        return ok(svc.commit_session(session_id))

    app.include_router(api)
    return app
