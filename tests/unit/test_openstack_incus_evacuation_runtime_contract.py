# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
EVACUATION = (
    REPO_ROOT / 'tools' / 'openstack-incus-bfv-evacuation-e2e.sh')
RETURN_AUDIT = (
    REPO_ROOT / 'tools' / 'openstack-incus-returning-host-audit.sh')


class EvacuationRuntimeContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.evacuation = EVACUATION.read_text(encoding='utf-8')
        cls.return_audit = RETURN_AUDIT.read_text(encoding='utf-8')

    def test_both_scripts_support_exact_kubernetes_node_mapping(self):
        for script in (self.evacuation, self.return_audit):
            self.assertIn(
                'INCUS_RUNTIME_MODE=${INCUS_RUNTIME_MODE:-podman}', script)
            self.assertIn(
                'INCUS_KUBE_NODE_MAP=${INCUS_KUBE_NODE_MAP:-}', script)
            self.assertIn('incus_runtime_remote()', script)
            self.assertIn('spec.nodeName=$kube_node', script)
            self.assertIn(
                'kubectl -n $namespace get pod -l application=incus',
                script)
        self.assertIn('compute_runtime_remote()', self.evacuation)
        self.assertIn(
            'application=nova,component=compute-incus', self.evacuation)

    def test_evacuation_streams_marker_through_selected_runtime(self):
        self.assertIn('incus_runtime_remote_stdin()', self.evacuation)
        self.assertIn('kubectl -n $namespace exec -i', self.evacuation)
        self.assertIn(
            'incus_runtime_remote_stdin "$SOURCE_SSH" incus',
            self.evacuation)

    def test_no_incus_operation_is_hard_coded_to_podman(self):
        for script in (self.evacuation, self.return_audit):
            podman_lines = [
                line for line in script.splitlines()
                if 'podman exec' in line
            ]
            self.assertTrue(podman_lines)
            self.assertTrue(all(
                'INCUS_RUNTIME_CONTAINER' in line for line in podman_lines))

    def test_runtime_settings_are_forwarded_to_return_audit(self):
        for name in (
                'INCUS_RUNTIME_MODE', 'INCUS_RUNTIME_CONTAINER',
                'INCUS_KUBE_NAMESPACE', 'INCUS_KUBE_NODE_MAP',
                'INCUS_KUBE_ADMISSION_LABEL_KEY',
                'INCUS_KUBE_ADMISSION_LABEL_VALUE'):
            self.assertIn(f'{name}="${name}"', self.evacuation)

    def test_kubernetes_quarantine_precedes_return_audit(self):
        self.assertIn('kube_compute_daemonset_is_guarded()', self.evacuation)
        self.assertIn('kube_quarantine_source_compute()', self.evacuation)
        self.assertIn('kube_source_compute_absent()', self.evacuation)
        self.assertIn('kube_admit_source_compute()', self.evacuation)
        quarantine = self.evacuation.index('kube_quarantine_source_compute\n')
        power_on = self.evacuation.index(
            '"$FENCE_PROVIDER" on "$SOURCE_FENCE_ID"')
        audit = self.evacuation.index('bash "$RETURN_AUDIT"')
        admit = self.evacuation.index('kube_admit_source_compute\n')
        self.assertLess(quarantine, power_on)
        self.assertLess(power_on, audit)
        self.assertLess(audit, admit)

    def test_return_audit_checks_kubernetes_label_and_compute_pod(self):
        self.assertIn('kube_returning_node_label()', self.return_audit)
        self.assertIn('kube_returning_compute_count()', self.return_audit)
        self.assertIn('active_compute_pods=$compute_count', self.return_audit)


if __name__ == '__main__':
    unittest.main()
