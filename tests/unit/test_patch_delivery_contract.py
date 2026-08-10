# Copyright 2026 OpenStack Incus contributors
# Licensed under the Apache License, Version 2.0 (the "License").

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
                'IncusLiveMigrateData', 'IterableWithLength', 'noudev',
                '_find_root_device', 'image_size', '_UnixAdapter'):
            self.assertIn(symbol, nova_gate)
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
