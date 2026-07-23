from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent import FormPilotAgent
from ..browser import PlaywrightFormBrowser
from ..cli import DEFAULT_GOAL, compose_goal
from ..config import AgentConfig, load_env_file
from ..logging_util import RunLogger
from ..model import create_model
from ..profile import ProfileStore
from ..task import TaskBrief
from ..tools.form_tools import FormPilotTools
from .bridge import InteractionBridge

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
PROFILE_PATH = ROOT / "profile.json"
TASK_PATH = ROOT / "task.md"
ENV_PATH = ROOT / ".env"

app = FastAPI(title="FormPilot Console", version="0.4.0")
bridge = InteractionBridge()
_run_lock = asyncio.Lock()
_run_task: asyncio.Task[Any] | None = None
_run_state: dict[str, Any] = {"status": "idle"}


class ProfileUpdate(BaseModel):
    profile: dict[str, Any]


class TaskUpdate(BaseModel):
    content: str


class RunRequest(BaseModel):
    url: str = Field(min_length=8)
    guidance: list[str] = Field(default_factory=list)
    headless: bool = False
    max_steps: int | None = None
    goal: str | None = None


class InteractResponse(BaseModel):
    answer: Any = None


def _ensure_workspace() -> None:
    if ENV_PATH.exists():
        load_env_file(ENV_PATH)
    if not PROFILE_PATH.exists():
        example = ROOT / "examples" / "profile.example.json"
        if example.exists():
            PROFILE_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    if not TASK_PATH.exists():
        example = ROOT / "examples" / "task.example.md"
        if example.exists():
            TASK_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


@app.on_event("startup")
async def _startup() -> None:
    _ensure_workspace()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "status": _run_state.get("status", "idle")}


@app.get("/api/profile")
async def get_profile() -> dict[str, Any]:
    _ensure_workspace()
    if not PROFILE_PATH.exists():
        raise HTTPException(404, "profile.json not found")
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return {"ok": True, "path": str(PROFILE_PATH), "profile": data}


@app.put("/api/profile")
async def put_profile(body: ProfileUpdate) -> dict[str, Any]:
    if not isinstance(body.profile, dict):
        raise HTTPException(400, "profile must be an object")
    PROFILE_PATH.write_text(json.dumps(body.profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(PROFILE_PATH)}


@app.get("/api/task")
async def get_task() -> dict[str, Any]:
    _ensure_workspace()
    content = TASK_PATH.read_text(encoding="utf-8") if TASK_PATH.exists() else ""
    return {"ok": True, "path": str(TASK_PATH), "content": content}


@app.put("/api/task")
async def put_task(body: TaskUpdate) -> dict[str, Any]:
    TASK_PATH.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": str(TASK_PATH)}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    _ensure_workspace()
    config = AgentConfig.from_env()
    return {
        "ok": True,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "max_steps": config.max_steps,
        "fast": os.getenv("FORMPILOT_FAST", ""),
        "auto_approve": os.getenv("FORMPILOT_AUTO_APPROVE", ""),
        "has_api_key": bool(os.getenv("OPENAI_API_KEY")),
        "profile_path": str(PROFILE_PATH),
        "task_path": str(TASK_PATH),
    }


@app.get("/api/run/status")
async def run_status() -> dict[str, Any]:
    return {"ok": True, **_run_state}


@app.post("/api/interact/{req_id}")
async def interact(req_id: str, body: InteractResponse) -> dict[str, Any]:
    ok = await bridge.respond(req_id, body.answer)
    if not ok:
        raise HTTPException(404, "interaction not found or already resolved")
    return {"ok": True}


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def gen():
        yield f"data: {json.dumps({'type': 'hello', 'status': _run_state.get('status')}, ensure_ascii=False)}\n\n"
        while True:
            try:
                item = await asyncio.wait_for(bridge.events.get(), timeout=15)
                yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'ping', 'status': _run_state.get('status')}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _execute_run(req: RunRequest) -> None:
    global _run_state
    run_logger = RunLogger(ROOT / ".formpilot" / "logs", also_print=False)
    browser = PlaywrightFormBrowser(headless=req.headless, profile_dir=AgentConfig.from_env().browser_profile_dir)
    try:
        _run_state = {"status": "running", "url": req.url, "log": str(run_logger.path)}
        await bridge.emit("run_started", url=req.url, log=str(run_logger.path))

        config = AgentConfig.from_env()
        if req.max_steps:
            config.max_steps = req.max_steps
        if req.headless:
            config.headless = True

        await browser.start(req.url)
        profile = ProfileStore.load(PROFILE_PATH)
        task = TaskBrief.load(TASK_PATH) if TASK_PATH.exists() else None
        tools = FormPilotTools(
            browser,
            profile,
            task=task,
            profile_path=PROFILE_PATH,
            template_path=PROFILE_PATH.with_name("profile.json.template"),
            confirm=bridge.confirm,
            pause=bridge.pause,
            missing=bridge.missing,
        )
        model = create_model(config.model, config.reasoning_effort, api_mode=config.api_mode)

        def sync_trace(message: str) -> None:
            run_logger.event("trace", message=message)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(bridge.emit("trace", message=message))
            except RuntimeError:
                pass

        agent = FormPilotAgent(
            model,
            tools.registry(),
            max_steps=config.max_steps,
            trace=sync_trace,
            run_logger=run_logger,
        )
        goal = compose_goal(req.goal or DEFAULT_GOAL, req.guidance, task_path=str(TASK_PATH) if task else None)
        result = await agent.run(goal)
        _run_state = {
            "status": "done",
            "url": req.url,
            "log": str(run_logger.path),
            "steps": result.steps,
            "text": result.text,
        }
        await bridge.emit("run_finished", text=result.text, steps=result.steps, log=str(run_logger.path))
    except asyncio.CancelledError:
        _run_state = {"status": "stopped", "url": req.url}
        await bridge.emit("run_stopped")
        raise
    except Exception as exc:  # noqa: BLE001
        _run_state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        await bridge.emit("run_error", error=_run_state["error"])
    finally:
        await browser.close()
        run_logger.close()


@app.post("/api/run")
async def start_run(req: RunRequest) -> dict[str, Any]:
    global _run_task
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(400, "缺少 OPENAI_API_KEY，请先配置 .env")
    if _run_lock.locked() or (_run_task and not _run_task.done()):
        raise HTTPException(409, "已有任务在运行")
    await _run_lock.acquire()

    async def runner() -> None:
        try:
            await _execute_run(req)
        finally:
            _run_lock.release()

    _run_task = asyncio.create_task(runner())
    return {"ok": True, "status": "starting"}


@app.post("/api/run/stop")
async def stop_run() -> dict[str, Any]:
    global _run_task
    if _run_task and not _run_task.done():
        _run_task.cancel()
        return {"ok": True, "status": "stopping"}
    return {"ok": True, "status": "idle"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    _ensure_workspace()
    host = os.getenv("FORMPILOT_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("FORMPILOT_WEB_PORT", "8787"))
    print(f"FormPilot Console → http://{host}:{port}")
    uvicorn.run("formpilot.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
