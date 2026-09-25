# OpenAI Platform tunnel management

`scripts/manage_tunnels.py` owns the cloud tunnel operations in this skill.
The onboarding skill's `tunnel-admin.sh` delegates to it. Local MCP servers,
tunnel-client profiles and daemons remain the onboarding skill's concern.

Platform manages tunnel metadata at
`https://platform.openai.com/settings/organization/tunnels`. ChatGPT manages
the custom app and its connected link on a different page. Deleting one
resource does not mean the other has been deleted.

## Credentials

### Obtain the keys or session

Reuse an existing valid credential for the intended organization. If one
is missing, obtain only the credential needed for the task:

- **Management admin key (`OPENAI_ADMIN_KEY`).** Sign in to the
  [Platform Admin keys page](https://platform.openai.com/settings/organization/admin-keys),
  select the intended organization, create and name an admin key, and save
  the displayed secret locally. The creator needs Platform admin-key
  permission; tunnel management also needs **Tunnels Read + Manage**. If
  creation is unavailable, ask the organization owner for the required
  access or credential provisioning. An ordinary runtime/project API key
  is not an admin key. The official [Admin APIs guide](https://developers.openai.com/api/docs/guides/admin-apis)
  links to this admin-key creation page.
- **Forwarding runtime key (`CONTROL_PLANE_API_KEY`).** Open
  [Platform Runtime API keys](https://platform.openai.com/settings/organization/api-keys)
  in the intended organization and create a runtime API key. The runtime
  principal needs **Tunnels Read + Use**. Ask the organization owner or RBAC
  administrator for that role if it is missing. Store the secret in the
  daemon's environment or private profile env file. The installed
  `tunnel-client help quickstart` names this page and variable; the
  [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
  explains the organization-level permissions. A project-only permission
  change does not grant an organization tunnel role.
- **Optional dashboard token (`OPENAI_DASHBOARD_TOKEN`).** This is the bearer
  from a signed-in Platform dashboard session, not a key created on either
  API-key page. Use this route only when an existing trusted token provider
  already obtains that session credential. Set `OPENAI_DASHBOARD_TOKEN_CMD`
  to that provider's command; it must print only the current bearer to stdout.
  This skill contains no dashboard-token extraction or login command. Use
  the admin-key setup above when no provider is configured. If a session
  expires, renew it through the provider's normal sign-in flow.

ChatGPT's own access token is obtained automatically from the signed-in
local browser session. Follow [ChatGPT setup](setup.md#select-the-access-needed-for-the-task);
the user does not create or copy a ChatGPT token for these commands.

### Configure credential selection

For unattended creation, listing, updates and deletion, configure either:

- `OPENAI_ADMIN_KEY` in the environment or in
  `~/.config/tunnel-client/admin.env`, owned by this user with mode 600.
  Its env-file entry is `OPENAI_ADMIN_KEY=...`. Use an existing credential
  service or configure it locally; do not paste a key into a conversation.
- `OPENAI_DASHBOARD_TOKEN`, or `OPENAI_DASHBOARD_TOKEN_CMD` naming an existing
  trusted command that prints a valid Platform dashboard session bearer.
  This command's output stays in memory. It has a 30-second timeout; its
  stderr and failed stdout are never included in an error message.

Management requires **Tunnels Read + Manage**. A normal runtime key cannot
create or delete tunnels. `get` also accepts `CONTROL_PLANE_API_KEY`, from
the environment or `~/.config/tunnel-client/binnacle-tunnel.env`.
The ChatGPT session bearer is not used for the Platform API. `OPENAI_API_KEY`
is not automatically treated as an admin credential.

Automatic credential selection is dashboard, then admin, then runtime for
`get` only. `--auth dashboard|admin|runtime` selects one source explicitly.
Within the admin source, a nonempty exported `OPENAI_ADMIN_KEY` takes
precedence over `admin.env`; the runtime variable similarly precedes its
runtime env file. `admin.env` is the default fallback path, not a required
second copy. Source selection happens before the request: a rejected key or
dashboard token does not cause an HTTP retry with another credential.
`--admin-env PATH` and `--runtime-env PATH` select other private env files.
Env files are parsed as data; shell substitutions are not executed.
No key is placed in a command argument, profile, result JSON or log by this
command. Requests are pinned to `https://api.openai.com`; redirects are
refused so an Authorization header cannot be forwarded elsewhere.

The command does not extract dashboard tokens or automate login verification.
During the 2026-09-25 probe, the saved browser account reached a Cloudflare
verification page at the Platform callback. It was closed without trying
to bypass the check. A management key was then available in the private
local env file, and the live mutation test completed with that key.

The official permission and setup reference is
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
The installed `tunnel-client v0.0.12` help also describes the management-key
and runtime-key split.

### Use an existing key from Bash

If `~/.bashrc` already exports `OPENAI_ADMIN_KEY`, open a new interactive
Bash terminal or run `source ~/.bashrc` in that terminal. The Python command
does not source shell files. A noninteractive agent, cron job or service
may not inherit that variable; pass it through that process's environment
or use the private fallback file below.

Check presence without displaying the key:

```bash
python3 -c 'import os; print("OPENAI_ADMIN_KEY: available" if os.environ.get("OPENAI_ADMIN_KEY", "").strip() else "OPENAI_ADMIN_KEY: missing from this process")'
```

The user's existing `.bashrc` key was loaded into a child Bash environment
and verified with a read-only tunnel GET returning HTTP 200 on 2026-09-25.
The key value was not printed or added to this repository.

If no credential is configured, enter a newly created admin key in a private
interactive Bash terminal with command tracing disabled. This keeps the key
out of command history:

```bash
read -rsp 'OpenAI admin key: ' OPENAI_ADMIN_KEY
printf '\n'
export OPENAI_ADMIN_KEY
```

For the optional fallback file, create it without replacing existing content:

```bash
mkdir -p ~/.config/tunnel-client
touch ~/.config/tunnel-client/admin.env
chmod 600 ~/.config/tunnel-client/admin.env
```

Edit that file locally and add `OPENAI_ADMIN_KEY=YOUR_ACTUAL_ADMIN_KEY`.
Replace the placeholder with the secret; do not put it in a chat or commit.
Use one assignment per line. The parser reads literal values, not shell
commands or variable expansion. The runtime fallback file follows the same
ownership and mode requirements, with `CONTROL_PLANE_API_KEY` as its name.

### Verify before making a change

Get the actual organization id from the selected Platform organization's
settings. Set `TUNNEL_ORG` to that id, or pass it through `--organization`.
Use `--workspace` at creation when the target ChatGPT workspace should list
the tunnel; check its association in Platform tunnel settings.

```bash
S=~/.claude/skills/chatgpt-web-operations/scripts
export TUNNEL_ORG='org-REPLACE_WITH_YOUR_ORGANIZATION_ID'
python3 "$S/manage_tunnels.py" list --auth admin --organization "$TUNNEL_ORG"
```

Replace the organization placeholder before running. Successful JSON output
and exit 0 verify this management read; an empty list is valid. Runtime-only
access can instead verify `get` on a known tunnel with `--auth runtime`.
Neither metadata read proves that the MCP server or forwarding daemon works.
Use the onboarding skill to verify those with a real tool call.

## Commands and results

All successful results use JSON except `create --id-only`. Create acts by
default. Update and delete are read-only previews until their action flag
is supplied. Previews identify the method, resource and proposed body.
In the examples below, set `TUNNEL_ORG` to the real organization id,
`TASK_TUNNEL_ID` to the exact tunnel id returned by create/list, and
`CHATGPT_WORKSPACE_ID` to the intended workspace id. Run the command needed
for the task; the examples include mutations.

```bash
S=~/.claude/skills/chatgpt-web-operations/scripts
python3 "$S/manage_tunnels.py" list --organization "$TUNNEL_ORG"
python3 "$S/manage_tunnels.py" get "$TASK_TUNNEL_ID"
python3 "$S/manage_tunnels.py" create "Local MCP" --organization "$TUNNEL_ORG" --description "Local MCP server" --dry-run
python3 "$S/manage_tunnels.py" create "Local MCP" --organization "$TUNNEL_ORG" --description "Local MCP server" --workspace "$CHATGPT_WORKSPACE_ID" --id-only
python3 "$S/manage_tunnels.py" update "$TASK_TUNNEL_ID" --name "Renamed MCP" --expect-name "Local MCP" --apply
python3 "$S/manage_tunnels.py" delete "$TASK_TUNNEL_ID" --expect-name "Renamed MCP" --confirm
```

`--organization` defaults to `TUNNEL_ORG`; list and create require it.
Repeat `--workspace` at creation to attach more than one workspace. Update
changes only the supplied name/description and preserves scope attachments.
Create/update verify the resulting fields with a GET. Delete reads the target,
checks an optional `--expect-name`, deletes once, and requires a subsequent
404. Failed checks return nonzero. Create is never automatically retried;
if creation succeeds but read-back fails, the error includes the new id for
recovery. List returns the server's JSON, including any paging metadata;
it does not guess an undocumented paging parameter.

Allow 25–30 seconds for a new tunnel to become usable. Then configure and
start its forwarding daemon before creating/connecting the ChatGPT app.
When retiring it, remove connector links and the app, stop the daemon, and
delete the specific tunnel. Do not delete a shared tunnel as a side effect
of removing one connector. Two daemons on the same tunnel can receive each
other's work; use a dedicated tunnel for an independent MCP server.

## Recovery

| Error or symptom | Fix and next check |
| --- | --- |
| `no suitable Platform credential` | Check whether the intended variable is exported in this process. Load the user's configured environment or configure the owner-only fallback file. Then repeat the read-only list/get check. |
| Key works in a terminal but fails in an agent or service | That process may not load `.bashrc`. Supply its environment or use `--admin-env PATH` with a private env file. The presence check above tests the current process only. |
| `credential file must be owned by this user with mode 600` | Confirm the file belongs to the intended desktop/service user, then set `chmod 600` on that file. Run under that user and retry. |
| Dashboard-token command fails, times out or returns an expired token | Renew the session through the configured provider. If an admin key is available, select it explicitly with `--auth admin`. The command does not fall through after a provider error or HTTP rejection. |
| Platform HTTP 401 | Check the selected credential source and whether its key or session has expired or been revoked. Obtain a replacement through the same creation/sign-in route, update the active source, then repeat the read-only check. |
| Platform HTTP 403 or “Tunnels access required” | Check the organization, credential type and assigned role. The operator needs Read + Manage for management; the runtime principal needs Read + Use. Ask the organization owner/RBAC administrator for the missing role. Role changes can take up to 30 minutes to propagate; see the official tunnel guide. |
| `--organization or TUNNEL_ORG is required` | Set the real Platform organization id from its settings. A ChatGPT workspace id or project id is not an organization id. Then repeat list or the create preview. |
| Invalid tunnel id, HTTP 404 or expected-name mismatch | Read the tunnel list under the intended organization. Use the exact returned id and verify its name before updating or deleting. Do not remove the name guard to work around selecting the wrong resource. |
| Tunnel missing from ChatGPT's picker | Check the target workspace association and the operator's Tunnels Use permission in Platform settings. This CLI's update changes name/description only; use Platform settings to change associations. |
| Create timed out or reports `created ...; verification failed` | Check the returned id with get, or inspect list for the attempted name. Creation may already have succeeded. Recover that resource before considering another create. |
| Delete reports a failed request or read-back | Use get and list to establish whether the exact target still exists. Confirm the result before retrying; do not delete another resource as a substitute. |
| Metadata reads work but connector discovery or tool calls fail | Check the local server and forwarding daemon. Use `tunnel-client doctor --profile PROFILE --explain` with the runtime credential and follow the onboarding skill's readiness and tool-call checks. |

## Request shapes and verification

These shapes were captured from the installed official `tunnel-client`
against a local HTTP recorder with a synthetic credential. No real key
was sent to that recorder. This establishes the CLI wire contract; it is
separate from live cloud acceptance.

| Operation | Request |
| --- | --- |
| List | `GET /v1/tunnels?organization_id=<org-id>` |
| Get | `GET /v1/tunnels/<tunnel-id>` |
| Create | `POST /v1/tunnels` with `name`, `description`, `organization_ids`, and optional `workspace_ids` |
| Update | `POST /v1/tunnels/<tunnel-id>` with only changed fields; it is not PATCH |
| Delete | `DELETE /v1/tunnels/<tunnel-id>` |

Verification on 2026-09-25:

- Live `get` succeeded against the existing tunnel with its runtime key.
  No existing tunnel was changed.
- Offline lifecycle, read-back, preview, credential, redirect, and error
  tests cover the new command. See `VERIFICATION.md` for final totals.
- Live list/create/get/update/delete passed with the local management key.
  One disposable tunnel was created with the existing organization and
  workspace attachments. Runtime-key get worked on it. Update preserved
  both attachments, a wrong name guard refused deletion, and previews left
  the tunnel present. The final delete was followed by a 404 and a listing
  without the test id. The original tunnel's full record was unchanged.
- The onboarding compatibility wrapper passed its shell syntax check and
  a credential-free create preview. It delegates to the same tested command.
- Raw acceptance logs and metadata remain outside the repository under
  `~/.local/state/chatgpt-web-operations/tunnel-verification-20260925/`.
