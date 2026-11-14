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
from pydantic import BaseModel, ConfigDict, Field

from .runtime import access_log_config, board_info
from .store import Conflict, NotFound, Store, USER
from .dispatch import claude_session, courier_online, deliver_here, messenger_listening, remote_claude_session
from .turns import (answered_at, continue_prompts, daily_usage, latest_limits, latest_main_turn, latest_per_pair,
                    link_discussions, project_sessions, project_turns, read_turns, turn_key)
from . import attention, autocontinue

AUTO_SECONDS = 3
log = logging.getLogger("correspondence.autocontinue")


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectState(Payload):
    expected_state: str


class ProjectRename(Payload):
    name: str
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


class PairCreate(Payload):
    name: str
    discussion_ids: list[int] = Field(default_factory=list)


class PairUpdate(Payload):
    name: str | None = None
    discussion_ids: list[int] | None = None


class DispatchCreate(Payload):
    pair_id: int
    expected_pins: dict[str, str]
    target: str
    body: str


class DecisionHandle(Payload):
    key: str


class AutoStart(Payload):
    hops: int
    ceiling: int


def create_app(data_dir=None, dist_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(data_dir)
        watcher = asyncio.create_task(watch_autos())
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

    def run_autos():
        if store().running_autos():
            for line in autocontinue.step(store(), read_turns(store().directory)):
                log.warning("auto-continue %s", line)

    async def watch_autos():
        while True:
            await asyncio.sleep(AUTO_SECONDS)
            try:
                await asyncio.to_thread(run_autos)
            except Exception:
                log.exception("auto-continue round failed")

    @app.get("/api/health")
    def health():
        return {"status": "ok", **board_info(USER), "user_name": store().user_name()}

    @app.get("/api/projects")
    def projects(archived: bool = False):
        projects = store().list_projects(USER, archived=archived)
        return projects if archived else attention.project_statuses(store(), projects, read_turns(store().directory))

    @app.post("/api/projects/{project_id}/seen")
    def seen_project(project_id: str, payload: Payload):
        return attention.acknowledge_section(store(), project_id, read_turns(store().directory))

    @app.get("/api/pairs/{pair_id}/attention")
    def pair_attention(pair_id: int):
        pair = store().pair(pair_id)
        return {"decisions": attention.pair_decisions(store(), pair, read_turns(store().directory))}

    @app.post("/api/pairs/{pair_id}/attention/handle")
    def handle_pair_attention(pair_id: int, payload: DecisionHandle):
        return attention.handle_decision(store(), pair_id, payload.key, read_turns(store().directory))

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate):
        return store().create_project(**payload.model_dump())

    @app.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str, payload: ProjectState):
        return store().archive_project(project_id, payload.expected_state)

    @app.post("/api/projects/{project_id}/rename")
    def rename_project(project_id: str, payload: ProjectRename):
        return store().rename_project(project_id, payload.name, payload.expected_state)

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
        snapshots = attention.activity_snapshots(store(), read_turns(store().directory))
        return store().mark_all_read(USER, snapshots)

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
    def turns(project_id: str | None = None, pair_id: int | None = Query(None, ge=1), limit: int = Query(10, ge=1, le=200)):
        history, pairs, pins, user = read_turns(store().directory), store().pairs(), store().pair_agents(), store().user_name()
        if pair_id is not None:
            pair = store().pair(pair_id)
            if project_id is not None and pair["project_id"] != project_id:
                raise NotFound("This pair is not in the selected section")
            project_id = pair["project_id"]
        listed = project_turns(history, project_id, limit, pairs, pins, user, pair_id) if project_id else latest_per_pair(history, pairs, pins, user)
        names = {p["id"]: p["name"] for p in store().list_projects(USER)}
        for turn in listed:
            turn["continue_prompts"] = continue_prompts(turn, names.get(turn["project"], turn["project"]), user)
        return link_discussions(listed, history, store().agent_posts, pairs)

    @app.get("/api/usage")
    def usage(project_id: str | None = None, days: int = Query(7, ge=1, le=60)):
        history = read_turns(store().directory)
        return {"limits": {"Claude": store().limits("Claude"), "Codex": latest_limits(history, "Codex")},
                "days": daily_usage(history, project_id, days)}

    @app.get("/api/projects/{project_id}/pairs")
    def pairs(project_id: str):
        return store().pairs(project_id)

    @app.post("/api/projects/{project_id}/pairs", status_code=201)
    def create_pair(project_id: str, payload: PairCreate):
        return store().create_pair(project_id, payload.name, payload.discussion_ids)

    @app.post("/api/pairs/{pair_id}")
    def update_pair(pair_id: int, payload: PairUpdate):
        return store().update_pair(pair_id, payload.name, payload.discussion_ids)

    @app.get("/api/pairs/{pair_id}/auto")
    def auto_continue(pair_id: int):
        return store().auto_continue(pair_id)

    @app.post("/api/pairs/{pair_id}/auto")
    def start_auto(pair_id: int, payload: AutoStart):
        store().start_auto(pair_id, payload.hops, payload.ceiling)
        run_autos()
        return store().auto_continue(pair_id)

    @app.post("/api/pairs/{pair_id}/auto/stop")
    def stop_auto(pair_id: int, payload: Payload):
        return store().stop_auto(pair_id)

    @app.get("/api/pairs/{pair_id}/agents")
    def pair_agents(pair_id: int):
        pair = store().pair(pair_id)
        directory = store().directory
        pins, machines = store().pair_agents(pair_id), store().pair_machines(pair_id)
        claude_machine = machines.get("Claude")
        live = None if "Claude" not in pins else (claude_session(pins["Claude"]) if claude_machine is None
                                                  else remote_claude_session(directory, claude_machine, pins["Claude"]))
        primed = store().primed_sessions(pins.values())
        return {"pins": pins, "machines": machines,
                "sessions": project_sessions(directory, pair["project_id"], store().pairs(), store().pair_agents()),
                "primed": {agent: session in primed for agent, session in pins.items()},
                "claude_live": live, "messenger_listening": messenger_listening(directory, claude_machine),
                "couriers": {m: courier_online(directory, m) for m in set(machines.values()) if m}}

    @app.post("/api/pairs/{pair_id}/agents")
    def set_pair_agent(pair_id: int, payload: AgentPin):
        pair = store().pair(pair_id)
        sessions = project_sessions(store().directory, pair["project_id"], store().pairs(), store().pair_agents())
        session = next((s for s in sessions if s["agent"] == payload.agent and s["session_id"] == payload.session_id), None)
        if payload.session_id is not None and session is None:
            raise NotFound("That session has no recorded turn in this section")
        store().set_pair_agent(pair_id, payload.agent, payload.session_id, session["machine"] if session else None)
        return pair_agents(pair_id)

    @app.get("/api/dispatches")
    def dispatches(pair_id: int = Query(..., ge=1), limit: int = Query(10, ge=1, le=100)):
        turns = read_turns(store().directory)
        return [{**d, "answered_at": answered_at(turns, d) if d["status"] == "delivered" else None}
                for d in store().list_dispatches(pair_id, limit)]

    @app.post("/api/dispatches", status_code=201)
    def create_dispatch(payload: DispatchCreate):
        pair = store().pair(payload.pair_id)
        latest = latest_main_turn(read_turns(store().directory), pair, store().pair_agents(), store().user_name())
        dispatch = store().create_dispatch(payload.pair_id, payload.target, payload.body, after_turn=turn_key(latest), expected_pins=payload.expected_pins)
        return {**deliver_here(store(), dispatch), "answered_at": None}

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
