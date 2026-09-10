# mcp-gtm

Google Tag Manager MCP Server — read, draft, and publish GTM configuration via the
Tag Manager API v2. Built to stop the class of bug where a site's tags (GA4, ads,
consent) silently drift or break because they're only editable by hand in the GTM
UI, with no way for an assistant to inspect or fix them.

## Tools

### Reading (safe, auto-approvable)

| Tool | Description |
|------|-------------|
| `list_accounts` | List GTM accounts the service account can see (call this first) |
| `list_containers` | List containers under an account |
| `list_workspaces` | List workspaces under a container |
| `list_tags` | List tags in a workspace |
| `list_triggers` | List triggers in a workspace |
| `list_variables` | List user-defined variables in a workspace |
| `list_versions` | List container versions (published and unpublished) |

Every tool above (except `list_accounts`) takes a `path` argument copied from a
prior call's output — e.g. an account's `path` feeds `list_containers`, a
container's `path` feeds `list_workspaces`. This matches GTM API v2's own
resource-path convention rather than inventing a separate ID scheme.

### Drafting (isolated workspace, no live effect until published)

| Tool | Description |
|------|-------------|
| `create_workspace` | Create a new, isolated workspace for this server's own changes |
| `create_tag` | Create a tag |
| `create_trigger` | Create a trigger |
| `create_variable` | Create a user-defined variable |
| `create_version` | Snapshot a workspace's current state into a publishable version |

**Always call `create_workspace` first and pass its `path` to every other draft
tool below it — never a container's shared "Default Workspace".** A GTM
container is routinely hand-edited live in the GTM UI by whoever manages the
site; writing into the Default Workspace risks clobbering or publishing someone
else's in-progress manual changes. Using a workspace this server created keeps
its edits isolated by construction. Rollback for anything created this way is
just deleting the workspace in the GTM UI — nothing it contains is live yet.

### Publishing (real production effect — never auto-approved)

| Tool | Description |
|------|-------------|
| `publish_version` | Make a container version live on every site using that container |

This is the one genuinely destructive-by-consequence action in this server —
it changes what actually runs on production sites immediately. There is no
"unpublish" tool: to revert, `list_versions` and `publish_version` an earlier
one, which is the same operation as publishing anything else.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **Tag Manager API**
3. Create a Service Account → download JSON key (or reuse one already used for
   another `techdeveloper-org` MCP server — Google service accounts aren't
   tied to one API)
4. In GTM, for **every account** this server should manage: Admin → User
   Management → add the service account's email, with:
   - **Read** permission for the list tools above to work at all
   - **Edit** permission for the draft-write tools (`create_workspace`,
     `create_tag`, `create_trigger`, `create_variable`, `create_version`)
   - **Publish** permission for `publish_version`

   A `PERMISSION_DENIED` from any tool means the service account's GTM role
   is below what that specific tool needs — this is Google's own GTM
   permission model, not something this server can work around.

### 3. Register the server with Claude Code

Add to `~/.claude/settings.json` (mirroring the `google-analytics-ga4` entry
in that same file):

```json
{
  "mcpServers": {
    "gtm": {
      "command": "python",
      "args": [
        "C:/path/to/mcp-gtm/server.py"
      ],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "C:/path/to/service_account.json"
      }
    }
  }
}
```

## Usage Example

A typical "add a new tag safely" flow:

```
list_accounts()                                    -> pick the right account's path
list_containers(account_path)                      -> pick the right container's path
create_workspace(container_path, "add-lead-tag")   -> get workspace_path
create_trigger(workspace_path, "Lead Form Submit", "formSubmission")  -> get trigger path/id
create_tag(workspace_path, "GA4 - generate_lead", "gaawe",
           parameter='[{"type":"template","key":"eventName","value":"generate_lead"}]',
           firing_trigger_id="<trigger id from above>")
create_version(workspace_path, "Add generate_lead conversion tag")
                                                     -> get version_path
publish_version(version_path)                       -> now live
```

## Part of techdeveloper-org MCP Suite

Sibling servers: [`mcp-server-ga4`](https://github.com/techdeveloper-org/mcp-server-ga4)
(GA4 reporting + Admin API tools), [`mcp-gsc`](https://github.com/techdeveloper-org/mcp-gsc)
(Search Console).
