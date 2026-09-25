# ChatGPT connectors: measured request shapes (2026-09-25)

Owner of every chatgpt.com interaction for connectors (custom MCP apps and
links). The scripts `list_connectors.py`, `create_connector.py`,
`connect_connector.py` and `delete_connector.py` replay exactly these shapes;
the `chatgpt-mcp-onboarding` skill calls them and adds nothing of its own on
the ChatGPT side. Captured from the web app with a page hook while the owner
clicked through the form, then verified by the scripts.

Current command behavior is in `SKILL.md`; the latest acceptance results
are in `VERIFICATION.md`. The captures below are a dated history. In the
current CLI, `--detail` takes connector ids, not link ids. Tool-list refresh
is provided by the adjacent `chatgpt-refresh` command outside this skill.
Deletion stops if a link cannot be removed, and a requested privacy value
must be confirmed before connect reports success.

The current acceptance run created a disposable app on an existing tunnel,
connected it, refreshed its six tools with `chatgpt-refresh`, and called
`run_command` from a project chat. The captured SSE tool result and local
command log both confirmed the output. The stored conversation did not
retain that tool-result text; assistant text alone is not a tool-call proof.
Link-only deletion, reconnection, and app-plus-link deletion all passed.
Persistent project-level app configuration was not tested.

Platform tunnel creation and management now use `manage_tunnels.py` in this
skill. They need a separate Platform credential. See `tunnels.md` for the
request shapes, command examples and live-test status. The ChatGPT picker
listing below is not the Platform management listing.


Observed in Chrome with a logged-in session:

