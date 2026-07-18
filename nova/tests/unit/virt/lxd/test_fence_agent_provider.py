# Copyright 2026 OpenStack Incus Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
from unittest import mock

from nova import test


SCRIPT = (
    Path(__file__).parents[5]
    / "tools"
    / "openstack-incus-fence-agent-provider"
)
LOADER = importlib.machinery.SourceFileLoader("fence_provider", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
provider = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(provider)


def secure_stat(mode=stat.S_IFREG | 0o600, uid=0):
    result = mock.Mock()
    result.st_mode = mode
    result.st_uid = uid
    result.st_size = 1
    return result


class FenceAgentProviderTest(test.NoDBTestCase):

    def test_rejects_path_traversal(self):
        error = self.assertRaises(
            provider.ProviderError,
            provider.load_config,
            Path("/etc"),
            "../node",
        )
        self.assertIn("invalid fence ID", str(error))

    @mock.patch.object(os, "open", return_value=7)
    @mock.patch.object(os, "close")
    @mock.patch.object(os, "fstat", return_value=secure_stat(
        stat.S_IFREG | 0o640))
    def test_rejects_insecure_configuration(
            self, mock_fstat, mock_close, mock_open):
        error = self.assertRaises(
            provider.ProviderError,
            provider.load_config,
            Path("/etc"),
            "node-01",
        )
        self.assertIn("group/other", str(error))

    def test_rejects_unknown_agent(self):
        payload = (
            '{"agent":"shell","ip":"bmc","username":"u",'
            '"password_file":"/secret"}'
        )
        with (
            mock.patch.object(
                provider,
                "_read_secure_file",
                return_value=payload,
            ),
        ):
            error = self.assertRaises(
                provider.ProviderError,
                provider.load_config,
                Path("/cfg"),
                "node-01",
            )
        self.assertIn("unsupported fence agent", str(error))

    def test_secret_uses_stdin_and_status_is_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "node-01.json"
            secret = root / "password"
            agent_dir = root / "agents"
            agent_dir.mkdir()
            agent = agent_dir / "fence_ipmilan"
            config.write_text(
                '{"agent":"ipmilan","ip":"192.0.2.1",'
                '"username":"operator",'
                f'"password_file":"{secret}"}}',
                encoding="utf-8",
            )
            secret.write_text("super-secret\n", encoding="utf-8")
            agent.write_text("#!/bin/sh\n", encoding="utf-8")
            agent.chmod(0o755)
            config.chmod(0o600)
            secret.chmod(0o600)
            stdout = io.StringIO()
            completed = mock.Mock(returncode=0, stdout="Status: ON\n")
            completed.stderr = ""
            with (
                mock.patch.object(
                    provider,
                    "_read_secure_file",
                    side_effect=[
                        config.read_text(encoding="utf-8"),
                        "super-secret\n",
                    ],
                ),
                mock.patch.object(
                    provider.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
                contextlib.redirect_stdout(stdout),
            ):
                result = provider.main(
                    [
                        "status",
                        "node-01",
                        "--config-dir",
                        str(root),
                        "--agent-dir",
                        str(agent_dir),
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual("on\n", stdout.getvalue())
            command = run.call_args.args[0]
            payload = run.call_args.kwargs["input"]
            self.assertEqual([str(agent)], command)
            self.assertNotIn("super-secret", " ".join(command))
            self.assertIn("action=status\n", payload)
            self.assertIn("password=super-secret\n", payload)

    @mock.patch.object(provider, "invoke", return_value="success")
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_fails_closed_on_ambiguous_output(
            self, mock_load, mock_invoke):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, provider.main(["status", "node-01"]))

    @mock.patch.object(provider, "invoke", return_value="Status: OFF\nnow ON")
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_fails_closed_on_conflicting_output(
            self, mock_load, mock_invoke):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, provider.main(["status", "node-01"]))

    def test_agent_failure_redacts_password(self):
        completed = mock.Mock(
            returncode=1,
            stdout="",
            stderr="authentication rejected secret-value",
        )
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(os, "access", return_value=True),
            mock.patch.object(
                provider.subprocess,
                "run",
                return_value=completed,
            ),
        ):
            error = self.assertRaises(
                provider.ProviderError,
                provider.invoke,
                "fence_ipmilan",
                {"password": "secret-value"},
                "status",
                Path("/usr/sbin"),
            )
        self.assertNotIn("secret-value", str(error))
        self.assertIn("[REDACTED]", str(error))
