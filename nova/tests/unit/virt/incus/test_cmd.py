# Copyright 2026 OpenStack Incus contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from unittest import mock

from nova import service
from nova import test
from nova.virt.incus.cmd import compute


class ComputeCommandTest(test.NoDBTestCase):

    @mock.patch.object(compute.compute, 'main')
    def test_selects_incus_manager_before_starting_nova(self, nova_main):
        original = service.SERVICE_MANAGERS['nova-compute']
        self.addCleanup(
            service.SERVICE_MANAGERS.__setitem__, 'nova-compute', original)

        compute.main()

        self.assertEqual(
            compute.INCUS_COMPUTE_MANAGER,
            service.SERVICE_MANAGERS['nova-compute'])
        nova_main.assert_called_once_with()
