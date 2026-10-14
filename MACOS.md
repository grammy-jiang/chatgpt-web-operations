# Local macOS ChatGPT Skill adapter

The macOS adapter uses the Raspberry Pi-installed Skill source history
starting at b626c06c3b640bfcd7bc3394e25640ce7ae94082 (27 commits newer
than the former GitHub main). It is maintained on
macos-chatgpt-skill-port-20261014 and reviewed in GitHub PR #1.
Do not presume PR #1 is merged until its actual merge commit is verified.

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


## 2026-10-15 macOS health and live-read completion gates

The local mac_skill.sh health.py command checks the real macOS runtime
and loopback-only Chrome CDP, not Linux /proc, Xvfb, wireless link or
GNOME Keyring. The version check accepts the actual Chrome version
metadata dictionary and denies missing, invalid or non-Chrome responses.
Browser-only session expiry is verified from CDP cookie metadata in
memory, without leaking cookie values. A transport exception is
reported by error type only, not a potentially sensitive exception message.

T0 2772 tests, Ruff and >90% coverage have passed on macOS for these
changes. Sixteen dedicated unit tests check CDP/authorization/expiry,
wrong versions, exceptions and fail-closed behavior. Real T1 dedicated
health and API-shape cases passed, but the full T1 suite later returned
HTTP 429 on account read and correctly declared DO NOT START. A rate
limit is an external readiness blocker: it must not be skipped, silenced
or reported as a green account-read gate. After cooldown, rerun the
complete make live-read on the final exact SHA.

No user conversation, cookie, device diagnostic or account setting is
intentionally modified by the T0/T1 checks.

## 2026-10-15 Mac → Raspberry Pi SSH reverse tunnel auto-recovery

Chrome 155 runs on MacBook Pro at 127.0.0.1:9233. Raspberry Pi
workers reach it through a restricted SSH reverse port, also bound
only on Pi 127.0.0.1:9233. Neither port may bind the LAN.

The existing dedicated Mac key is ~/.ssh/android-mcp-chrome-tunnel.
The Raspberry Pi authorized_keys entry restricts this key to remote
port forwarding at 127.0.0.1:9233. The installer checks key owner,
permissions, strict known_hosts pinning and conflicting LaunchAgents.
No secret, cookie, root/admin SSH key or Chrome profile is copied.

After logging in to the Mac GUI desktop, run in the Skill checkout:

    .venv/bin/python scripts/mac_chrome_tunnel.py install
    .venv/bin/python scripts/mac_chrome_tunnel.py status

The user-scoped launchd service is named
com.grammy.android-mcp.chrome-reverse-tunnel. It runs /usr/bin/ssh with
BatchMode, strict host checking, no ControlMaster, ServerAliveInterval 20,
ServerAliveCountMax 2, ExitOnForwardFailure and a sole reverse port
127.0.0.1:9233 → 127.0.0.1:9233. KeepAlive=true, RunAtLoad=true and
ThrottleInterval=20 enable recovery on SSH/network failure.
Its launchd plist and private error log are under ~/Library/LaunchAgents
and ~/Library/Logs/AndroidMCP respectively; no password prompts.

For one-time migration from the preexisting dedicated manual reverse SSH
master, first bootstrap launchd, then shut down ONLY that known master:

    ssh -S ~/.ssh/android-mcp-chrome-tunnel.ctl -O exit grammy-jiang@raspberrypi.local

Wait for launchd to reclaim the remote port. Verify the active service with:

    launchctl print gui/$(id -u)/com.grammy.android-mcp.chrome-reverse-tunnel

Then independently check http://127.0.0.1:9233/json/version from Pi.
A loaded launchd service alone does NOT prove that SSH CDP forwarding works.
To test watchdog restart, record the SSH pid and use ONLY the specific
service target:

    launchctl kill SIGTERM gui/$(id -u)/com.grammy.android-mcp.chrome-reverse-tunnel

Confirm a NEW SSH pid and Pi Chrome CDP connection, without manual help.
Do not kill unrelated users' ssh/Chrome processes.

The user-scoped LaunchAgent starts only after the Mac GUI user logs in.
The current headless, closed-lid GUI login qualifies. A cold reboot without
user auto-login remains a separate macOS policy: this service alone does
NOT guarantee running before a user session. Chrome must also be launched
and authenticated independently.

### T2 sandbox writes and limits

T2 has exactly four live-write cases in the dedicated rp-test-sandbox:
temporary project create/delete; instructions change/restore; memory scope
global/project-only round trip; and the separately verified rp-test-plugin
release/install/skill-toggle/download/uninstall lifecycle. A test-only
plugin release may remain in history. Every write is constrained by the
live tier guard to sandbox resources, with post-test identity and state
checks. Real production projects, chats and plugins must remain untouched.
