"""Tests for server.py.

All Google Tag Manager API calls are mocked -- no real credentials are
needed to run these tests.
Run with: pytest test_server.py -v
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


def _load_module(env_overrides=None):
    """Import server.py with a fresh environment.

    Args:
        env_overrides: Extra environment variables to apply for the duration
            of the import, merged over a minimal working default.

    Returns:
        The freshly imported ``server`` module.
    """
    env = {
        "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/service_account.json",
        **(env_overrides or {}),
    }
    with patch.dict(os.environ, env, clear=False):
        if "server" in sys.modules:
            del sys.modules["server"]
        import server as mod
    return mod


def _mock_client_returning(resource_chain, execute_result):
    """Build a MagicMock client where a chained resource call returns a fixed result.

    Args:
        resource_chain: Dotted chain of method names to call on the client,
            e.g. "accounts.containers.workspaces.tags" for
            client.accounts().containers().workspaces().tags().
        execute_result: The dict .execute() should return once the server's
            actual request method (list/create/publish) is called on the
            resolved resource and then .execute() is called on that.

    Returns:
        (client, node): client is the mock to pass as _get_client's return
        value; node is the resolved resource (e.g. the tags() collection)
        so a test can assert on node.create.call_args etc. .execute()
        returns execute_result regardless of which request method
        (list/create/publish) the code under test happens to call.
    """
    client = MagicMock()
    node = client
    for name in resource_chain.split("."):
        node = getattr(node, name).return_value
    for request_method in ("list", "create", "publish", "create_version"):
        getattr(node, request_method).return_value.execute.return_value = execute_result
    return client, node


class TestListAccounts(unittest.TestCase):

    def test_returns_accounts_with_path(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts",
            {"account": [{"accountId": "1", "name": "Acme", "path": "accounts/1"}]},
        )
        with patch("server._get_client", return_value=client):
            result = mod.list_accounts()
        data = json.loads(result)
        self.assertEqual(
            data["accounts"],
            [{"accountId": "1", "name": "Acme", "path": "accounts/1"}],
        )

    def test_empty_when_no_accounts(self):
        mod = _load_module()
        client, node = _mock_client_returning("accounts", {})
        with patch("server._get_client", return_value=client):
            result = mod.list_accounts()
        self.assertEqual(json.loads(result)["accounts"], [])


class TestListContainers(unittest.TestCase):

    def test_requires_account_path(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.list_containers("")

    def test_returns_containers(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers",
            {"container": [{"containerId": "9", "name": "Site", "publicId": "GTM-X",
                             "path": "accounts/1/containers/9"}]},
        )
        with patch("server._get_client", return_value=client):
            result = mod.list_containers("accounts/1")
        data = json.loads(result)
        self.assertEqual(data["containers"][0]["publicId"], "GTM-X")


class TestCreateWorkspace(unittest.TestCase):

    def test_creates_and_returns_workspace(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.workspaces",
            {"workspaceId": "5", "name": "claude-edits", "path": "accounts/1/containers/9/workspaces/5"},
        )
        with patch("server._get_client", return_value=client):
            result = mod.create_workspace("accounts/1/containers/9", "claude-edits")
        data = json.loads(result)
        self.assertEqual(data["workspaceId"], "5")
        node.create.assert_called_once()
        _, kwargs = node.create.call_args
        self.assertEqual(kwargs["body"], {"name": "claude-edits"})

    def test_requires_name(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.create_workspace("accounts/1/containers/9", "   ")


class TestCreateTag(unittest.TestCase):

    def test_creates_tag_with_parsed_parameters_and_triggers(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.workspaces.tags",
            {"tagId": "3", "name": "GA4 Lead Event", "type": "gaawe",
             "path": "accounts/1/containers/9/workspaces/5/tags/3"},
        )
        with patch("server._get_client", return_value=client):
            result = mod.create_tag(
                "accounts/1/containers/9/workspaces/5",
                "GA4 Lead Event",
                "gaawe",
                parameter='[{"type":"template","key":"eventName","value":"generate_lead"}]',
                firing_trigger_id="10, 11",
            )
        data = json.loads(result)
        self.assertEqual(data["tagId"], "3")
        _, kwargs = node.create.call_args
        self.assertEqual(kwargs["body"]["firingTriggerId"], ["10", "11"])
        self.assertEqual(
            kwargs["body"]["parameter"],
            [{"type": "template", "key": "eventName", "value": "generate_lead"}],
        )

    def test_rejects_invalid_parameter_json(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.create_tag("accounts/1/containers/9/workspaces/5", "Tag", "html",
                                parameter="not json")

    def test_rejects_parameter_that_is_not_a_list(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.create_tag("accounts/1/containers/9/workspaces/5", "Tag", "html",
                                parameter='{"not": "a list"}')

    def test_omits_optional_fields_when_not_provided(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.workspaces.tags",
            {"tagId": "3", "name": "Tag", "type": "html", "path": "p"},
        )
        with patch("server._get_client", return_value=client):
            mod.create_tag("accounts/1/containers/9/workspaces/5", "Tag", "html")
        _, kwargs = node.create.call_args
        self.assertNotIn("parameter", kwargs["body"])
        self.assertNotIn("firingTriggerId", kwargs["body"])


class TestCreateVersionAndPublish(unittest.TestCase):

    def test_create_version_unwraps_container_version(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.workspaces",
            {"containerVersion": {"containerVersionId": "7", "name": "v1",
                                   "path": "accounts/1/containers/9/versions/7"}},
        )
        with patch("server._get_client", return_value=client):
            result = mod.create_version("accounts/1/containers/9/workspaces/5", "v1")
        data = json.loads(result)
        self.assertEqual(data["containerVersionId"], "7")

    def test_publish_version_unwraps_container_version(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.versions",
            {"containerVersion": {"containerVersionId": "7", "name": "v1",
                                   "path": "accounts/1/containers/9/versions/7"}},
        )
        with patch("server._get_client", return_value=client):
            result = mod.publish_version("accounts/1/containers/9/versions/7")
        data = json.loads(result)
        self.assertEqual(data["containerVersionId"], "7")
        node.publish.assert_called_once_with(path="accounts/1/containers/9/versions/7")

    def test_publish_version_requires_path(self):
        mod = _load_module()
        with patch("server._get_client", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                mod.publish_version("")


class TestListVersions(unittest.TestCase):

    def test_returns_version_headers(self):
        mod = _load_module()
        client, node = _mock_client_returning(
            "accounts.containers.version_headers",
            {"containerVersionHeader": [
                {"containerVersionId": "7", "name": "v1", "path": "p", "deleted": False},
            ]},
        )
        with patch("server._get_client", return_value=client):
            result = mod.list_versions("accounts/1/containers/9")
        data = json.loads(result)
        self.assertEqual(data["versions"][0]["containerVersionId"], "7")


class TestRetry(unittest.TestCase):
    """_call_with_retry must retry only the transient status codes, never
    permanent client errors like 403/404, and must give up after the retry
    budget is exhausted."""

    def _http_error(self, status):
        from googleapiclient.errors import HttpError
        resp = MagicMock()
        resp.status = status
        return HttpError(resp, b"{}", uri="https://tagmanager.googleapis.com/x")

    def test_retries_503_then_succeeds(self):
        mod = _load_module()
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise self._http_error(503)
            return "ok"

        with patch("server.time.sleep"):
            result = mod._call_with_retry(flaky, "test_op")
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 2)

    def test_does_not_retry_403(self):
        mod = _load_module()

        def always_403():
            raise self._http_error(403)

        with self.assertRaises(Exception):
            mod._call_with_retry(always_403, "test_op")

    def test_gives_up_after_max_retries(self):
        mod = _load_module()

        def always_503():
            raise self._http_error(503)

        with patch("server.time.sleep"):
            with self.assertRaises(Exception):
                mod._call_with_retry(always_503, "test_op")


if __name__ == "__main__":
    unittest.main()
