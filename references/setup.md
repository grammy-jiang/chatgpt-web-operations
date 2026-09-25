# Setup and recovery

Use this guide on first installation or when a prerequisite check fails.
Existing working credentials and browser sessions can be reused.

## Prepare the local environment

Run commands as the desktop user who owns the browser profile and keyring.
On this machine, the skill is installed at the path below. The Codex and
shared-agent paths are symlinks to it. Adjust the path if the skill moved.

```bash
CHATGPT_OPS_DIR="$HOME/.claude/skills/chatgpt-web-operations"
S="$CHATGPT_OPS_DIR/scripts"
```

If `.venv` is absent, run:

```bash
bash "$CHATGPT_OPS_DIR/bootstrap.sh"
```

Bootstrap creates the skill's virtual environment and installs its Python
requirements. Commands then select that environment automatically. Bootstrap
checks for the system `dbus` module; it does not install host packages.

On Raspberry Pi OS, missing system dependencies can be installed with:

```bash
sudo apt install python3-venv python3-dbus xvfb
```

Browser operations also require the Chrome installation used by Playwright's
`channel="chrome"`. This is an arm64 host; use a compatible arm64 build.
Installing Playwright's default Chromium alone does not satisfy the current
Chrome launch path. Reuse the working browser installation on this machine.

## Select the access needed for the task

| Operation | Access to configure | Verification |
| --- | --- | --- |
| Chats, projects, Deep research and ChatGPT connectors | Sign in to `https://chatgpt.com` in this user's Chrome **Default** profile. Unlock the same user's GNOME keyring. | `python3 "$S/preflight.py"` |
| Sending or browser diagnostics | The same ChatGPT session, plus Chrome, Xvfb and an available browser slot | `python3 "$S/preflight.py" --browser` |
| Platform tunnel management | Admin key or configured Platform dashboard token, plus the intended organization | Use the [tunnel credential setup and read-only check](tunnels.md#credentials) |
| Local MCP forwarding | Runtime key, local server and running tunnel-client profile | Follow `chatgpt-mcp-onboarding`; a successful Platform metadata read does not test forwarding |

ChatGPT login does not supply a Platform management credential. An admin key
does not sign in to ChatGPT. The forwarding runtime key is a third credential;
this skill also accepts it for inspecting an existing tunnel with `get`.

### Obtain credentials when they are missing

For **ChatGPT**, sign in through the normal browser. The skill reads that
local session and obtains its access token automatically. No manual token
copy or Platform API key is needed for chats and ChatGPT connector commands.

For **Platform admin keys and runtime keys**, follow the
[key creation links and permission instructions](tunnels.md#obtain-the-keys-or-session).
That reference also explains the optional dashboard-token provider, how to
load a key from `.bashrc`, and when to use an owner-only fallback env file.
For ChatGPT custom apps, developer-mode access is a separate prerequisite;
the workspace administrator grants access where the workspace requires it.
See the [official tunnel access guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

If the local MCP server requires its own bearer, create it through that
server's setup procedure and let the onboarding profile reference it.
That server credential is separate from both OpenAI keys.

### Interpret the first check

Preflight prints each failing check with a proposed fix. Exit **0** means
GO, **1** means a blocking check failed, and **2** means GO WITH WARNINGS.
Review warnings before a long run. Preflight checks the ChatGPT path, not
Platform credentials or an MCP forwarding connection. A tunnel-only task
uses the tunnel command's read-only check instead.

## Fix a failed check

Start with the row that matches the actual error. Run its verification
after the fix. Do not recreate a connector or tunnel to repair a login.

| Symptom | Action | Verify |
| --- | --- | --- |
| `no virtual environment at ...` or a missing Python dependency | Run `bash "$CHATGPT_OPS_DIR/bootstrap.sh"`. If it reports missing `dbus`, install `python3-dbus` first. If Python cannot create a venv, install `python3-venv`. | Repeat bootstrap, then the original command's `--help`. |
| Missing Chrome or Xvfb | Install or restore the required host browser/Xvfb. Bootstrap only repairs the Python dependencies. | Run `preflight.py --browser`. |
| `cookie DB not found` or `no ChatGPT session cookie` | Open this user's Chrome Default profile and sign in to ChatGPT. If the login is in Chromium, use `probe_cookies.py --browser chromium` to check that profile and select it on commands that expose a browser option. | Run `probe_cookies.py`, then `probe_account.py` with the matching browser option. |
| GNOME keyring is locked, its key is missing, or the session bus is unavailable | Run as the desktop user, sign in to the desktop and unlock the keyring used by that browser. For a scheduled job, ensure that user's session bus and keyring service are available. The client supplies default D-Bus paths; it cannot unlock the keyring. | Run `preflight.py` and `probe_cookies.py`. |
| ChatGPT authentication returns 401/403 or a Cloudflare challenge | Check `probe_cookies.py` and whether ChatGPT opens in the user's normal browser. Session creation already tries the browser cookie after a failed keyring login. If sign-in or verification is required, complete it in the normal browser before retrying. | Run `probe_account.py`; add `preflight.py --browser` if the task needs a send. |
| Only the stored session appears to fail | Use `CHATGPT_SESSION_STORE=0 python3 "$S/probe_account.py"` for one comparison with Chrome's existing session. This does not delete the stored session. | Compare the two authentication results; retain the setting only if the evidence requires it. |
| Platform key missing, permission error, or environment/file confusion | Follow the [tunnel credential and recovery instructions](tunnels.md#credentials). Check variable presence without printing its value. | Run `manage_tunnels.py list` with the intended organization and authentication mode. |
| Browser budget, low memory or host load blocks a send | Let the current send or research dispatch finish. Reduce competing local work. | Repeat `preflight.py --browser`. Do not start another browser to diagnose an occupied slot. |
| A send fails but reads work | Run `probe_send_gates.py`. Read the recorded conversation before sending again. A history rate-limit notice does not establish a send failure. | Use `read_chat.py` for the known chat id. |
| A recent conversation is absent from search | Search indexing can lag. Use the id recorded by the send. | Run `read_chat.py`; retry search later if needed. |
| Deep research has no widget yet, or export says the report is unfinished | Run `deep_research.py status --run RUN_JSON --wait`. After it confirms DONE, a widget report can use `export --force` to bypass the legacy title check. | Fetch the report or inspect the exported file. |
| Connector exists but tools are missing or calls fail | Check the local MCP server, the forwarding daemon, and the exact tunnel id. An app must also have a connected link. Use the onboarding skill to check the complete chain. | List connector details, then make a harmless tool call within the user's requested scope. |

If a fix needs the user, report the command that failed, the observed error,
the specific action required, and the check to run afterward. Request a
configured path or confirmation of local setup, never a key or cookie value.
Keep raw account payloads and diagnostic artifacts outside the repository.

For API changes and the evidence behind previous diagnoses, see
[endpoint discovery](endpoint-discovery.md) and [the failure atlas](failure-atlas.md).
