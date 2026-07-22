from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .agent import FormPilotAgent
from .browser import PlaywrightFormBrowser
from .config import AgentConfig, load_env_file
from .model import OpenAIResponsesModel
from .profile import ProfileStore
from .tools.form_tools import FormPilotTools


DEFAULT_GOAL = "检查当前报名网页，根据本地资料自主填写所有能够可靠判断的字段；遇到登录、验证码、歧义或提交动作时让我接管或确认。"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="FormPilot Python LLM form-filling agent")
    result.add_argument("--url", required=True, help="目标表单 URL")
    result.add_argument("--profile", default="profile.json", help="本地资料 JSON")
    result.add_argument("--env-file", default=".env", help="环境变量文件，默认 .env")
    result.add_argument("--goal", default=DEFAULT_GOAL, help="交给 Agent 的目标")
    result.add_argument("--model", help="Responses API 模型，默认读取 FORMPILOT_MODEL")
    result.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh", "max"])
    result.add_argument("--headless", action="store_true", help="无头浏览器；需要人工登录/验证码时不要开启")
    result.add_argument("--cdp-url", help="连接已开启远程调试的 Chromium，例如 http://127.0.0.1:9222")
    result.add_argument("--max-steps", type=int, help="最大模型决策轮数")
    result.add_argument("--no-trace", action="store_true", help="隐藏工具调用轨迹")
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

    config = AgentConfig.from_env()
    if args.model:
        config.model = args.model
    if args.reasoning_effort:
        config.reasoning_effort = args.reasoning_effort
    if args.max_steps:
        config.max_steps = args.max_steps
    if args.headless:
        config.headless = True
    if args.cdp_url:
        config.cdp_url = args.cdp_url
    if args.no_trace:
        config.trace = False

    browser = PlaywrightFormBrowser(
        headless=config.headless,
        profile_dir=config.browser_profile_dir,
        cdp_url=config.cdp_url,
    )
    try:
        print(f"启动浏览器并打开：{args.url}")
        await browser.start(args.url)
        profile = ProfileStore.load(profile_path)
        form_tools = FormPilotTools(browser, profile)
        model = OpenAIResponsesModel(config.model, config.reasoning_effort)
        agent = FormPilotAgent(
            model,
            form_tools.registry(),
            max_steps=config.max_steps,
            trace=print if config.trace else None,
        )
        result = await agent.run(args.goal)
        print("\n=== FormPilot 结果 ===")
        print(result.text)
        print(f"决策轮数：{result.steps}；工具调用：{len(result.tool_calls)}")
        return 0
    finally:
        await browser.close()


def main() -> None:
    args = parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\n用户中止。", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
