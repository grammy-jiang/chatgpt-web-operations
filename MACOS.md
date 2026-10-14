# Local macOS ChatGPT Skill adapter

Source is copied at Git SHA b626c06c3b640bfcd7bc3394e25640ce7ae94082
from the clean Raspberry Pi installed checkout, including 27 commits not
yet present on its GitHub remote main. Changes on this Mac are isolated
on branch macos-chatgpt-skill-port-20261014, not pushed upstream.

- Mac Codex finds the Skill through ~/.codex/skills/chatgpt-web-operations.
- Mac Chrome binary: ~/Applications/Google Chrome.app.
- Dedicated authenticated Chrome profile:
  ~/Library/Application Support/ChatGPT-MCP/Chrome.
- CDP: 127.0.0.1:9233 only. The browser is already running and owned by
  the Mac GUI user; the Skill never launches or kills it.
- scripts/mac_skill.sh is the canonical Mac entry point. It pins
  RP_BROWSER_CDP to loopback, RP_BROWSER_TAB=new, and
  CHATGPT_SESSION_STORE=0 plus CHATGPT_MACOS_CDP_MODE=1. This explicit mode
  activates macOS CDP integration without altering Linux-simulation tests;
  no Linux Xvfb, GNOME keyring or dbus.
- scripts/macos_cdp_auth.py only retrieves request-scoped ChatGPT cookies
  from the Mac's own authenticated Chrome through Playwright CDP, keeps
  them in process memory, and never prints or persists cookie/token values.
- scripts/mac_preflight.py replaces Linux /proc, Xvfb and D-Bus checks.
- model_settings.py, read_chat.py and macOS preflight.py --browser
  have been exercised against the authenticated Mac browser.
- Verified 2026-10-14: Mac Skill sent an isolated test via send_prompt.py
  (Mac Chrome CDP, Chat Mode) and retrieved it independently with read_chat.py;
  exact task marker, pagination answer, real gpt-6-thinking/standard metadata
  and finished-turn status all passed. Evidence is stored separately at
  ~/Projects/android-mcp-mac-skill-e2e-20261014/acceptance.json.
- This does not by itself prove a persistent Supervisor/Task Manager exists,
  nor that a 30-minute multi-agent development task has been completed.
- Use a separate Task Manager/browser lock and unique worker chat IDs;
  stop only own test tabs. No feature grants shell/root/device writes.
- MacBook can host local Codex (logged in), while Supervisor on Pi can
  use the existing restricted SSH reverse loopback tunnel to the browser.

## 2026-10-14 实测：Mac Codex → Skill → ChatGPT → Mac MCP

- Mac Codex CLI 0.160.0，gpt-6-astra / high，在独立工作目录中读取并调用本 Skill。
- mac_skill.sh preflight.py --browser 通过 GO：Chrome 155.0.8059.80、ChatGPT 已认证、Chat Mode 输入框可用。
- mac_skill.sh send_prompt.py 实际创建 HOST-MAC-SKILL-E2E-20261014 对话：https://chatgpt.com/c/6acf7a02-af84-83ec-841d-b660a9871d5d 。实际模型 gpt-6-thinking、effort standard。
- ChatGPT 工作代理通过 MacBook Pro MCP 的 read_file 真实读取了两份独立测试文件；Mac Codex 通过 Skill 读取回复和工具调用记录，并独立审查。
- 初始回复格式含有 escape 包装；人工独立审查还发现本次测试夹具自相矛盾：offset==len(values) 在终止页测试中允许、另一用例却认定非法。纠正隔离夹具后，ChatGPT 原始修复建议 4/4 单测通过。因此派发/回收链路成功，初始回复质量评估仍须忠实标记 partial。
- 本地 macOS 端口的会话、浏览器、发送、模型和参数重写等 576 项离线回归测试通过。
- 证据目录：~/Projects/android-mcp-mac-skill-worker-smoke-20261014、~/Projects/android-mcp-chatgpt-worker-e2e-20261014 。没有修改正式 Android MCP 源码。
- 尚未在这次 Smoke 中验证：多代理自动合并、生产级工作树权限、Supervisor 自动恢复和真实 Android 硬件运行。不能把这些宣称为通过。
