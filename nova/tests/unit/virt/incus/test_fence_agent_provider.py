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
import json
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

    def test_virsh_uses_validated_identity_without_password(self):
        payload = (
            '{"agent":"virsh","ip":"192.0.2.1","username":"root",'
            '"identity_file":"/root/.ssh/fence","plug":"compute-01"}'
        )
        with mock.patch.object(
                provider,
                "_read_secure_file",
                side_effect=[payload, "private-key"],
        ) as read_file:
            binary, parameters = provider.load_config(
                Path("/cfg"),
                "node-01",
            )

        self.assertEqual("fence_virsh", binary)
        self.assertEqual("/root/.ssh/fence", parameters["identity_file"])
        self.assertTrue(parameters["ssh"])
        self.assertNotIn("password", parameters)
        self.assertEqual(
            mock.call(Path("/root/.ssh/fence"), "fence SSH identity"),
            read_file.call_args_list[1],
        )

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

    @mock.patch.object(provider, "invoke", return_value=("success", 0))
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_fails_closed_on_ambiguous_output(
            self, mock_load, mock_invoke):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, provider.main(["status", "node-01"]))

    @mock.patch.object(
        provider,
        "invoke",
        return_value=("Status: OFF\nnow ON", 2),
    )
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_fails_closed_on_conflicting_output(
            self, mock_load, mock_invoke):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, provider.main(["status", "node-01"]))

    @mock.patch.object(
        provider,
        "invoke",
        return_value=("Status: OFF", 2),
    )
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_accepts_standard_off_return_code(
            self, mock_load, mock_invoke):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, provider.main(["status", "node-01"]))
        self.assertEqual("off\n", stdout.getvalue())

    @mock.patch.object(
        provider,
        "invoke",
        return_value=("Status: ON", 2),
    )
    @mock.patch.object(provider, "load_config", return_value=("agent", {}))
    def test_status_rejects_return_code_output_mismatch(
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


class FenceComputeBindingTest(test.NoDBTestCase):
    """compute_id ties a fence entry to the compute it powers.

    A powered-off host cannot be asked for its own UUID, so the binding
    has to be declared here in advance for tools acting on a fenced host
    to verify it.
    """

    def test_every_agent_accepts_the_binding_key(self):
        # virsh defines its own key set rather than extending the common
        # one, and was missed the first time.
        for name, agent in provider.AGENTS.items():
            self.assertIn(
                'compute_id', agent['keys'],
                '%s must accept compute_id' % name)

    def test_the_binding_is_never_passed_to_the_fence_agent(self):
        # It is metadata for the caller, not a fence agent parameter;
        # forwarding it would make every fence action fail.
        with tempfile.TemporaryDirectory() as config_dir:
            identity = os.path.join(config_dir, 'id')
            with open(identity, 'w', encoding='utf-8') as handle:
                handle.write('key')
            os.chmod(identity, 0o600)
            config = {
                'agent': 'virsh',
                'ip': '192.0.2.9',
                'username': 'root',
                'identity_file': identity,
                'plug': 'compute-1',
                'compute_id': '00000000-0000-0000-0000-000000000002',
            }
            path = os.path.join(config_dir, 'compute-1.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(config, handle)
            os.chmod(path, 0o600)
            with mock.patch.object(
                    provider.os, 'stat', return_value=secure_stat()):
                unused_binary, parameters = provider.load_config(
                    provider.Path(config_dir), 'compute-1')

        self.assertNotIn('compute_id', parameters)
        self.assertEqual('compute-1', parameters['plug'])
