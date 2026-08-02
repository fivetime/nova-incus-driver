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

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PATCH = (
    REPO_ROOT / 'patches' / 'nova' /
    '0005-compute-add-failed-build-allocation-policy.patch')
DEVSTACK_PLUGIN = REPO_ROOT / 'devstack' / 'plugin.sh'


class NovaFailedBuildPatchContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.patch = PATCH.read_text(encoding='utf-8')
        cls.plugin = DEVSTACK_PLUGIN.read_text(encoding='utf-8')

    def test_patch_preserves_default_and_adds_policy_call(self):
        self.assertIn(
            'def _should_delete_allocation_for_failed_build(', self.patch)
        self.assertIn('+        return True', self.patch)
        self.assertIn(
            'self._should_delete_allocation_for_failed_build(\n'
            '+                                context, instance)',
            self.patch)
        self.assertIn(
            'delete_allocation_for_instance', self.patch)

    def test_patch_covers_default_delete_and_retention(self):
        self.assertIn(
            'test_failed_build_deletes_allocation_by_default', self.patch)
        self.assertIn(
            'test_failed_build_policy_can_retain_allocation', self.patch)
        self.assertIn('delete_allocation.assert_not_called()', self.patch)
        self.assertIn('mock_failed.assert_called_once_with', self.patch)

    def test_devstack_applies_patch_mandatorily_and_idempotently(self):
        patch_name = (
            '0005-compute-add-failed-build-allocation-policy.patch')
        self.assertIn(patch_name, self.plugin)
        self.assertIn(
            'apply --reverse --check \\\n'
            '            "${failed_build_allocation_patch}"', self.plugin)
        self.assertIn(
            'apply --check \\\n'
            '            "${failed_build_allocation_patch}"', self.plugin)
        self.assertIn(
            'Nova failed-build allocation policy patch does not apply '
            'cleanly', self.plugin)


if __name__ == '__main__':
    unittest.main()
