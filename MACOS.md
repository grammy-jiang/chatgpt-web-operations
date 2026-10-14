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
  CHATGPT_SESSION_STORE=0; no Linux Xvfb, GNOME keyring or dbus.
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
