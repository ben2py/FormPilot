# FormPilot Python Agent

FormPilot 是一个由 LLM 自主决策并调用浏览器工具的网页填表 Agent。Python 进程负责多轮 Agent 循环，模型根据实时网页结构决定下一项工具调用；Playwright 工具只执行受约束的扫描、填写、验证和点击操作。

## Agent 如何运行

每一轮中，模型可以自主选择以下工具：

- `inspect_page`：观察可见字段、选项、按钮和错误提示；
- `get_profile_catalog`：查看本地资料的语义路径，不读取任何资料原值；
- `fill_from_profile`：把本地资料填入指定字段并回读验证；
- `verify_field`：验证网页是否保留了已填值；
- `wait_and_rescan`：处理异步加载和级联下拉框；
- `pause_for_user`：让用户手动完成密码、验证码或歧义字段；
- `request_user_confirmation`：申请一次性操作授权；
- `click_control`：点击下一步或经批准的登录、保存、提交等按钮。

模型通过 Responses API 返回 `function_call`，Python 执行工具后追加对应的 `function_call_output`，再让模型根据新状态继续决策，直到模型返回最终文本或达到最大轮数。

## 安全边界

- `inspect_page` 只把 `has_value` 发送给模型，不发送页面现有输入值。
- 资料目录只包含路径、语义名称和数据类型；姓名等普通字段也不暴露原值。
- 所有资料真实值均由本地工具读取，不进入模型参数或工具结果。
- 高风险资料写入具体域名前需要一次性确认。
- 密码、验证码、短信码、文件和签名字段由策略层硬拦截。
- 登录、保存、发送验证码、注册和提交等点击需要一次性确认。
- 审批令牌与 URL、字段和资料路径绑定，并且使用一次后立即失效。
- 模型没有“执行任意 JavaScript”工具。

## 安装

需要 Python 3.11 以上：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

如果希望把浏览器文件留在项目目录：

```bash
PLAYWRIGHT_BROWSERS_PATH=.formpilot/browsers playwright install chromium
```

创建本地环境文件和资料文件：

```bash
cp .env.example .env
cp examples/profile.example.json profile.json
```

将 API Key 写入 `.env` 的 `OPENAI_API_KEY`。`.env` 和 `profile.json` 都已加入 `.gitignore`；不要在资料文件中保存 API Key、密码或验证码。

## 运行

启动 Agent：

```bash
formpilot \
  --url "https://目标报名网站/" \
  --profile profile.json
```

默认自动加载当前目录的 `.env`，也可以通过 `--env-file /path/to/config.env` 指定其他文件。已经存在的进程环境变量优先于 `.env`。

默认使用 `gpt-5.6-terra`，它适合平衡工具决策能力和成本。可以覆盖：

```bash
formpilot --url "https://example.com/form" --profile profile.json --model gpt-5.6-sol
```

浏览器默认可见，并使用 `.formpilot/browser-profile` 保存独立登录会话。如果你希望连接自己启动的 Chromium：

```bash
formpilot \
  --url "https://example.com/form" \
  --profile profile.json \
  --cdp-url "http://127.0.0.1:9222"
```

在登录页运行时，Agent 会调用 `pause_for_user`，你可以直接在浏览器里输入密码和验证码，完成后回到终端按 Enter。

## 测试

完整静态与单元测试：

```bash
python3 -m unittest discover -s tests_python -v
python3 -m compileall -q formpilot
```

真实 Playwright 浏览器冒烟测试：

```bash
.venv/bin/python scripts/smoke_python_browser.py
```

当前测试覆盖：Agent 自主多轮工具调用、工具结果回传、敏感值隔离、验证码硬拦截、高风险资料审批、审批单次使用、提交前确认，以及动态下拉框。

## 目录

- `formpilot/agent.py`：自主工具调用循环和停止条件；
- `formpilot/model.py`：OpenAI Responses API 适配器；
- `formpilot/browser.py`：Playwright 网页观察和受控操作；
- `formpilot/tools/form_tools.py`：提供给模型的工具及隐私过滤；
- `formpilot/policy.py`：秘密字段、外部副作用和一次性审批策略；
- `formpilot/profile.py`：本地资料库及脱敏目录；
- `formpilot/cli.py`：命令行入口；
- `tests_python/`：Python Agent 与安全测试；
- `scripts/`：真实浏览器冒烟测试；
- `demo/`：只在本地使用的动态报名测试页。

更完整的模块边界和数据流见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界见 [SECURITY.md](SECURITY.md)。

## 当前边界

第一版主要支持标准 DOM 表单。跨域 iframe、Canvas 表单、复杂富文本组件、材料文件解析和视觉定位尚未加入。网站反自动化和验证码必须尊重网站规则并由用户处理。
