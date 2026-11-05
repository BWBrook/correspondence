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

from .runtime import access_log_config, board_info
from .store import Conflict, NotFound, Store
from .dispatch import deliver_codex
from .turns import answered_at, latest_per_project, project_sessions, project_turns, read_turns


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectState(Payload):
    expected_state: str


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


class AgentPin(Payload):
    agent: str
    session_id: str | None


class DispatchCreate(Payload):
    project_id: str
    target: str
    body: str


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
        return {"status": "ok", **board_info("Barry")}

    @app.get("/api/projects")
    def projects(archived: bool = False):
        return store().list_projects("Barry", archived=archived)

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate):
        return store().create_project(**payload.model_dump())

    @app.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str, payload: ProjectState):
        return store().archive_project(project_id, payload.expected_state)

    @app.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str, payload: ProjectState):
        return store().restore_project(project_id, payload.expected_state)

    @app.get("/api/threads")
    def threads(project_id: str | None = None, unread_only: bool = False, limit: int = Query(200, ge=1, le=1000), archived: bool = False):
        return store().list_threads("Barry", project_id, unread_only, limit, archived=archived)

    @app.post("/api/threads", status_code=201)
    def create_thread(payload: ThreadCreate):
        return store().create_thread("Barry", **payload.model_dump())

    @app.get("/api/threads/{thread_id}")
    def read_thread(thread_id: int, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), max_bytes: int = Query(200_000, ge=1_000, le=10_000_000), mode: str = "oldest", before: int | None = Query(None, ge=1), archived: bool = False):
        return store().read_thread("Barry", thread_id, after, limit, max_bytes, mode=mode, before=before, archived=archived)

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: int, archived: bool = False):
        return store().get_message("Barry", message_id, archived=archived)

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    def post_message(thread_id: int, payload: MessageCreate):
        return store().post_message("Barry", thread_id, **payload.model_dump())

    @app.post("/api/threads/{thread_id}/read")
    def mark_read(thread_id: int, payload: ReadMarker):
        return store().mark_read("Barry", thread_id, payload.message_id)

    @app.post("/api/read-all")
    def mark_all_read(payload: Payload):
        return store().mark_all_read("Barry")

    @app.post("/api/threads/{thread_id}/status")
    def set_status(thread_id: int, payload: StatusChange):
        return store().set_thread_status("Barry", thread_id, payload.status)

    @app.get("/api/search")
    def search(q: str, project_id: str | None = None, limit: int = Query(100, ge=1, le=500), thread_id: int | None = Query(None, ge=1), before: int | None = Query(None, ge=1), archived: bool = False):
        return store().search_messages("Barry", q, project_id, limit, thread_id=thread_id, before=before, archived=archived)

    @app.get("/api/threads/{thread_id}/export", response_class=PlainTextResponse)
    def export(thread_id: int, archived: bool = False):
        return PlainTextResponse(store().export_thread("Barry", thread_id, archived=archived), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="discussion-{thread_id}.md"'})

    @app.get("/api/turns")
    def turns(project_id: str | None = None, limit: int = Query(10, ge=1, le=200)):
        directory, pins = store().directory, store().project_agents()
        return project_turns(directory, project_id, limit, pins) if project_id else latest_per_project(directory, pins)

    @app.get("/api/projects/{project_id}/agents")
    def project_agents(project_id: str):
        return {"pins": store().project_agents(project_id), "sessions": project_sessions(store().directory, project_id)}

    @app.post("/api/projects/{project_id}/agents")
    def set_project_agent(project_id: str, payload: AgentPin):
        store().set_project_agent(project_id, payload.agent, payload.session_id)
        return project_agents(project_id)

    @app.get("/api/dispatches")
    def dispatches(project_id: str, limit: int = Query(10, ge=1, le=100)):
        turns = read_turns(store().directory)
        return [{**d, "answered_at": answered_at(turns, d) if d["status"] == "delivered" else None}
                for d in store().list_dispatches(project_id, limit)]

    @app.post("/api/dispatches", status_code=201)
    def create_dispatch(payload: DispatchCreate):
        if payload.target == "Claude":
            raise Conflict("Sending to Claude needs the Board Messenger, which is not set up yet")
        dispatch = store().create_dispatch(payload.project_id, payload.target, payload.body)
        delivered, detail = deliver_codex(dispatch["session_id"], dispatch["body"])
        return {**store().finish_dispatch(dispatch["id"], delivered, detail), "answered_at": None}

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
    uvicorn.run("correspondence.web:app", host="127.0.0.1", port=int(os.environ.get("CORRESPONDENCE_PORT", "8765")), log_config=access_log_config())


if __name__ == "__main__":
    main()
