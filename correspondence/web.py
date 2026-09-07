"""Loopback web application. Browser writes always belong to Barry."""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .store import Conflict, NotFound, Store


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Payload):
    id: str
    name: str
    description: str = ""
    repo_path: str | None = None


class ThreadCreate(Payload):
    project_id: str
    title: str
    body: str
    kind: str = "note"
    request_id: str | None = None


class MessageCreate(Payload):
    body: str
    kind: str = "note"
    parent_id: int | None = None
    request_id: str | None = None


class ReadMarker(Payload):
    message_id: int


class StatusChange(Payload):
    status: str


def create_app(data_dir=None, dist_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(data_dir)
        yield

    app = FastAPI(title="Correspondence", lifespan=lifespan)

    @app.middleware("http")
    async def local_browser_boundary(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host):
            return JSONResponse({"detail": "Untrusted host"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != f"{request.url.scheme}://{host}":
                return JSONResponse({"detail": "Cross-origin writes are forbidden"}, status_code=403)
            if request.headers.get("x-correspondence-client") != "browser":
                return JSONResponse({"detail": "Missing browser client header"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"detail": "Writes require application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
        return JSONResponse({"detail": str(exc)}, status_code=code)

    def store():
        return app.state.store

    @app.get("/api/health")
    def health():
        return {"status": "ok", "participant": "Barry", "service": "correspondence"}

    @app.get("/api/projects")
    def projects():
        return store().list_projects("Barry")

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate):
        return store().create_project(**payload.model_dump())

    @app.get("/api/threads")
    def threads(project_id: str | None = None, unread_only: bool = False, limit: int = Query(200, ge=1, le=1000)):
        return store().list_threads("Barry", project_id, unread_only, limit)

    @app.post("/api/threads", status_code=201)
    def create_thread(payload: ThreadCreate):
        return store().create_thread("Barry", **payload.model_dump())

    @app.get("/api/threads/{thread_id}")
    def read_thread(thread_id: int, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return store().read_thread("Barry", thread_id, after, limit)

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    def post_message(thread_id: int, payload: MessageCreate):
        return store().post_message("Barry", thread_id, **payload.model_dump())

    @app.post("/api/threads/{thread_id}/read")
    def mark_read(thread_id: int, payload: ReadMarker):
        return store().mark_read("Barry", thread_id, payload.message_id)

    @app.post("/api/threads/{thread_id}/status")
    def set_status(thread_id: int, payload: StatusChange):
        return store().set_thread_status("Barry", thread_id, payload.status)

    @app.get("/api/search")
    def search(q: str, project_id: str | None = None, limit: int = Query(100, ge=1, le=500)):
        return store().search_messages("Barry", q, project_id, limit)

    @app.get("/api/threads/{thread_id}/export", response_class=PlainTextResponse)
    def export(thread_id: int):
        return PlainTextResponse(store().export_thread("Barry", thread_id), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="discussion-{thread_id}.md"'})

    @app.get("/api/{path:path}")
    def unknown_api(path: str):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    dist = Path(dist_dir or Path(__file__).resolve().parent.parent / "web" / "dist")
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        @app.get("/", response_class=PlainTextResponse)
        def build_notice():
            return "Correspondence backend is running. Build the browser interface in web/ to view the board."
    return app


app = create_app()


def main():
    import uvicorn
    uvicorn.run("correspondence.web:app", host="127.0.0.1", port=int(os.environ.get("CORRESPONDENCE_PORT", "8765")))


if __name__ == "__main__":
    main()
