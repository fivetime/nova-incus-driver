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
import json
import os
from pathlib import Path
from unittest import mock

import fixtures

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


class FenceRetirementWiringTest(test.NoDBTestCase):
    """The order and the record, not just the helpers.

    The helper tests below prove the power check refuses correctly; they
    would all still pass if a regression moved the check after the
    retirement, or dropped the confirmation instead of writing it into
    the permanent ledger. These exercise main() end to end.
    """

    INSTANCE = '00000000-0000-0000-0000-000000000001'
    HOST = '00000000-0000-0000-0000-000000000002'

    def setUp(self):
        super().setUp()
        self.fence_dir = self.useFixture(fixtures.TempDir()).path
        self.calls = []
        self.allocator = mock.Mock()
        self.allocator.get.return_value = mock.Mock(
            allocation_id='10000000-0000-0000-0000-000000000003')
        self.allocator.fence_retire_claim.side_effect = (
            lambda *a, **kw: self.calls.append('retire'))
        self.allocator.audit_state.return_value = ([], [], [])
        # registry_document serialises these straight into the report.
        self.allocator.base = 1000000
        self.allocator.count = 10000
        self.allocator.size = 65536
        self.allocator.namespace = 'region-one-cell1'
        self.allocator.endpoint = 'https://127.0.0.1:2379'
        self.allocator.fingerprint = 'a' * 64

    def _fence_entry(self, compute_id=None):
        entry = {
            'agent': 'virsh', 'ip': '192.0.2.9', 'username': 'root',
            'identity_file': '/root/.ssh/id', 'plug': 'compute-1',
        }
        if compute_id:
            entry['compute_id'] = compute_id
        path = os.path.join(self.fence_dir, 'compute-1.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(entry, handle)
        return path

    def _argv(self, *extra):
        return [
            '--endpoint', 'https://127.0.0.1:2379',
            '--namespace', 'region-one-cell1',
            '--base', '1000000', '--count', '10000',
            '--fence-retire-host-claim', self.INSTANCE,
            '--host-id', self.HOST,
            '--fence-agent', 'fence_virsh',
            '--fenced-at', '2026-08-07T00:00:00Z',
            '--operator', 'ops@example.com',
            '--fence-evidence', 'rc=0',
            '--fence-config-dir', self.fence_dir,
        ] + list(extra)

    def _run(self, *extra, power='off'):
        self._fence_entry(compute_id=self.HOST)

        def fake_run(command, **kwargs):
            self.calls.append('power-check')
            return mock.Mock(stdout=power + '\n', stderr='',
                             returncode=2 if power == 'off' else 0)

        with mock.patch.object(registry.subprocess, 'run', fake_run):
            return registry.main(
                self._argv('--fence-plug', 'compute-1', *extra),
                allocator_factory=lambda **kwargs: self.allocator,
                stdout=mock.Mock(), stderr=mock.Mock())

    def test_power_is_checked_before_anything_is_retired(self):
        self.assertEqual(0, self._run())

        self.assertEqual(['power-check', 'retire'], self.calls)

    def test_a_live_host_is_never_retired(self):
        self._run(power='on')

        self.assertEqual(['power-check'], self.calls)
        self.allocator.fence_retire_claim.assert_not_called()

    def test_the_confirmation_reaches_the_permanent_ledger(self):
        self._run()

        proof = self.allocator.fence_retire_claim.call_args[0][2]
        self.assertIn('rc=0', proof.evidence)
        self.assertIn('powered off', proof.evidence)
        self.assertIn('confirms compute', proof.evidence)

    def test_a_fence_entry_naming_another_compute_stops_the_retirement(self):
        # The power check answers a question about --fence-plug; without
        # this the retirement could act on an unrelated, live --host-id.
        self._fence_entry(compute_id='00000000-0000-0000-0000-0000000000ff')

        with mock.patch.object(
                registry.subprocess, 'run',
                return_value=mock.Mock(
                    stdout='off\n', stderr='', returncode=2)):
            rc = registry.main(
                self._argv('--fence-plug', 'compute-1'),
                allocator_factory=lambda **kwargs: self.allocator,
                stdout=mock.Mock(), stderr=mock.Mock())

        self.assertEqual(2, rc)
        self.allocator.fence_retire_claim.assert_not_called()

    def test_an_undeclared_binding_is_recorded_rather_than_refused(self):
        # Existing deployments have no compute_id; they keep working, but
        # the ledger says the binding was never proven.
        self._fence_entry()

        with mock.patch.object(
                registry.subprocess, 'run',
                return_value=mock.Mock(
                    stdout='off\n', stderr='', returncode=2)):
            rc = registry.main(
                self._argv('--fence-plug', 'compute-1'),
                allocator_factory=lambda **kwargs: self.allocator,
                stdout=mock.Mock(), stderr=mock.Mock())

        self.assertEqual(0, rc)
        proof = self.allocator.fence_retire_claim.call_args[0][2]
        self.assertIn('binding is unverified', proof.evidence)

    def test_a_waived_power_state_is_recorded_and_nothing_is_probed(self):
        with mock.patch.object(registry.subprocess, 'run') as run:
            rc = registry.main(
                self._argv(
                    '--unverified-power-state', 'BMC unreachable'),
                allocator_factory=lambda **kwargs: self.allocator,
                stdout=mock.Mock(), stderr=mock.Mock())

        self.assertEqual(0, rc)
        run.assert_not_called()
        proof = self.allocator.fence_retire_claim.call_args[0][2]
        self.assertIn('BMC unreachable', proof.evidence)
