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

import dataclasses
import importlib.util
import io
from pathlib import Path
import tempfile
import uuid

from nova import test
from nova.virt.incus import idmap
from oslo_serialization import jsonutils


TOOL = (Path(__file__).resolve().parents[2] / "tools" /
        "openstack-incus-idmap-registry.py")
SPEC = importlib.util.spec_from_file_location("incus_idmap_registry", TOOL)
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)
IDMAP_TEST = (Path(__file__).resolve().parents[2] / "nova" / "tests" /
              "unit" / "virt" / "incus" / "test_idmap.py")
IDMAP_TEST_SPEC = importlib.util.spec_from_file_location(
    "incus_test_idmap", IDMAP_TEST)
test_idmap = importlib.util.module_from_spec(IDMAP_TEST_SPEC)
IDMAP_TEST_SPEC.loader.exec_module(test_idmap)


class IDMapRegistryV3Test(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.etcd = test_idmap._FakeEtcd()
        self.allocator = self._allocator(self.etcd)
        self.allocator.bootstrap()

    @staticmethod
    def _uuid(number):
        return str(uuid.UUID(int=number))

    def _allocator(self, client):
        return idmap.IDMapAllocator(
            endpoint="http://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            allow_insecure=True,
            client=client,
        )

    def _cleaned_claim(self, instance_number=1, host_number=1,
                       token_number=1):
        assignment = self.allocator.allocate(self._uuid(instance_number))
        host_id = self._uuid(10000 + host_number)
        token = self._uuid(20000 + token_number)
        assignment = self.allocator.claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        proof = idmap.IDMapMaterializationProof(
            token=token,
            allocation_id=assignment.allocation_id,
            compute_id=host_id,
            owner=assignment.instance_uuid,
            project="nova",
            instance_name="instance-00000001",
            idmap_base=assignment.base,
            idmap_size=assignment.size,
            storage_driver="ceph",
            storage_pool="root-pool",
            storage_volume="nova_instance-00000001",
            rbd_image="container_nova_instance-00000001",
            storage_identity="rbd-image-id",
            baseline_clean=True,
            cleanup_disposition="delete",
            state="clean",
            storage_phase="clean",
            started=True,
            finished=True,
            outcome="reconciled-clean",
            digest="",
        )
        proof = dataclasses.replace(
            proof, digest=self.allocator._materialization_digest(proof))
        claim = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        return assignment, claim

    def test_export_is_canonical_v3_with_full_claim_proof(self):
        assignment, claim = self._cleaned_claim()
        intent = self.allocator.request_release(
            assignment.instance_uuid, "instance-00000001",
            assignment=assignment)
        document = registry.registry_document(
            self.allocator, [assignment], [intent])
        self.assertEqual(
            "openstack-incus-idmap-registry/v3", document["schema"])
        payload = document["assignments"][0]
        self.assertNotIn("rootfs_materialized", payload)
        self.assertNotIn("host_ids", payload)
        self.assertEqual(1, len(payload["host_claims"]))
        claim_payload = payload["host_claims"][0]
        self.assertEqual(claim.materialization_id,
                         claim_payload["materialization_id"])
        self.assertEqual("cleaned", claim_payload["state"])
        self.assertEqual(
            "materialization-attempt", claim_payload["proof"]["kind"])

    def test_export_uses_one_linearizable_prefix_snapshot(self):
        assignment, unused_claim = self._cleaned_claim()
        self.etcd.get_calls.clear()
        self.etcd.get_prefix_calls.clear()
        registry.registry_document(self.allocator, [assignment], [])
        self.assertEqual([], self.etcd.get_calls)
        self.assertEqual(
            ["%s/" % self.allocator._prefix],
            self.etcd.get_prefix_calls)

    def test_v2_document_is_rejected_without_upgrade(self):
        document = {
            "assignments": [],
            "base": self.allocator.base,
            "count": self.allocator.count,
            "fingerprint": self.allocator.fingerprint,
            "namespace": self.allocator.namespace,
            "release_intents": [],
            "schema": "openstack-incus-idmap-registry/v2",
            "size": self.allocator.size,
        }
        self.assertRaises(
            ValueError, registry.validate_restore_document,
            self.allocator, document)

    def test_restore_preserves_exact_t_state_and_proof(self):
        assignment, claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        target_etcd = test_idmap._FakeEtcd()
        target = self._allocator(target_etcd)
        restored, intents = registry.restore_registry(target, document)
        repeated, repeated_intents = registry.restore_registry(
            target, document)
        self.assertEqual([assignment], restored)
        self.assertEqual(restored, repeated)
        self.assertEqual([], intents)
        self.assertEqual(intents, repeated_intents)
        self.assertEqual(
            claim,
            target.get_host_claim(assignment.instance_uuid, claim.host_id))

    def test_restore_rejects_changed_materialization_id(self):
        assignment, unused_claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        claim = document["assignments"][0]["host_claims"][0]
        claim["materialization_id"] = self._uuid(29999)
        self.assertRaises(
            (ValueError, idmap.IDMapError),
            registry.validate_restore_document, self.allocator, document)

    def test_restore_rejects_changed_proof_digest(self):
        assignment, unused_claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        proof = document["assignments"][0]["host_claims"][0]["proof"]
        proof["digest"] = "sha256:%s" % ("f" * 64)
        self.assertRaises(
            (ValueError, idmap.IDMapError),
            registry.validate_restore_document, self.allocator, document)

    def test_restore_rejects_proof_without_clean_baseline(self):
        assignment, unused_claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        proof = document["assignments"][0]["host_claims"][0]["proof"]
        proof.pop("baseline_clean")
        self.assertRaises(
            (ValueError, idmap.IDMapError),
            registry.validate_restore_document, self.allocator, document)

    def test_restore_rejects_false_clean_baseline_with_valid_digest(self):
        assignment, unused_claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        proof = document["assignments"][0]["host_claims"][0]["proof"]
        proof["baseline_clean"] = False
        parsed = dict(proof)
        parsed.pop("kind")
        parsed["digest"] = ""
        candidate = idmap.IDMapMaterializationProof(**parsed)
        proof["digest"] = self.allocator._materialization_digest(candidate)
        self.assertRaises(
            (ValueError, idmap.IDMapError),
            registry.validate_restore_document, self.allocator, document)

    def test_restore_rejects_noncanonical_claim_order(self):
        first_assignment, first_claim = self._cleaned_claim(
            instance_number=2, host_number=1, token_number=1)
        host_id = self._uuid(10002)
        token = self._uuid(20002)
        current = self.allocator.claim(
            first_assignment.instance_uuid, host_id, token,
            assignment=first_assignment)
        document = registry.registry_document(
            self.allocator, [current], [])
        document["assignments"][0]["host_claims"].reverse()
        self.assertRaises(
            (ValueError, idmap.IDMapError),
            registry.validate_restore_document, self.allocator, document)

    def test_retirement_requires_frozen_exact_cleaned_proof(self):
        assignment, claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        assignments, intents = registry.retire_host_claim(
            self.allocator, document, assignment.instance_uuid,
            claim.host_id)
        self.assertEqual([], intents)
        self.assertEqual((), assignments[0].host_ids)

    def test_retirement_rejects_unmaterialized_claim(self):
        assignment = self.allocator.allocate(self._uuid(3))
        host_id = self._uuid(10003)
        token = self._uuid(20003)
        assignment = self.allocator.claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        document = registry.registry_document(
            self.allocator, [assignment], [])
        self.assertRaises(
            idmap.IDMapIntegrityError, registry.retire_host_claim,
            self.allocator, document, assignment.instance_uuid, host_id)

    def test_retirement_rejects_live_claim_changed_after_snapshot(self):
        assignment, claim = self._cleaned_claim()
        document = registry.registry_document(
            self.allocator, [assignment], [])
        self.allocator.retire_claim(
            assignment.instance_uuid, claim.host_id,
            claim.materialization_id, assignment=assignment)
        new_token = self._uuid(21111)
        self.allocator.claim(
            assignment.instance_uuid, claim.host_id, new_token,
            assignment=assignment)
        self.assertRaises(
            idmap.IDMapConflict, registry.retire_host_claim,
            self.allocator, document, assignment.instance_uuid,
            claim.host_id)

    def test_load_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text('{"schema":"v3","schema":"v2"}',
                            encoding="utf-8")
            self.assertRaises(ValueError, registry.load_restore_document, path)

    def test_cli_has_no_fence_only_retirement_escape_hatch(self):
        with self.assertRaises(SystemExit):
            registry._parser().parse_args([
                "--endpoint", "http://etcd.example:2379",
                "--namespace", "cell1", "--base", "500000000",
                "--count", "8", "--allow-insecure", "--confirm-fenced",
            ])

    def test_main_audit_outputs_v3(self):
        self.allocator.allocate(self._uuid(4))
        stdout = io.StringIO()
        stderr = io.StringIO()

        def factory(**kwargs):
            return self.allocator

        result = registry.main([
            "--endpoint", "http://etcd.example:2379",
            "--namespace", "cell1", "--base", "500000000",
            "--count", "8", "--allow-insecure",
        ], allocator_factory=factory, stdout=stdout, stderr=stderr)
        self.assertEqual(0, result)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            registry.DOCUMENT_SCHEMA,
            jsonutils.loads(stdout.getvalue())["schema"])
