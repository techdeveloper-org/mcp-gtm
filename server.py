"""Google Tag Manager MCP Server.

Provides GTM configuration tools via the Tag Manager API v2 (discovery-based
client -- GTM has no generated Python client library the way GA4 does).
Authentication: service account JSON (GOOGLE_APPLICATION_CREDENTIALS env var).
The service account must additionally be added as a User on each GTM account
this server manages (GTM Admin -> User Management), with Edit permission for
the write tools and Publish permission for publish_version -- holding the
OAuth scopes below is necessary but not sufficient, same as GA4's Admin API.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import threading
import time
from typing import Any, Callable, List, Optional, Union

# google.oauth2/googleapiclient are deliberately NOT imported at module
# level (see _ensure_google_imports()) -- the `google` namespace package's
# first touch in a fresh process measured 10-30+ seconds on this machine
# (Windows Defender scanning the namespace across every installed google-*
# package), which exceeds the MCP client's connection timeout and made this
# server fail to complete its stdio handshake at all. `from __future__
# import annotations` above makes every type hint in this file a lazy
# string, so none of the names below need to exist at import time.

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer

from mcp.types import ToolAnnotations

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

_LOGGER = logging.getLogger("gtm_server")

mcp = MCPServer("gtm-server")

_READ_REMOTE = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# Draft-write tools only ever touch a workspace this server itself created
# (see create_workspace) -- never the container's Default Workspace, which
# may hold a human's in-progress manual edits in the GTM UI. Isolated by
# construction, so these are non-destructive even though they mutate state.
_WRITE_DRAFT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# publish_version pushes a container version live to every site using that
# GTM container immediately, with no in-tool undo (reverting means manually
# publishing an earlier version in the GTM UI). Never auto-approved.
_PUBLISH = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

try:
    from rate_limiter import check_rate_limit
    _RATE_LIMITER_AVAILABLE = True
except ImportError:
    _RATE_LIMITER_AVAILABLE = False

    def check_rate_limit(client_id="default", bucket="tool_calls"):
        """Fallback used when ``rate_limiter`` cannot be imported.

        Always reports the call as allowed, so a missing vendored module
        fails open rather than crashing every tool call.

        Args:
            client_id: Identifier for the caller. Unused in the fallback.
            bucket: Name of the rate limit bucket. Unused in the fallback.

        Returns:
            dict with ``allowed`` always ``True``.
        """
        return {"allowed": True}

_RATE_LIMIT_UNAVAILABLE_WARNED = threading.Event()


def _rate_limit_verdict(bucket: str) -> dict:
    """Consume one token from ``bucket`` and report whether a call may run.

    Mirrors mcp-server-ga4's identical helper -- see that repo for the full
    rationale (opt-in via ENABLE_RATE_LIMITING, warn-once on misconfiguration).

    Args:
        bucket: Name of the token bucket to draw from.

    Returns:
        The limiter verdict dict, always containing ``allowed``.
    """
    if not _RATE_LIMITER_AVAILABLE:
        if (os.environ.get("ENABLE_RATE_LIMITING") == "1"
                and not _RATE_LIMIT_UNAVAILABLE_WARNED.is_set()):
            _RATE_LIMIT_UNAVAILABLE_WARNED.set()
            _LOGGER.warning(
                "rate_limiting_enabled_but_limiter_unavailable",
                extra={"detail": "ENABLE_RATE_LIMITING=1 has no effect; "
                                  "rate_limiter is not importable"},
            )
        return {"allowed": True}
    return check_rate_limit(bucket=bucket)


def rate_limited(bucket: str):
    """Decorator that gates a sync MCP tool behind the token-bucket limiter.

    Args:
        bucket: Name of the token bucket this tool draws from.

    Returns:
        The decorated tool function.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> str:
            """Rate-limit gate wrapping the original sync tool function."""
            verdict = _rate_limit_verdict(bucket)
            if not verdict.get("allowed", True):
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Rate limit exceeded for bucket '{bucket}'. "
                        f"Retry in {verdict.get('retry_after')} seconds."
                    ),
                    "error_type": "RateLimitExceeded",
                    "bucket": bucket,
                    "retry_after": verdict.get("retry_after"),
                })
            return fn(*args, **kwargs)
        return wrapper
    return decorator


CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "service_account.json"),
)

# GTM API v2 scopes. edit.containers covers create/update of workspaces,
# tags, triggers, variables, and versions; publish is a separate scope
# because publishing is a materially higher-risk action than drafting.
_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
]

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
# GTM's discovery client raises HttpError uniformly; only retry the status
# codes that mean "try again later", never 4xx client errors (bad request,
# permission denied, not found), which retrying cannot fix.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_client = None
_client_lock = threading.Lock()

_google_imports_loaded = False
_import_lock = threading.Lock()


def _ensure_google_imports() -> None:
    """Import google.oauth2/googleapiclient on first use, not at module load.

    See the module docstring comment above the (deliberately absent)
    top-level imports for why: this measured 10-30+ seconds on this
    machine, which blew past the MCP client's connection timeout and made
    the server look unregistered even though the code was correct.
    Deferring the import here means the handshake completes immediately;
    only the first actual tool call pays this cost.
    """
    global _google_imports_loaded, service_account, build, HttpError
    if _google_imports_loaded:
        return
    with _import_lock:
        if _google_imports_loaded:
            return
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        _google_imports_loaded = True


def _get_client():
    """Return a cached, authenticated GTM API v2 client.

    The client is built once and reused, avoiding a re-fetch of the API
    discovery document (a network round trip) on every tool call.

    Returns:
        Authenticated googleapiclient Resource for the Tag Manager API v2.

    Raises:
        FileNotFoundError: If the service account JSON file cannot be found at
            the configured path.
    """
    global _client
    if _client is not None:
        return _client

    _ensure_google_imports()
    with _client_lock:
        if _client is not None:
            return _client
        if not os.path.exists(CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Credentials file not found: {CREDENTIALS_PATH}. "
                "Set the GOOGLE_APPLICATION_CREDENTIALS env var to the absolute "
                "path of your GTM service account JSON key."
            )
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_PATH, scopes=_SCOPES,
        )
        _client = build("tagmanager", "v2", credentials=creds)
        return _client


def _call_with_retry(operation: Callable[[], Any], operation_name: str) -> Any:
    """Run a GTM API call, retrying transient failures with backoff.

    Args:
        operation: Zero-argument callable performing the API request
            (typically `.execute()` on a discovery-client request object).
        operation_name: Short label used in retry log records.

    Returns:
        Whatever operation() returns.

    Raises:
        googleapiclient.errors.HttpError: The final failure once the retry
            budget is exhausted, or immediately for non-transient errors.
    """
    _ensure_google_imports()
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return operation()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in _RETRYABLE_STATUS_CODES or attempt >= _MAX_RETRIES:
                raise
            ceiling = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)
            delay = random.uniform(0.0, ceiling)
            _LOGGER.warning(
                "gtm_api_retry",
                extra={
                    "operation": operation_name,
                    "status": status,
                    "attempt": attempt + 1,
                    "max_attempts": _MAX_RETRIES + 1,
                    "delay_seconds": round(delay, 3),
                },
            )
            time.sleep(delay)

    raise RuntimeError(
        f"GTM API call '{operation_name}' exhausted {_MAX_RETRIES + 1} attempts."
    )


def _require(value: Optional[str], field_name: str) -> str:
    """Validate a required string argument is present and non-blank.

    Args:
        value: Caller-supplied argument value.
        field_name: Name of the parameter, used in the error message.

    Returns:
        The stripped value.

    Raises:
        ValueError: If value is None, empty, or whitespace-only.
    """
    if not value or not value.strip():
        raise ValueError(f"{field_name} is required.")
    return value.strip()


