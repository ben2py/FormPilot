from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .agent import FormPilotAgent
from .browser import PlaywrightFormBrowser
from .config import AgentConfig, load_env_file
from .logging_util import RunLogger
from .model import create_model
from .profile import ProfileStore
from .task import TaskBrief
from .tools.form_tools import FormPilotTools


DEFAULT_GOAL = (
    "读取任务文档与本地个人资料（含具体值），自主导航到报名信息填写页；"
    "inspect 后先填完本页必填项再点下一步；能匹配的用资料填写，能推理的用 fill_text；"
    "资料不足时用 request_missing_profile_fields 在终端向我补齐并写回 profile.json；"
    "仅在登录短信验证码、签名/上传或最终提交时让我接管。"
)


def compose_goal(goal: str, guidance: list[str] | None = None, *, task_path: str | None = None) -> str:
    parts = [goal.strip()]
    if task_path:
        parts.append(f"请先阅读任务说明（{task_path}），按里面的自然语言要求去做。")
    notes = [item.strip() for item in (guidance or []) if item and item.strip()]
    if notes:
        joined = "\n".join(f"- {item}" for item in notes)
        parts.append(f"用户补充指引：\n{joined}")
    return "\n\n".join(parts)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="FormPilot Python LLM form-filling agent")
    result.add_argument("--url", required=True, help="目标表单 URL")
    result.add_argument("--profile", default="profile.json", help="本地资料 JSON")
    result.add_argument("--task", default="task.md", help="任务文档 Markdown，默认 task.md（存在时自动加载）")
    result.add_argument("--env-file", default=".env", help="环境变量文件，默认 .env")
    result.add_argument("--goal", default=DEFAULT_GOAL, help="交给 Agent 的目标")
    result.add_argument(
        "--guidance",
        action="append",
        default=[],
        help="追加自然语言指引，可多次使用；例如 --guidance '报考博士，从网上报名进入'",
    )
    result.add_argument("--log-dir", default=".formpilot/logs", help="行动轨迹日志目录")
    result.add_argument("--model", help="模型名，默认读取 FORMPILOT_MODEL")
    result.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"])
    result.add_argument(
        "--api-mode",
        choices=["auto", "responses", "chat"],
        help="API 协议：auto 按模型/BASE_URL 推断；DeepSeek 使用 chat",
    )
    result.add_argument("--headless", action="store_true", help="无头浏览器；需要人工登录/验证码时不要开启")
    result.add_argument("--cdp-url", help="连接已开启远程调试的 Chromium，例如 http://127.0.0.1:9222")
    result.add_argument("--max-steps", type=int, help="最大模型决策轮数")
    result.add_argument("--no-trace", action="store_true", help="隐藏终端工具调用轨迹（仍写入日志文件）")
    return result


async def run(args: argparse.Namespace) -> int:
    try:
        load_env_file(args.env_file)
    except ValueError as exc:
        print(f"环境文件格式错误：{exc}", file=sys.stderr)
        return 2
    if not os.getenv("OPENAI_API_KEY"):
        print(f"缺少 OPENAI_API_KEY。请写入 {args.env_file} 或进程环境，不要写入 profile.json。", file=sys.stderr)
        return 2
    profile_path = Path(args.profile)
    if not profile_path.exists():
        print(f"资料文件不存在：{profile_path}。可复制 examples/profile.example.json。", file=sys.stderr)
        return 2

    task_path = Path(args.task)
    task: TaskBrief | None = None
    if task_path.exists():
        try:
            task = TaskBrief.load(task_path)
        except OSError as exc:
            print(f"无法读取任务文档：{exc}", file=sys.stderr)
            return 2
    elif args.task != "task.md":
        print(f"任务文档不存在：{task_path}。可复制 examples/task.example.md。", file=sys.stderr)
        return 2

    config = AgentConfig.from_env()
    if args.model:
        config.model = args.model
    if args.reasoning_effort:
        config.reasoning_effort = args.reasoning_effort
    if args.api_mode:
        config.api_mode = args.api_mode
    if args.max_steps:
        config.max_steps = args.max_steps
    if args.headless:
        config.headless = True
    if args.cdp_url:
        config.cdp_url = args.cdp_url
    if args.no_trace:
        config.trace = False

    goal = compose_goal(args.goal, args.guidance, task_path=str(task_path) if task else None)
    run_logger = RunLogger(args.log_dir, also_print=False)

    def console_and_log(message: str) -> None:
        print(message)
        run_logger.event("trace", message=message)

    browser = PlaywrightFormBrowser(
        headless=config.headless,
        profile_dir=config.browser_profile_dir,
        cdp_url=config.cdp_url,
    )
    try:
        print(f"启动浏览器并打开：{args.url}")
        print(f"行动日志：{run_logger.path}")
        if task:
            print(f"任务文档：{task.path}")
        if args.guidance:
            print("用户指引：")
            for item in args.guidance:
                print(f"- {item}")
        await browser.start(args.url)
        profile = ProfileStore.load(profile_path)
        form_tools = FormPilotTools(
            browser,
            profile,
            task=task,
            profile_path=profile_path,
            template_path=profile_path.with_name("profile.json.template"),
        )
        model = create_model(config.model, config.reasoning_effort, api_mode=config.api_mode)
        agent = FormPilotAgent(
            model,
            form_tools.registry(),
            max_steps=config.max_steps,
            trace=console_and_log if config.trace else None,
            run_logger=run_logger,
        )
        result = await agent.run(goal)
        print("\n=== FormPilot 结果 ===")
        print(result.text)
        print(f"决策轮数：{result.steps}；工具调用：{len(result.tool_calls)}")
        print(f"行动日志：{result.log_path or run_logger.path}")
        return 0
    finally:
        await browser.close()
        run_logger.close()


def main() -> None:
    args = parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\n用户中止。", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
