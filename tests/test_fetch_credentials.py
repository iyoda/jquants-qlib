from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

from jqqlib.fetch import create_jquants_client


def _security_result(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


_KEYCHAIN_MISSING = _security_result(44)  # security's real exit code for "item not found"


class CreateJquantsClientCredentialTests(unittest.TestCase):
    """Resolver order: env -> Keychain -> (opt-in) yaml -> ValueError.

    No real secret VALUES appear anywhere in this file: env/Keychain doubles
    always return non-real sentinel/fixture strings, and every assertion
    checks the mocked ClientV2/Client constructor call args rather than the
    real macOS Keychain.
    """

    def setUp(self) -> None:
        env_patch = mock.patch.dict(
            "os.environ",
            {},
            clear=True,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_no_env_no_keychain_empty_yaml_raises_value_error(self) -> None:
        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING):
            with self.assertRaises(ValueError):
                create_jquants_client({"jquants": {"api_key": "", "refresh_token": ""}})

    def test_env_wins_over_keychain(self) -> None:
        import os

        os.environ["JQUANTS_API_KEY"] = "fixture-env-api-key"
        with mock.patch("jqqlib.fetch.subprocess.run") as mock_run, mock.patch(
            "jquantsapi.ClientV2"
        ) as mock_client_v2:
            create_jquants_client({"jquants": {}})

        mock_run.assert_not_called()
        mock_client_v2.assert_called_once_with(api_key="fixture-env-api-key")

    def test_keychain_used_when_env_unset(self) -> None:
        sentinel = "fixture-keychain-sentinel-not-a-real-key"

        def _fake_run(argv, **kwargs):
            if argv[3] == "jquants-api-key":
                return _security_result(0, stdout=sentinel + "\n")
            return _KEYCHAIN_MISSING

        with mock.patch("jqqlib.fetch.subprocess.run", side_effect=_fake_run), mock.patch(
            "jquantsapi.ClientV2"
        ) as mock_client_v2:
            create_jquants_client({"jquants": {}})

        mock_client_v2.assert_called_once_with(api_key=sentinel)

    def test_keychain_refresh_token_used_when_api_key_absent_everywhere(self) -> None:
        sentinel = "fixture-keychain-refresh-sentinel"

        def _fake_run(argv, **kwargs):
            if argv[3] == "jquants-refresh-token":
                return _security_result(0, stdout=sentinel + "\n")
            return _KEYCHAIN_MISSING

        with mock.patch("jqqlib.fetch.subprocess.run", side_effect=_fake_run), mock.patch(
            "jqqlib.fetch._refresh_token_client"
        ) as mock_client:
            create_jquants_client({"jquants": {}})

        mock_client.assert_called_once_with(sentinel)

    def test_refresh_token_without_v1_client_raises_actionable_error(self) -> None:
        import builtins

        real_import = builtins.__import__

        def _no_v1_client(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "jquantsapi" and "Client" in (fromlist or ()):
                raise ImportError("cannot import name 'Client'")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING), mock.patch.dict(
            "os.environ", {"JQUANTS_REFRESH_TOKEN": "fixture-refresh"}, clear=False
        ), mock.patch("builtins.__import__", side_effect=_no_v1_client):
            with self.assertRaises(ValueError) as ctx:
                create_jquants_client({"jquants": {}})

        self.assertIn("JQUANTS_API_KEY", str(ctx.exception))
        self.assertNotIn("fixture-refresh", str(ctx.exception))

    def test_yaml_without_flag_does_not_succeed_even_with_api_key(self) -> None:
        config = {"jquants": {"api_key": "fixture-yaml-api-key"}}
        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING):
            with self.assertRaises(ValueError) as ctx:
                create_jquants_client(config)

        # The error explains why the value in the file was not used, without echoing it.
        message = str(ctx.exception)
        self.assertIn("JQQLIB_ALLOW_YAML_CREDENTIALS=1", message)
        self.assertNotIn("fixture-yaml-api-key", message)

    def test_hint_about_yaml_credentials_is_absent_when_none_are_set(self) -> None:
        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING):
            with self.assertRaises(ValueError) as ctx:
                create_jquants_client({"jquants": {"api_key": "", "refresh_token": ""}})

        self.assertNotIn("JQQLIB_ALLOW_YAML_CREDENTIALS", str(ctx.exception))

    def test_yaml_with_allow_flag_succeeds_and_alerts_on_stderr(self) -> None:
        import os

        os.environ["JQQLIB_ALLOW_YAML_CREDENTIALS"] = "1"
        config = {"jquants": {"api_key": "fixture-yaml-api-key"}}
        stderr_capture = io.StringIO()

        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING), mock.patch(
            "jquantsapi.ClientV2"
        ) as mock_client_v2, redirect_stderr(stderr_capture):
            create_jquants_client(config)

        mock_client_v2.assert_called_once_with(api_key="fixture-yaml-api-key")
        self.assertIn("ALERT", stderr_capture.getvalue())

    def test_keychain_service_name_env_override_is_read_at_call_time(self) -> None:
        import os

        # jqqlib.fetch is already imported; the override must still take effect.
        os.environ["JQQLIB_KEYCHAIN_API_KEY_SERVICE"] = "jqqlib-override-api-key"
        os.environ["JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE"] = "jqqlib-override-refresh"
        sentinel = "fixture-override-refresh-sentinel"
        seen_services: list[str] = []

        def _fake_run(argv, **kwargs):
            seen_services.append(argv[3])
            if argv[3] == "jqqlib-override-refresh":
                return _security_result(0, stdout=sentinel + "\n")
            return _KEYCHAIN_MISSING

        with mock.patch("jqqlib.fetch.subprocess.run", side_effect=_fake_run), mock.patch(
            "jqqlib.fetch._refresh_token_client"
        ) as mock_client:
            create_jquants_client({"jquants": {}})

        self.assertEqual(seen_services, ["jqqlib-override-api-key", "jqqlib-override-refresh"])
        mock_client.assert_called_once_with(sentinel)

    def test_error_message_never_mentions_config_yaml_as_default_path(self) -> None:
        with mock.patch("jqqlib.fetch.subprocess.run", return_value=_KEYCHAIN_MISSING):
            with self.assertRaises(ValueError) as ctx:
                create_jquants_client({"jquants": {}})

        message = str(ctx.exception)
        self.assertNotIn("config.yaml", message.lower())
        self.assertIn("Keychain", message)
        self.assertIn("JQUANTS_API_KEY", message)
        self.assertIn("jquants-api-key", message)
        self.assertIn("jquants-refresh-token", message)


if __name__ == "__main__":
    unittest.main()
