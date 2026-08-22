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

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[2]


class PatchDeliveryContractTest(unittest.TestCase):

    def test_every_patch_has_an_install_or_documented_packaging_path(self):
        plugin = (ROOT / 'devstack' / 'plugin.sh').read_text()
        matrix = (ROOT / 'doc' / 'source' / 'upgrade_matrix.rst').read_text()
        patches = sorted((ROOT / 'patches').glob('*/*.patch'))

        for patch in patches:
            relative = patch.relative_to(ROOT).as_posix()
            self.assertIn(relative, plugin, relative)
            self.assertIn(patch.stem, matrix, relative)

    def test_non_fork_patch_maintenance_policy_is_documented(self):
        matrix = (ROOT / 'doc' / 'source' / 'upgrade_matrix.rst').read_text()
        guide = (ROOT / 'doc' / 'source' / 'deployment_guide.rst').read_text()
        matrix_words = ' '.join(matrix.split())
        guide_words = ' '.join(guide.split())

        for statement in (
                'Non-fork dependency patch policy',
                'kept as pristine',
                'authoritative copies of every required downstream change',
                'openstack-incus maintainers',
                'release reviewers',
                'deployment operators',
                'Every upstream version change',
                'A patch must be removed when upstream provides equivalent',
                'Do not retain a downstream patch merely because it still '
                'applies'):
            self.assertIn(statement, matrix_words)

        for project in (
                'Nova', 'os-brick', 'python-glanceclient', 'Ceilometer',
                'Manila'):
            self.assertIn(project, matrix_words)
            self.assertIn(project, guide_words)

        self.assertIn(':doc:`upgrade_matrix`', guide_words)
        self.assertIn(
            'There is currently no Manila source patch', matrix_words)
        self.assertIn('Manila itself is not patched', guide_words)

    def test_forked_dependencies_are_distinguished_from_patches(self):
        matrix = (ROOT / 'doc' / 'source' / 'upgrade_matrix.rst').read_text()
        guide = (ROOT / 'doc' / 'source' / 'deployment_guide.rst').read_text()
        settings = (ROOT / 'devstack' / 'settings').read_text()
        matrix_words = ' '.join(matrix.split())
        guide_words = ' '.join(guide.split())

        for statement in (
                'Forked dependency policy',
                'https://github.com/fivetime/incus',
                'https://github.com/fivetime/incus-python-sdk',
                'canonical/pylxd',
                'Instance.console_log()',
                'cryptography>=43.0.3',
                '72568c3',
                '1a26b14',
                'mutable branch name such as ``main``'):
            self.assertIn(statement, matrix_words)

        self.assertIn('Incus Python SDK fork commit', guide_words)
        self.assertIn('INCUS_PYTHON_SDK_BRANCH=main', guide_words)
        self.assertIn(
            'https://github.com/fivetime/incus-python-sdk.git', settings)

    def test_retired_lxc_fork_is_not_a_release_dependency(self):
        matrix = (ROOT / 'doc' / 'source' / 'upgrade_matrix.rst').read_text()
        guide = (ROOT / 'doc' / 'source' / 'deployment_guide.rst').read_text()
        matrix_words = ' '.join(matrix.split())
        guide_words = ' '.join(guide.split())

        for statement in (
                'LXC is explicitly **not** an active project fork',
                'criu-finalize-cgroups-after-restore',
                '6ebdb54a2',
                'f30cbb86f',
                'lxc/lxc#4695',
                'https://github.com/lxc/lxc.git'):
            self.assertIn(statement, matrix_words)

        self.assertIn('upstream LXC and CRIU commits', guide_words)
        self.assertIn('LXC is not one of those forks', guide_words)
        self.assertIn('retired ``fivetime/lxc``', guide_words)

    def test_runtime_gates_cover_patched_service_roles(self):
        nova_gate = (
            ROOT / 'tools' / 'openstack-incus-nova-runtime-preflight.sh'
        ).read_text()
        ceilometer_gate = (
            ROOT / 'tools' / 'openstack-incus-ceilometer-runtime-preflight.sh'
        ).read_text()
        fleet_gate = (
            ROOT / 'tools' / 'openstack-incus-fleet-preflight.sh'
        ).read_text()

        for role in ('api', 'compute', 'conductor'):
            self.assertIn(role, nova_gate)
        for symbol in (
                'IncusLiveMigrateData', 'IterableWithLength',
                '_find_root_device', 'image_size', '_UnixAdapter',
                'ks_identity.V3Token(',
                'load_session_from_conf_options(CONF,confgrp,auth=token_auth)',
                'session=token_session', 'oslo_conf=CONF'):
            self.assertIn(symbol, nova_gate)
        self.assertIn('registered_migrate_versions', nova_gate)
        self.assertIn('"noudev" not in rbd_source', nova_gate)
        self.assertIn('/run/udev/control', nova_gate)
        self.assertIn('MIN_INCUS_MIGRATE_DATA_VERSION', nova_gate)
        self.assertIn('minimum_migrate_data_version_tuple', nova_gate)
        self.assertIn('max(registered_migrate_versions', nova_gate)
        self.assertIn(
            'IncusLiveMigrateData version {} or newer', nova_gate)
        for symbol in (
                'ceilometer-acompute', 'hypervisor_inspector',
                'instance_discovery_method', 'polling_file',
                'yaml.safe_load', 'Finished polling pollster cpu',
                'instance_network_interface', 'ResourceTypeNotFound',
                'ceilometer-volume-io'):
            self.assertIn(symbol, ceilometer_gate)
        self.assertIn('REQUIRE_CEILOMETER_RUNTIME', fleet_gate)
        self.assertIn('CEILOMETER_NOTIFICATION_NODES', fleet_gate)
        self.assertIn('CEILOMETER_COMPUTE_NODES', fleet_gate)

    def test_ceilometer_inspector_is_registered(self):
        setup = (ROOT / 'setup.cfg').read_text()
        self.assertIn('ceilometer.compute.virt =', setup)
        self.assertIn(
            'incus = nova.virt.incus.ceilometer:IncusInspector', setup)
        inspector = (ROOT / 'nova' / 'virt' / 'incus' /
                     'ceilometer.py').read_text()
        self.assertIn('incus_config.register_opts(conf)', inspector)


if __name__ == '__main__':
    unittest.main()