def _parse_parameters(raw: Optional[Union[str, list]], field_name: str) -> List[dict]:
    """Parse a JSON array of GTM Parameter objects.

    GTM's Tag/Trigger/Variable ``parameter`` field is a list of objects like
    ``{"type": "template", "key": "url", "value": "https://example.com"}``
    (see the Tag Manager API v2 reference for the full Parameter schema,
    including nested "list"/"map" types). This server does not validate the
    schema beyond "valid JSON array" -- GTM's own API rejects a malformed
    Parameter list with a clear 400, which is surfaced as-is rather than
    duplicated here.

    Args:
        raw: JSON array string, an already-parsed list (some MCP clients
            pre-parse JSON-array-shaped string arguments into native lists
            before the call reaches this server, despite the declared
            string type -- accepting both avoids a confusing type error for
            callers who did nothing wrong), or None/empty for no parameters.
        field_name: Name of the parameter, used in error messages.

    Returns:
        Parsed list of parameter dicts (empty list if raw is None/empty).

    Raises:
        ValueError: If raw is a non-empty string that isn't valid JSON, or
            resolves to something other than a list.
    """
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, list):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array of Parameter objects.")
    return parsed


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_accounts() -> str:
    """List GTM accounts the configured service account can see.

    Call this first -- every other tool takes a ``path`` argument copied
    from a prior list/create call's output (e.g. an account's path feeds
    list_containers, a container's path feeds list_workspaces), matching
    GTM API v2's own resource-path convention rather than inventing a
    separate ID scheme.

    Returns:
        JSON string with ``accounts``: list of {accountId, name, path}.
    """
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().list().execute(), "accounts.list"
    )
    accounts = response.get("account", [])
    return json.dumps({
        "accounts": [
            {"accountId": a["accountId"], "name": a["name"], "path": a["path"]}
            for a in accounts
        ],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_containers(account_path: str) -> str:
    """List GTM containers under an account.

    Args:
        account_path: An account's ``path`` from list_accounts, e.g.
            'accounts/123456789'.

    Returns:
        JSON string with ``containers``: list of
        {containerId, name, publicId, path}.

    Raises:
        ValueError: If account_path is missing.
    """
    account_path = _require(account_path, "account_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().list(parent=account_path).execute(),
        "containers.list",
    )
    containers = response.get("container", [])
    return json.dumps({
        "containers": [
            {
                "containerId": c["containerId"],
                "name": c["name"],
                "publicId": c.get("publicId"),
                "path": c["path"],
            }
            for c in containers
        ],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_workspaces(container_path: str) -> str:
    """List GTM workspaces under a container.

    A container the user manages manually in the GTM UI typically has a
    'Default Workspace' plus any workspaces created by other collaborators
    -- this server's own write tools never touch those, only workspaces
    created via create_workspace below.

    Args:
        container_path: A container's ``path`` from list_containers, e.g.
            'accounts/123456789/containers/456'.

    Returns:
        JSON string with ``workspaces``: list of {workspaceId, name, path}.

    Raises:
        ValueError: If container_path is missing.
    """
    container_path = _require(container_path, "container_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().workspaces()
        .list(parent=container_path).execute(),
        "workspaces.list",
    )
    workspaces = response.get("workspace", [])
    return json.dumps({
        "workspaces": [
            {"workspaceId": w["workspaceId"], "name": w["name"], "path": w["path"]}
            for w in workspaces
        ],
    }, indent=2)


@mcp.tool(annotations=_WRITE_DRAFT)
@rate_limited("tool_calls")
def create_workspace(container_path: str, name: str, description: str = "") -> str:
    """Create a new, isolated GTM workspace for this server's own changes.

    Every other write tool (create_tag, create_trigger, create_variable,
    create_version) takes a workspace_path -- always pass one created here,
    never the container's 'Default Workspace', so this server's edits never
    collide with a human's in-progress manual changes in the GTM UI.

    Args:
        container_path: A container's ``path`` from list_containers.
        name: Workspace name, shown in the GTM UI.
        description: Optional workspace description.

    Returns:
        JSON string with workspaceId, name, and path of the created workspace.

    Raises:
        ValueError: If container_path or name are missing.
    """
    container_path = _require(container_path, "container_path")
    name = _require(name, "name")
    client = _get_client()
    body = {"name": name}
    if description:
        body["description"] = description
    result = _call_with_retry(
        lambda: client.accounts().containers().workspaces()
        .create(parent=container_path, body=body).execute(),
        "workspaces.create",
    )
    return json.dumps({
        "workspaceId": result["workspaceId"],
        "name": result["name"],
        "path": result["path"],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_tags(workspace_path: str) -> str:
    """List tags in a GTM workspace.

    Args:
        workspace_path: A workspace's ``path`` from list_workspaces or
            create_workspace.

    Returns:
        JSON string with ``tags``: list of {tagId, name, type, path}.

    Raises:
        ValueError: If workspace_path is missing.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().workspaces().tags()
        .list(parent=workspace_path).execute(),
        "tags.list",
    )
    tags = response.get("tag", [])
    return json.dumps({
        "tags": [
            {"tagId": t["tagId"], "name": t["name"], "type": t["type"], "path": t["path"]}
            for t in tags
        ],
    }, indent=2)


@mcp.tool(annotations=_WRITE_DRAFT)
@rate_limited("tool_calls")
def create_tag(
    workspace_path: str,
    name: str,
    tag_type: str,
    parameter: Optional[Union[str, list]] = None,
    firing_trigger_id: Optional[str] = None,
) -> str:
    """Create a tag in a GTM workspace (draft -- has no effect until published).

    Args:
        workspace_path: A workspace's ``path``, always one this server
            created via create_workspace, never a container's shared
            Default Workspace.
        name: Tag name, shown in the GTM UI.
        tag_type: GTM built-in tag type id (e.g. 'gaawe' for a GA4 event
            tag, 'html' for Custom HTML) or a custom template's short name.
        parameter: JSON array of GTM Parameter objects configuring the tag
            (e.g. '[{"type":"template","key":"eventName","value":"generate_lead"}]'),
            or None for a tag with no parameters yet.
        firing_trigger_id: Comma-separated trigger IDs (from list_triggers)
            that should fire this tag, or None to leave unset (the tag will
            not fire until a trigger is attached, in the GTM UI or via a
            follow-up call to this same tool with update semantics -- this
            MVP only creates, it does not update an existing tag).

    Returns:
        JSON string with tagId, name, type, and path of the created tag.

    Raises:
        ValueError: If workspace_path, name, tag_type, or parameter (when
            provided but not valid JSON) are invalid.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    name = _require(name, "name")
    tag_type = _require(tag_type, "tag_type")
    params = _parse_parameters(parameter, "parameter")

    body = {"name": name, "type": tag_type}
    if params:
        body["parameter"] = params
    if firing_trigger_id:
        body["firingTriggerId"] = [
            t.strip() for t in firing_trigger_id.split(",") if t.strip()
        ]

    client = _get_client()
    result = _call_with_retry(
        lambda: client.accounts().containers().workspaces().tags()
        .create(parent=workspace_path, body=body).execute(),
        "tags.create",
    )
    return json.dumps({
        "tagId": result["tagId"],
        "name": result["name"],
        "type": result["type"],
        "path": result["path"],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_triggers(workspace_path: str) -> str:
    """List triggers in a GTM workspace.

    Args:
        workspace_path: A workspace's ``path`` from list_workspaces or
            create_workspace.

    Returns:
        JSON string with ``triggers``: list of {triggerId, name, type, path}.

    Raises:
        ValueError: If workspace_path is missing.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().workspaces().triggers()
        .list(parent=workspace_path).execute(),
        "triggers.list",
    )
    triggers = response.get("trigger", [])
    return json.dumps({
        "triggers": [
            {"triggerId": t["triggerId"], "name": t["name"], "type": t["type"], "path": t["path"]}
            for t in triggers
        ],
    }, indent=2)


@mcp.tool(annotations=_WRITE_DRAFT)
@rate_limited("tool_calls")
def create_trigger(
    workspace_path: str,
    name: str,
    trigger_type: str,
    filter_: Optional[Union[str, list]] = None,
    custom_event_filter: Optional[Union[str, list]] = None,
) -> str:
    """Create a trigger in a GTM workspace (draft -- has no effect until published).

    Args:
        workspace_path: A workspace's ``path``, always one this server
            created via create_workspace.
        name: Trigger name, shown in the GTM UI.
        trigger_type: GTM trigger type id (e.g. 'pageview', 'click',
            'customEvent', 'formSubmission').
        filter_: JSON array of GTM Condition objects restricting when the
            trigger fires (e.g. matching a specific page path), or None to
            fire on every instance of trigger_type. For 'customEvent'
            triggers, GTM's API requires the event-name match to live in
            ``custom_event_filter`` instead (a separate field, not this
            one) -- ``filter_`` on a customEvent trigger is only for
            *additional* AND-ed conditions beyond the event name.
        custom_event_filter: JSON array with exactly one GTM Condition
            object matching the event name, required by GTM's API for
            'customEvent'-type triggers specifically (e.g.
            '[{"type":"equals","parameter":[{"type":"template","key":"arg0","value":"{{_event}}"},{"type":"template","key":"arg1","value":"generate_lead"}]}]').
            Ignored for other trigger types.

    Returns:
        JSON string with triggerId, name, type, and path of the created
        trigger.

    Raises:
        ValueError: If workspace_path, name, trigger_type, filter_, or
            custom_event_filter (when provided but not valid JSON) are
            invalid.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    name = _require(name, "name")
    trigger_type = _require(trigger_type, "trigger_type")
    conditions = _parse_parameters(filter_, "filter_")
    custom_event_conditions = _parse_parameters(custom_event_filter, "custom_event_filter")

    body = {"name": name, "type": trigger_type}
    if conditions:
        body["filter"] = conditions
    if custom_event_conditions:
        body["customEventFilter"] = custom_event_conditions

    client = _get_client()
    result = _call_with_retry(
        lambda: client.accounts().containers().workspaces().triggers()
        .create(parent=workspace_path, body=body).execute(),
        "triggers.create",
    )
    return json.dumps({
        "triggerId": result["triggerId"],
        "name": result["name"],
        "type": result["type"],
        "path": result["path"],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_variables(workspace_path: str) -> str:
    """List user-defined variables in a GTM workspace.

    Args:
        workspace_path: A workspace's ``path`` from list_workspaces or
            create_workspace.

    Returns:
        JSON string with ``variables``: list of {variableId, name, type, path}.

    Raises:
        ValueError: If workspace_path is missing.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().workspaces().variables()
        .list(parent=workspace_path).execute(),
        "variables.list",
    )
    variables = response.get("variable", [])
    return json.dumps({
        "variables": [
            {"variableId": v["variableId"], "name": v["name"], "type": v["type"], "path": v["path"]}
            for v in variables
        ],
    }, indent=2)


@mcp.tool(annotations=_WRITE_DRAFT)
@rate_limited("tool_calls")
def create_variable(
    workspace_path: str,
    name: str,
    variable_type: str,
    parameter: Optional[Union[str, list]] = None,
) -> str:
    """Create a user-defined variable in a GTM workspace (draft -- has no
    effect until published).

    Args:
        workspace_path: A workspace's ``path``, always one this server
            created via create_workspace.
        name: Variable name, shown in the GTM UI (referenced elsewhere as
            {{name}}).
        variable_type: GTM variable type id (e.g. 'c' for Constant, 'jsm'
            for Custom JavaScript, 'v' for a Data Layer Variable).
        parameter: JSON array of GTM Parameter objects configuring the
            variable, or None for a variable with no parameters yet.

    Returns:
        JSON string with variableId, name, type, and path of the created
        variable.

    Raises:
        ValueError: If workspace_path, name, variable_type, or parameter
            (when provided but not valid JSON) are invalid.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    name = _require(name, "name")
    variable_type = _require(variable_type, "variable_type")
    params = _parse_parameters(parameter, "parameter")

    body = {"name": name, "type": variable_type}
    if params:
        body["parameter"] = params

    client = _get_client()
    result = _call_with_retry(
        lambda: client.accounts().containers().workspaces().variables()
        .create(parent=workspace_path, body=body).execute(),
        "variables.create",
    )
    return json.dumps({
        "variableId": result["variableId"],
        "name": result["name"],
        "type": result["type"],
        "path": result["path"],
    }, indent=2)


@mcp.tool(annotations=_WRITE_DRAFT)
@rate_limited("tool_calls")
def create_version(workspace_path: str, name: str, notes: str = "") -> str:
    """Snapshot a workspace's current draft changes into a container version.

    A version is still not live -- it is a fixed, publishable snapshot of
    whatever tags/triggers/variables the workspace holds at this moment.
    Use publish_version to actually make it live.

    Args:
        workspace_path: A workspace's ``path``, always one this server
            created via create_workspace.
        name: Version name, shown in the GTM UI's version history.
        notes: Optional version notes describing what changed and why.

    Returns:
        JSON string with containerVersionId, name, and path of the created
        version.

    Raises:
        ValueError: If workspace_path or name are missing.
    """
    workspace_path = _require(workspace_path, "workspace_path")
    name = _require(name, "name")
    body = {"name": name}
    if notes:
        body["notes"] = notes

    client = _get_client()
    result = _call_with_retry(
        lambda: client.accounts().containers().workspaces()
        .create_version(path=workspace_path, body=body).execute(),
        "workspaces.create_version",
    )
    version = result["containerVersion"]
    return json.dumps({
        "containerVersionId": version["containerVersionId"],
        "name": version.get("name"),
        "path": version["path"],
    }, indent=2)


@mcp.tool(annotations=_READ_REMOTE)
@rate_limited("tool_calls")
def list_versions(container_path: str) -> str:
    """List container versions (published and unpublished) for a container.

    Use this to find a version's path before calling publish_version, or to
    audit publish history.

    Args:
        container_path: A container's ``path`` from list_containers.

    Returns:
        JSON string with ``versions``: list of
        {containerVersionId, name, deleted, path}.

    Raises:
        ValueError: If container_path is missing.
    """
    container_path = _require(container_path, "container_path")
    client = _get_client()
    response = _call_with_retry(
        lambda: client.accounts().containers().version_headers()
        .list(parent=container_path).execute(),
        "version_headers.list",
    )
    versions = response.get("containerVersionHeader", [])
    return json.dumps({
        "versions": [
            {
                "containerVersionId": v["containerVersionId"],
                "name": v.get("name"),
                "deleted": v.get("deleted", False),
                "path": v["path"],
            }
            for v in versions
        ],
    }, indent=2)


@mcp.tool(annotations=_PUBLISH)
def publish_version(version_path: str) -> str:
    """Publish a GTM container version, making it live on every site using
    that container immediately.

    Not rate-limited like the tools above: publishing is already gated by
    requiring an explicit, non-auto-approved tool call (see the destructive
    annotation), and a rate-limit wrapper here would only slow down an
    already-deliberate action, not add safety.

    Reverting: GTM keeps prior versions -- to undo, list_versions and
    publish an earlier one. There is no separate "unpublish" or "rollback"
    tool in this MVP; re-publishing an older version is the same operation
    as publishing any other version.

    Args:
        version_path: A container version's ``path`` from create_version or
            list_versions, e.g.
            'accounts/123/containers/456/versions/7'.

    Returns:
        JSON string with containerVersionId, name, and path of the now-live
        version.

    Raises:
        ValueError: If version_path is missing.
    """
    version_path = _require(version_path, "version_path")
    client = _get_client()
    result = _call_with_retry(
        lambda: client.accounts().containers().versions()
        .publish(path=version_path).execute(),
        "versions.publish",
    )
    version = result["containerVersion"]
    return json.dumps({
        "containerVersionId": version["containerVersionId"],
        "name": version.get("name"),
        "path": version["path"],
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