- Settings dialog: `https://chatgpt.com/#settings/Plugins` lists installed plugins/connectors; "Developer mode" lives at `#settings/Security?section=developer-mode` (switch, was ON).
- `/plugins` page: "Add plugin" opens a menu: Create plugin (opens the conversational Plugin Creator app), Upload plugin, Create app.
- "Create app" opens a "New Plugin" dialog (upload archive or **Create MCP App**) and rewrites the URL to `#settings/Connectors?create-connector=true&redirectAfter=%2Fplugins`.
- "Create MCP App" shows the custom connector form: icon (optional), `custom-connector-name`, `custom-connector-description`, Connection radios `Server URL` | `Tunnel`, `custom-connector-url` (placeholder `https://example.com/sse`), `custom-connector-auth` select (`OAUTH`, `NONE`, `MIXED`), Advanced OAuth settings, `trust-checkbox` "I understand and want to continue", buttons Back / Create.
- Choosing `Tunnel` calls `GET /backend-api/aip/connectors/mcp/tunnels` (bearer access token, 401 without it) and renders a `<select>` "Select a tunnel" with `name (tunnel_id)` options plus "Use tunnel ID instead".
- Listing: `POST /backend-api/aip/connectors/links/list_accessible`; refresh tools: `POST /backend-api/aip/connectors/mcp/refresh_actions` (implemented by the adjacent `chatgpt-refresh` command).
- Create request (captured on the re-creation, 10:22:53): `POST /backend-api/aip/connectors/mcp` with JSON `{"name":"rp-skilltest","tunnel_id":"tunnel_6a8a...","description":"","logo_url":null,"auth_request":{"supported_auth":[],"oauth_client_params":null}}` -> `{"connector":{"id":"asdk_app_6ab5bed1195c8191b35529e60f4d1547","connector_type":"MCP","tunnel_id":...,"supported_auth":[{"type":"NONE"}],"status":"ONLY_ME","distribution_channel":"INDIVIDUAL","policy_info":{"safety_status":"SCANNED_OK"},...}}`. The connect interstitial then posted `links/noauth` (10:23:03 -> `link_6ab5bee65efc8191a4e66e8eba901757`) and, on the privacy choice, `PATCH /backend-api/aip/connectors/links/<link_id>` `{"apps_privacy_control":"full_access"}`. Earlier observation, 10:08: the first submit created the connector; a second submit of the same form returned HTTP 409 `{"detail":{"message":"Connector with name 'rp-skilltest' already exists","existing_connector_id":"asdk_app_6ab5bb8f112c8191ad19d74f4cfb80f1"}}`. The request body was not captured by the first hook (non-string body, most likely multipart because of the icon field); the second hook records FormData entries and XHR bodies.
- What a created custom MCP connector is: an *app* `asdk_app_<32hex>` with a plugin release (`PluginRelease_<32hex>`, version 1.0.0, `discoverability: PRIVATE`, `status: ENABLED`, `installation_policy: AVAILABLE`, `app_manifest.apps.dev-<hex>`), listed by `GET /backend-api/ps/plugins/installed` (88 KB for this account). It does NOT appear in `POST /backend-api/aip/connectors/links/list_accessible` (which is what `chatgpt-refresh --list` reads), and `GET /backend-api/aip/connectors/mcp/<asdk_app id>` is 404. The pre-plugin connector `Raspberry Pi MCP` is a `link_<32hex>` and is refreshed through `refresh_actions`; refresh uses that app's connected `link_` id, verified by `chatgpt-refresh` in the current acceptance run.
- Detail lookup: `POST /backend-api/aip/connectors/batch` with `{"connector_ids":["asdk_app_..."],"include_actions":false}` returns `connectors[]` with `connector_type: "MCP"`, `name`, `description`, `model_description`, `service` (the tunnel gateway), `base_url: null`, `tunnel_id`, `created_at` (the rp-skilltest row says 2026-09-25T00:08:49Z = the owner's first click). `POST /backend-api/apps/content` `{"app_ids":[...]}` returned `{"apps":[]}` for it.
- Plugin page: `https://chatgpt.com/plugins/plugin_<asdk_app id>` with a "Plugin actions" overflow menu: Manage, Uninstall, Download plugin ZIP, Upload new version. "Manage" opens the settings dialog `#settings/Plugins/plugin_<asdk_app id>`: a "Connection: Connect" button (a freshly created app is NOT connected yet; connecting is what creates the user's link, expected `POST /backend-api/aip/connectors/links/noauth` for No Auth) and one "Copy input schema" button per tool (six for binnacle: the tool list was discovered through the tunnel at creation time).
- Connect (the owner clicked "Connect" at 10:19:11): `POST /backend-api/aip/connectors/links/noauth` with `{"connector_id":"asdk_app_...","name":"rp-skilltest","action_names":[]}` -> the link record: `id: link_6ab5bdffa58081919f2d8eac3211e43b`, `connector_id: asdk_app_...`, `actions: [read_file, list_files, search_text, run_command, job_status, stop_job]`, `auth_type: NONE`, `auth_status: ACTIVE`, `visibility: VISIBLE`, `disable_auto_invocation: false`. After this the connector appears in `links/list_accessible` (and `chatgpt-refresh --list`) as a normal `link_` with 6 tools; the page also re-lists with `{"principals":[],"link_refresh_strategy":"NONE"}`.
- Uninstall (the owner clicked "Plugin actions -> Uninstall" at 10:20:03): `DELETE /backend-api/aip/connectors/<asdk_app id>` -> `{}`. The app and its plugin release are gone (`ps/plugins/installed` no longer lists it; `connectors/batch` returns nothing for the app id, and it never returns links at all). CORRECTION 10:45: the link is NOT removed with the app: after the script's DELETE of the second app at 10:42 its link `link_6ab5bee6...` stayed in `list_accessible` as `auth_status ACTIVE`, `connector_status ONLY_ME` for five minutes, until `DELETE /backend-api/aip/connectors/links/<link id>` (-> 200 `{}`) removed it. The first throwaway's link `link_6ab5bdff...` was also still listed one minute after the UI uninstall (10:21 listing) and was gone later; whether it was garbage-collected or removed when the same name was re-created was not observed.
- The existing `Raspberry Pi MCP` connector has the same shape: app `asdk_app_6a8af16275ac8191a891cf48118748f5` (`ps/plugins/installed` name `dev-6a8af162...`, PRIVATE, created 2026-08-23) plus link `link_6a8af17294788191b9e35978879baaf3`.
- `connectors/batch` detail of a custom MCP app also carries `supported_auth: [{"type":"NONE"}]`, `status: ONLY_ME`, `labels: {"writes":"true"}`, `policy_info.safety_status: SCANNED_OK`, `developer_type: UNTRUSTED`.
- Delete request: captured above (Uninstall).
- Use of a connected app from a project chat: verified in the current acceptance run. Persistent project-level app configuration is not verified.
- Tunnels (platform.openai.com, logged-in dashboard session, no admin key): the page `https://platform.openai.com/settings/organization/tunnels` calls `GET https://api.openai.com/v1/tunnels?organization_id=<org>` and `GET https://api.openai.com/v1/tunnels/principals` with the dashboard session's bearer; its "Create tunnel" button opens the creation form (fields still to be recorded). `tunnel-client admin tunnels create` posts to the same API with an admin key, so the dashboard route is the same endpoint under the user's own session.

## Verification transcript (2026-09-25)

- 10:29:0x `send_prompt.py verify-prompt.txt --system-hint plugin:plugin_asdk_app_6ab5bed1195c8191b35529e60f4d1547 --title "rp-test skilltest verify"`; the recorded `f/conversation` body carried `system_hints: ["plugin:plugin_asdk_app_6ab5bed1195c8191b35529e60f4d1547"]`, model `gpt-6-astra-wm`.
- 10:29:27 journal: `event=tool_call call=653f8053ff0c tool=run_command client=openai-mcp(Codex) ... args={"workdir":"/tmp","command":"echo skilltest-96064"}`.
- Reply (chat 6ab5c055-e3cc-83ec-9cab-8f0baefb3091): `rp-skilltest` / `skilltest-96064`.
- `chatgpt-refresh rp-skilltest` -> "Tools now: read_file, list_files, search_text, run_command, job_status, stop_job".
- `delete_connector.py asdk_app_6ab5bed1195c8191b35529e60f4d1547 --confirm` -> deleted (app, release and link gone; `list_connectors.py --match rp-skilltest` empty).

## Acceptance run of the scripts (2026-09-25 10:50-10:52, no browser)

- `create_connector.py --name rp-skilltest --tunnel tunnel_6a8adbbdd4408191b4df1f0898c32550` -> `asdk_app_6ab5c55f33708191abd6e343eba3139a` (created 00:50:44Z, supported_auth NONE, status ONLY_ME).
- `connect_connector.py asdk_app_6ab5c55f... --name rp-skilltest --apps-privacy full_access` -> `link_6ab5c56f10408191ad5bdab284dd458b`, auth NONE, tools read_file, list_files, search_text, run_command, job_status, stop_job; `apps_privacy_control=full_access`.
- `list_connectors.py --match rp-skilltest` -> 1 link, 1 custom MCP app. `chatgpt-refresh rp-skilltest` -> "Refreshed rp-skilltest. Tools now: read_file, list_files, search_text, run_command, job_status, stop_job".
- `delete_connector.py asdk_app_6ab5c55f... --confirm` -> deleted link_6ab5c56f..., deleted asdk_app_6ab5c55f...; listing empty afterwards.

## Full chain test (2026-09-25 11:17-11:27)

- 11:2x owner created tunnel `rp-onboarding-test` (`tunnel_6ab5ccf0930c81919a2b498c49c6866b`, org only) on the platform page; the page hook captured nothing (the page re-rendered), the id was read from the table. `admin tunnels get` with the runtime key: organization_ids [org-15FjIoynRqX0UP2k98SLmXNE].
- `GET /aip/connectors/mcp/tunnels` (ChatGPT) still listed only the workspace-attached tunnel; `POST /aip/connectors/mcp` with the new tunnel id succeeded anyway (`asdk_app_6ab5cd43f220819189c064881ae79208`).
- `links/noauth` → `link_6ab5cd62bd188191b76d28a0059dd9c6`, six tools discovered through the new tunnel (test server journal: `tools/list`, `client=openai-mcp (ChatGPT)`).
- Nonce call 11:26:01 on the test server (`client=openai-mcp(Codex)`, `owner=embedded`); reply `rp-onboarding-test` / `onboard-99496`; chat `6ab5cd96-ffe0-83ec-8f77-580f9673c4e0` deleted afterwards.
- Teardown by `scripts/tunnel-teardown.sh ... --purge` and unit stop; tunnel deleted by the owner on the platform page.
- 12:12 tunnel `rp-onboarding-test` deleted through the platform page by the agent (claude-in-chrome): `find` the row's "Delete tunnel" button (verified by the row text via JS before clicking), click, the confirmation dialog "Delete tunnel? Deleting rp-onboarding-test removes this tunnel..." appeared in the accessibility tree, click "Delete"; the table then listed only the primary tunnel and `tunnel-client admin tunnels get` answered 404 for the deleted id.

- Creation needs a served tunnel (measured 2026-09-25 17:12): `create_connector.py` on a freshly created tunnel with no tunnel-client daemon answers HTTP 424 `{"detail":{"type":"mcp_error","developer_message":"MCP SSE probe returned 429 from openai.org", ...}}`; nothing is created. Start the MCP server and the daemon first, then create and connect.
