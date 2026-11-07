"""Loopback web application. Browser writes always belong to the human participant, stored as ``User``."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .runtime import access_log_config, board_info
from .store import Conflict, NotFound, Store, USER
from .dispatch import claude_session, courier_online, deliver_codex, messenger_listening, remote_claude_session
from .turns import (answered_at, continue_prompts, daily_usage, latest_limits, latest_per_project, link_discussions,
                    project_sessions, project_turns, read_turns)
from . import cruise

CRUISE_SECONDS = 3
log = logging.getLogger("correspondence.cruise")


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


class ModeChange(Payload):
    mode: str
    hops: int | None = None
    ceiling: int | None = None


def create_app(data_dir=None, dist_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(data_dir)
        watcher = asyncio.create_task(watch_cruises())
        yield
        watcher.cancel()

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

    def run_cruises():
        if store().cruising_projects():
            for line in cruise.step(store(), read_turns(store().directory)):
                log.warning("cruise %s", line)

    async def watch_cruises():
        while True:
            await asyncio.sleep(CRUISE_SECONDS)
            try:
                await asyncio.to_thread(run_cruises)
            except Exception:
                log.exception("cruise round failed")

    @app.get("/api/health")
    def health():
        return {"status": "ok", **board_info(USER), "user_name": store().user_name()}

    @app.get("/api/projects")
    def projects(archived: bool = False):
        return store().list_projects(USER, archived=archived)

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
        return store().list_threads(USER, project_id, unread_only, limit, archived=archived)

    @app.post("/api/threads", status_code=201)
    def create_thread(payload: ThreadCreate):
        return store().create_thread(USER, **payload.model_dump())

    @app.get("/api/threads/{thread_id}")
    def read_thread(thread_id: int, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), max_bytes: int = Query(200_000, ge=1_000, le=10_000_000), mode: str = "oldest", before: int | None = Query(None, ge=1), archived: bool = False):
        return store().read_thread(USER, thread_id, after, limit, max_bytes, mode=mode, before=before, archived=archived)

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: int, archived: bool = False):
        return store().get_message(USER, message_id, archived=archived)

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    def post_message(thread_id: int, payload: MessageCreate):
        return store().post_message(USER, thread_id, **payload.model_dump())

    @app.post("/api/threads/{thread_id}/read")
    def mark_read(thread_id: int, payload: ReadMarker):
        return store().mark_read(USER, thread_id, payload.message_id)

    @app.post("/api/read-all")
    def mark_all_read(payload: Payload):
        return store().mark_all_read(USER)

    @app.post("/api/threads/{thread_id}/status")
    def set_status(thread_id: int, payload: StatusChange):
        return store().set_thread_status(USER, thread_id, payload.status)

    @app.get("/api/search")
    def search(q: str, project_id: str | None = None, limit: int = Query(100, ge=1, le=500), thread_id: int | None = Query(None, ge=1), before: int | None = Query(None, ge=1), archived: bool = False):
        return store().search_messages(USER, q, project_id, limit, thread_id=thread_id, before=before, archived=archived)

    @app.get("/api/threads/{thread_id}/export", response_class=PlainTextResponse)
    def export(thread_id: int, archived: bool = False):
        return PlainTextResponse(store().export_thread(USER, thread_id, archived=archived), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="discussion-{thread_id}.md"'})

    @app.get("/api/turns")
    def turns(project_id: str | None = None, limit: int = Query(10, ge=1, le=200)):
        history, pins, user = read_turns(store().directory), store().project_agents(), store().user_name()
        listed = project_turns(history, project_id, limit, pins, user) if project_id else latest_per_project(history, pins, user)
        names = {p["id"]: p["name"] for p in store().list_projects(USER)}
        for turn in listed:
            turn["continue_prompts"] = continue_prompts(turn, names.get(turn["project"], turn["project"]), user)
        return link_discussions(listed, history, store().agent_posts)

    @app.get("/api/usage")
    def usage(project_id: str | None = None, days: int = Query(7, ge=1, le=60)):
        history = read_turns(store().directory)
        return {"limits": {"Claude": store().limits("Claude"), "Codex": latest_limits(history, "Codex")},
                "days": daily_usage(history, project_id, days)}

    @app.get("/api/projects/{project_id}/mode")
    def project_mode(project_id: str):
        return store().project_mode(project_id)

    @app.post("/api/projects/{project_id}/mode")
    def set_project_mode(project_id: str, payload: ModeChange):
        store().set_project_mode(project_id, payload.mode, payload.hops, payload.ceiling)
        if payload.mode == "cruise":
            run_cruises()
        return store().project_mode(project_id)

    @app.get("/api/projects/{project_id}/agents")
    def project_agents(project_id: str):
        directory = store().directory
        pins, machines = store().project_agents(project_id), store().pin_machines(project_id)
        claude_machine = machines.get("Claude")
        if "Claude" not in pins:
            live = None
        elif claude_machine is None:
            live = claude_session(pins["Claude"])
        else:
            live = remote_claude_session(directory, claude_machine, pins["Claude"])
        primed = store().primed_sessions(pins.values())
        return {"pins": pins, "machines": machines, "sessions": project_sessions(directory, project_id),
                "primed": {agent: session in primed for agent, session in pins.items()},
                "claude_live": live, "messenger_listening": messenger_listening(directory, claude_machine),
                "couriers": {m: courier_online(directory, m) for m in set(machines.values()) if m}}

    @app.post("/api/projects/{project_id}/agents")
    def set_project_agent(project_id: str, payload: AgentPin):
        machine = next((s["machine"] for s in project_sessions(store().directory, project_id)
                        if s["agent"] == payload.agent and s["session_id"] == payload.session_id), None)
        store().set_project_agent(project_id, payload.agent, payload.session_id, machine)
        return project_agents(project_id)

    @app.get("/api/dispatches")
    def dispatches(project_id: str, limit: int = Query(10, ge=1, le=100)):
        turns = read_turns(store().directory)
        return [{**d, "answered_at": answered_at(turns, d) if d["status"] == "delivered" else None}
                for d in store().list_dispatches(project_id, limit)]

    @app.post("/api/dispatches", status_code=201)
    def create_dispatch(payload: DispatchCreate):
        dispatch = store().create_dispatch(payload.project_id, payload.target, payload.body)
        if payload.target == "Claude" or dispatch["machine"] is not None:
            return {**dispatch, "answered_at": None}
        delivered, detail = deliver_codex(dispatch["session_id"], dispatch["body"])
        return {**store().finish_dispatch(dispatch["id"], delivered, detail), "answered_at": None}

    @app.post("/api/dispatches/{dispatch_id}/cancel")
    def cancel_dispatch(dispatch_id: int, payload: Payload):
        return {**store().cancel_dispatch(dispatch_id), "answered_at": None}

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
