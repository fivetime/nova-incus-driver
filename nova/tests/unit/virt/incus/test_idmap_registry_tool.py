# Copyright 2026 OpenStack Incus contributors
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
import importlib.machinery
import importlib.util
from pathlib import Path
from unittest import mock

from nova import test


SCRIPT = (
    Path(__file__).parents[5]
    / "tools"
    / "openstack-incus-idmap-registry.py"
)
LOADER = importlib.machinery.SourceFileLoader("idmap_registry", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
registry = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(registry)


def _completed(stdout, returncode=0, stderr=""):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


class FencePowerVerificationTest(test.NoDBTestCase):
    """The check that survives a mistyped --host-id.

    Two compute UUIDs usually appear side by side in one ticket, and
    naming the evacuation destination would delete a healthy instance's
    claim and leave forged fence evidence behind.
    """

    def test_confirmed_off_returns_evidence_for_the_ledger(self):
        with mock.patch.object(
                registry.subprocess, 'run',
                return_value=_completed('off\n', 2)) as run:
            confirmation = registry.verify_host_is_powered_off(
                '/usr/local/sbin/provider', 'incus-node-02', None)

        self.assertIn('incus-node-02', confirmation)
        self.assertIn('powered off', confirmation)
        self.assertEqual(
            ['/usr/local/sbin/provider', 'status', 'incus-node-02'],
            run.call_args[0][0])

    def test_refuses_a_host_that_is_still_powered_on(self):
        with mock.patch.object(
                registry.subprocess, 'run',
                return_value=_completed('on\n', 0)):
            self.assertRaisesRegex(
                ValueError, 'reports it powered on',
                registry.verify_host_is_powered_off,
                '/usr/local/sbin/provider', 'incus-node-03', None)

    def test_refuses_when_the_provider_cannot_answer(self):
        for outcome in (
                _completed('', 1, 'agent unreachable'),
                _completed('confused\n', 0)):
            with mock.patch.object(
                    registry.subprocess, 'run', return_value=outcome):
                self.assertRaisesRegex(
                    ValueError, 'usable power state',
                    registry.verify_host_is_powered_off,
                    '/usr/local/sbin/provider', 'incus-node-03', None)

    def test_refuses_when_the_provider_cannot_be_run(self):
        with mock.patch.object(
                registry.subprocess, 'run',
                side_effect=OSError('no such file')):
            self.assertRaisesRegex(
                ValueError, 'cannot confirm',
                registry.verify_host_is_powered_off,
                '/usr/local/sbin/provider', 'incus-node-03', None)

    def test_config_dir_is_forwarded_to_the_provider(self):
        with mock.patch.object(
                registry.subprocess, 'run',
                return_value=_completed('off\n', 2)) as run:
            registry.verify_host_is_powered_off(
                '/usr/local/sbin/provider', 'incus-node-02', '/etc/fence.d')

        command = run.call_args[0][0]
        self.assertEqual('/etc/fence.d', command[command.index(
            '--config-dir') + 1])


class FenceArgumentContractTest(test.NoDBTestCase):

    BASE = [
        '--endpoint', 'https://127.0.0.1:2379',
        '--namespace', 'region-one-cell1',
        '--base', '1000000', '--count', '10000',
        '--fence-retire-host-claim',
        '00000000-0000-0000-0000-000000000001',
        '--host-id', '00000000-0000-0000-0000-000000000002',
        '--fence-agent', 'fence_ipmilan',
        '--fenced-at', '2026-08-07T00:00:00Z',
        '--operator', 'ops@example.com',
        '--fence-evidence', 'rc=0',
    ]

    def _run(self, extra):
        stderr = mock.Mock()
        return registry.main(self.BASE + extra, stdout=mock.Mock(),
                             stderr=stderr)

    def test_retirement_without_any_power_statement_is_refused(self):
        # Silence about the power state is the dangerous default, so it
        # is not a default at all.
        self.assertRaises(SystemExit, self._run, [])

    def test_verifying_and_waiving_at_once_is_refused(self):
        self.assertRaises(
            SystemExit, self._run,
            ['--fence-plug', 'incus-node-02',
             '--unverified-power-state', 'no BMC access'])
