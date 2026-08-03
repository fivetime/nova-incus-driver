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
from unittest import mock

from nova import test
from nova.virt.incus import idmap
from nova.virt.incus import storage_protocol


class StorageOwnershipClientTest(test.NoDBTestCase):

    TOKEN = "06ada2e7-67f9-4c4e-b071-da45f25cfc67"
    ALLOCATION_ID = "22222222-2222-2222-2222-222222222222"
    COMPUTE_ID = "33333333-3333-3333-3333-333333333333"
    OWNER = "11111111-1111-1111-1111-111111111111"

    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()
        self.client.host_info = {"api_extensions": [
            storage_protocol.STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,
            storage_protocol.STORAGE_RELEASE_RECEIPT_EXTENSION,
        ]}
        self.protocol = storage_protocol.StorageOwnershipClient(self.client)
        self.binding = storage_protocol.StorageMaterializationBinding(
            token=self.TOKEN,
            allocation_id=self.ALLOCATION_ID,
            compute_id=self.COMPUTE_ID,
            owner=self.OWNER,
            project="nova",
            instance_name="instance-00000001",
            idmap_base=1000000,
            idmap_size=65536,
            storage_driver="ceph",
            storage_pool="rootfs",
            storage_volume="nova_instance-00000001",
            cleanup_disposition="delete",
            rbd_image="container_nova_instance-00000001",
        )

    @staticmethod
    def _response(metadata):
        response = mock.Mock()
        response.json.return_value = {"metadata": metadata}
        return response

    def _attempt_metadata(self, **updates):
        metadata = {
            "token": self.binding.token,
            "allocation_id": self.binding.allocation_id,
            "compute_id": self.binding.compute_id,
            "owner": self.binding.owner,
            "project": self.binding.project,
            "instance_name": self.binding.instance_name,
            "idmap_base": self.binding.idmap_base,
            "idmap_size": self.binding.idmap_size,
            "storage_driver": self.binding.storage_driver,
            "storage_pool": self.binding.storage_pool,
            "storage_volume": self.binding.storage_volume,
            "rbd_image": self.binding.rbd_image,
            "storage_identity": "",
            "baseline_clean": True,
            "cleanup_disposition": self.binding.cleanup_disposition,
            "state": "active",
            "storage_phase": "none",
            "started": False,
            "finished": False,
            "operation_uuid": "",
            "daemon_start": 0,
        }
        metadata.update(updates)
        return metadata

    def _materialization_endpoint(self):
        return self.client.api["storage-materialization-attempts"][
            self.binding.token]

    def _receipt_endpoint(self, binding=None):
        binding = binding or self.binding
        return self.client.api["storage-release-receipts"][binding.token]

    def _release_receipt(
            self, storage_driver="ceph", cleanup_disposition="delete",
            outcome="deleted"):
        ceph = storage_driver in ("ceph", "cephext")
        receipt = idmap.IDMapRootfsReleaseReceipt(
            token=self.binding.token,
            allocation_id=self.binding.allocation_id,
            compute_id=self.binding.compute_id,
            materialization_id=self.binding.token,
            owner=self.binding.owner,
            project=self.binding.project,
            instance_name=self.binding.instance_name,
            idmap_base=self.binding.idmap_base,
            idmap_size=self.binding.idmap_size,
            storage_driver=storage_driver,
            storage_pool=self.binding.storage_pool,
            storage_volume=self.binding.storage_volume,
            rbd_image=(self.binding.rbd_image if ceph else ""),
            storage_identity=(
                "rbd_data.1234567890abcdef" if ceph else ""),
            baseline_clean=True,
            cleanup_disposition=cleanup_disposition,
            outcome=outcome,
            state="complete",
            digest="",
            created_at=10,
            completed_at=11,
        )
        proof = idmap.IDMapRootfsReleaseProof(**receipt.__dict__)
        return dataclasses.replace(
            receipt, digest=idmap.rootfs_release_proof_digest(proof))

    def _terminal_attempt_metadata(
            self, storage_identity="rbd_data.1234567890abcdef"):
        metadata = self._attempt_metadata(
            storage_identity=storage_identity,
            state="clean",
            storage_phase="clean",
            started=True,
            finished=True,
            daemon_start=1722499200000000000,
        )
        proof = idmap.IDMapMaterializationProof(
            token=self.binding.token,
            allocation_id=self.binding.allocation_id,
            compute_id=self.binding.compute_id,
            owner=self.binding.owner,
            project=self.binding.project,
            instance_name=self.binding.instance_name,
            idmap_base=self.binding.idmap_base,
            idmap_size=self.binding.idmap_size,
            storage_driver=self.binding.storage_driver,
            storage_pool=self.binding.storage_pool,
            storage_volume=self.binding.storage_volume,
            rbd_image=self.binding.rbd_image,
            storage_identity=metadata["storage_identity"],
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
            proof, digest=idmap.materialization_proof_digest(proof))
        metadata["proof"] = {
            "outcome": proof.outcome,
            "digest": proof.digest,
        }
        return metadata, proof

    def test_extension_gate_requires_explicit_v1_and_v2(self):
        storage_protocol.require_storage_ownership_extensions(self.client)
        self.client.host_info["api_extensions"] = [
            "storage_release_receipt"]
        self.assertRaises(
            idmap.IDMapConfigurationError,
            storage_protocol.require_storage_ownership_extensions,
            self.client)

    def test_binding_requires_canonical_a_h_t_u(self):
        values = dict(self.binding.__dict__)
        for name in ("token", "allocation_id", "compute_id", "owner"):
            invalid = dict(values)
            invalid[name] = "{%s}" % invalid[name]
            self.assertRaises(
                idmap.IDMapConfigurationError,
                storage_protocol.StorageMaterializationBinding, **invalid)

    def test_register_sends_full_binding_and_requires_pristine_reply(self):
        endpoint = self._materialization_endpoint()
        endpoint.put.return_value = self._response(self._attempt_metadata())
        attempt = self.protocol.register_materialization(self.binding)
        self.assertEqual(self.binding, attempt.binding)
        self.assertTrue(attempt.baseline_clean)
        endpoint.put.assert_called_once_with(
            params={"project": "nova"},
            json={
                "state": "active",
                "allocation_id": self.ALLOCATION_ID,
                "compute_id": self.COMPUTE_ID,
                "owner": self.OWNER,
                "instance_name": "instance-00000001",
                "idmap_base": 1000000,
                "idmap_size": 65536,
                "storage_driver": "ceph",
                "storage_pool": "rootfs",
                "storage_volume": "nova_instance-00000001",
                "rbd_image": "container_nova_instance-00000001",
                "cleanup_disposition": "delete",
            })

    def test_detach_registration_requires_baseline_storage_identity(self):
        binding = dataclasses.replace(
            self.binding, storage_driver="cephext",
            cleanup_disposition="detach")
        endpoint = self.client.api["storage-materialization-attempts"][
            binding.token]
        metadata = self._attempt_metadata(
            storage_driver="cephext", cleanup_disposition="detach")
        endpoint.put.return_value = self._response(metadata)
        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.protocol.register_materialization, binding)
        metadata["storage_identity"] = "rbd_data.1234567890abcdef"
        endpoint.put.return_value = self._response(metadata)
        attempt = self.protocol.register_materialization(binding)
        self.assertEqual(
            "rbd_data.1234567890abcdef", attempt.storage_identity)

    def test_handover_registration_requires_existing_ceph_identity(self):
        binding = dataclasses.replace(
            self.binding, cleanup_disposition="handover")
        endpoint = self.client.api["storage-materialization-attempts"][
            binding.token]
        metadata = self._attempt_metadata(cleanup_disposition="handover")
        endpoint.put.return_value = self._response(metadata)
        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.protocol.register_materialization, binding)
        metadata["storage_identity"] = "rbd_data.1234567890abcdef"
        endpoint.put.return_value = self._response(metadata)
        attempt = self.protocol.register_materialization(binding)
        self.assertEqual("handover", attempt.binding.cleanup_disposition)
        self.assertEqual(
            "rbd_data.1234567890abcdef", attempt.storage_identity)

        for driver in ("zfs", "cephext"):
            self.assertRaises(
                idmap.IDMapConfigurationError,
                storage_protocol.StorageMaterializationBinding,
                **dict(binding.__dict__, storage_driver=driver))

    def test_clean_phase_without_identity_is_accepted(self):
        """A build that failed before materialization has nothing to name."""
        metadata, unused_proof = self._terminal_attempt_metadata(
            storage_identity="")
        self._materialization_endpoint().get.return_value = self._response(
            metadata)

        attempt = self.protocol.get_materialization(self.binding)

        self.assertEqual("clean", attempt.storage_phase)
        self.assertEqual("", attempt.storage_identity)

    def test_materialized_phase_still_requires_an_identity(self):
        endpoint = self._materialization_endpoint()
        metadata = self._attempt_metadata(
            state="committed", storage_phase="materialized",
            storage_identity="", started=True, finished=True,
            daemon_start=1722499200000000000)
        endpoint.get.return_value = self._response(metadata)

        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.protocol.get_materialization, self.binding)

    def test_get_rejects_every_binding_class_mismatch(self):
        endpoint = self._materialization_endpoint()
        for name, value in (
                ("allocation_id", self.OWNER),
                ("compute_id", self.OWNER),
                ("owner", self.COMPUTE_ID),
                ("project", "other"),
                ("instance_name", "instance-00000002"),
                ("idmap_base", 2000000),
                ("idmap_size", 131072),
                ("storage_driver", "zfs"),
                ("storage_pool", "other"),
                ("storage_volume", "nova_instance-00000002"),
                ("rbd_image", "container_other"),
                ("cleanup_disposition", "detach")):
            endpoint.get.return_value = self._response(
                self._attempt_metadata(**{name: value}))
            self.assertRaises(
                idmap.IDMapIntegrityError,
                self.protocol.get_materialization, self.binding)

    def test_get_rejects_missing_false_or_invalid_clean_baseline(self):
        endpoint = self._materialization_endpoint()
        for value in (None, False, "true", 1):
            metadata = self._attempt_metadata()
            if value is None:
                metadata.pop("baseline_clean")
            else:
                metadata["baseline_clean"] = value
            endpoint.get.return_value = self._response(metadata)
            self.assertRaises(
                idmap.IDMapIntegrityError,
                self.protocol.get_materialization, self.binding)

    def test_observe_start_is_a_get_and_requires_durable_boundary(self):
        endpoint = self._materialization_endpoint()
        endpoint.get.return_value = self._response(self._attempt_metadata())
        self.assertRaises(
            idmap.IDMapConflict,
            self.protocol.observe_materialization_start, self.binding)
        endpoint.get.return_value = self._response(self._attempt_metadata(
            state="active", storage_phase="pending", started=True,
            operation_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            daemon_start=1722499200000000000))
        attempt = self.protocol.observe_materialization_start(self.binding)
        self.assertTrue(attempt.started)
        endpoint.get.assert_called_with(params={"project": "nova"})

    def test_abort_and_settle_use_only_server_state_transitions(self):
        endpoint = self._materialization_endpoint()
        aborted = self._attempt_metadata(
            state="aborted", finished=True,
            proof={"outcome": "not-materialized", "digest": ""})
        proof = idmap.IDMapMaterializationProof(
            token=self.binding.token,
            allocation_id=self.binding.allocation_id,
            compute_id=self.binding.compute_id,
            owner=self.binding.owner,
            project=self.binding.project,
            instance_name=self.binding.instance_name,
            idmap_base=self.binding.idmap_base,
            idmap_size=self.binding.idmap_size,
            storage_driver=self.binding.storage_driver,
            storage_pool=self.binding.storage_pool,
            storage_volume=self.binding.storage_volume,
            rbd_image=self.binding.rbd_image,
            storage_identity="",
            baseline_clean=True,
            cleanup_disposition="delete",
            state="aborted",
            storage_phase="none",
            started=False,
            finished=True,
            outcome="not-materialized",
            digest="",
        )
        aborted["proof"]["digest"] = idmap.materialization_proof_digest(proof)
        endpoint.put.return_value = self._response(aborted)
        self.protocol.abort_materialization(self.binding)
        endpoint.put.assert_called_once_with(
            params={"project": "nova"}, json={"state": "aborted"})
        endpoint.put.reset_mock()
        terminal, unused_proof = self._terminal_attempt_metadata()
        endpoint.put.return_value = self._response(terminal)
        result = self.protocol.settle_materialization(self.binding)
        self.assertEqual("reconciled-clean", result.proof.outcome)
        endpoint.put.assert_called_once_with(
            params={"project": "nova"}, json={"state": "settled"})

    def test_materialization_proof_ack_is_exact_and_separate(self):
        metadata, proof = self._terminal_attempt_metadata()
        endpoint = self._materialization_endpoint()
        endpoint.get.return_value = self._response(metadata)
        returned = self.protocol.get_materialization(self.binding).proof
        self.assertEqual(proof, returned)
        self.protocol.acknowledge_materialization_proof(
            self.binding, returned)
        endpoint.delete.assert_called_once_with(params={
            "project": "nova",
            "proof-digest": proof.digest,
            "allocation-id": self.ALLOCATION_ID,
            "compute-id": self.COMPUTE_ID,
            "owner": self.OWNER,
            "instance": "instance-00000001",
            "idmap-base": "1000000",
            "idmap-size": "65536",
        })
        wrong = dataclasses.replace(proof, owner=self.COMPUTE_ID, digest="")
        wrong = dataclasses.replace(
            wrong, digest=idmap.materialization_proof_digest(wrong))
        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.protocol.acknowledge_materialization_proof,
            self.binding, wrong)

    def test_release_receipt_matches_fixed_incus_go_golden(self):
        binding = storage_protocol.StorageMaterializationBinding(
            token=self.TOKEN,
            allocation_id=self.ALLOCATION_ID,
            compute_id=self.COMPUTE_ID,
            owner=self.OWNER,
            project="nova",
            instance_name="instance-00000001",
            idmap_base=1000000,
            idmap_size=65536,
            storage_driver="cephext",
            storage_pool="cinder-bfv",
            storage_volume="nova_instance-00000001",
            cleanup_disposition="detach",
            rbd_image=(
                "volume-11111111-1111-1111-1111-111111111111"),
        )
        metadata = {
            "digest": (
                "sha256:55ec35e696e6e75f855daef4fda1d536a904e296380c29ab"
                "a12fc202032f9429"),
            "token": self.TOKEN,
            "allocation_id": self.ALLOCATION_ID,
            "compute_id": self.COMPUTE_ID,
            "materialization_id": self.TOKEN,
            "owner": self.OWNER,
            "project": "nova",
            "instance_name": "instance-00000001",
            "idmap_base": 1000000,
            "idmap_size": 65536,
            "storage_driver": "cephext",
            "storage_pool": "cinder-bfv",
            "storage_volume": "nova_instance-00000001",
            "rbd_image": (
                "volume-11111111-1111-1111-1111-111111111111"),
            "storage_identity": "rbd_data.1234567890abcdef",
            "baseline_clean": True,
            "cleanup_disposition": "detach",
            "outcome": "normalized",
            "state": "complete",
            "created_at": 1722499200,
            "completed_at": 1722499260,
        }
        endpoint = self._receipt_endpoint(binding)
        endpoint.get.return_value = self._response(metadata)
        receipt = self.protocol.get_release_receipt(binding)
        self.assertEqual(metadata["digest"], receipt.digest)
        params = {
            "project": "nova",
            "rootfs-idmap-release-owner": self.OWNER,
            "rootfs-idmap-allocation-id": self.ALLOCATION_ID,
            "rootfs-idmap-compute-id": self.COMPUTE_ID,
            "instance": "instance-00000001",
            "idmap-base": "1000000",
            "idmap-size": "65536",
        }
        endpoint.get.assert_called_once_with(params=params)
        self.protocol.acknowledge_release_receipt(binding, receipt)
        params["receipt-digest"] = metadata["digest"]
        endpoint.delete.assert_called_once_with(params=params)

    def test_release_receipt_missing_false_or_invalid_v2_fields_fails(self):
        receipt = idmap.IDMapRootfsReleaseReceipt(
            token=self.binding.token,
            allocation_id=self.binding.allocation_id,
            compute_id=self.binding.compute_id,
            materialization_id=self.binding.token,
            owner=self.binding.owner,
            project=self.binding.project,
            instance_name=self.binding.instance_name,
            idmap_base=self.binding.idmap_base,
            idmap_size=self.binding.idmap_size,
            storage_driver="ceph",
            storage_pool=self.binding.storage_pool,
            storage_volume=self.binding.storage_volume,
            rbd_image=self.binding.rbd_image,
            storage_identity="rbd-id",
            baseline_clean=True,
            cleanup_disposition="delete",
            outcome="deleted",
            state="complete",
            digest="",
            created_at=10,
            completed_at=11,
        )
        for changes in (
                {"baseline_clean": False},
                {"cleanup_disposition": "retain"},
                {"cleanup_disposition": "detach"}):
            candidate = dataclasses.replace(receipt, **changes, digest="")
            proof = idmap.IDMapRootfsReleaseProof(**candidate.__dict__)
            candidate = dataclasses.replace(
                candidate,
                digest=idmap.rootfs_release_proof_digest(proof))
            self.assertRaises(
                idmap.IDMapConfigurationError,
                idmap.validate_rootfs_release_receipt, candidate)

        endpoint = self._receipt_endpoint()
        for missing in ("baseline_clean", "cleanup_disposition"):
            metadata = dict(receipt.__dict__)
            metadata.pop(missing)
            endpoint.get.return_value = self._response(metadata)
            self.assertRaises(
                idmap.IDMapIntegrityError,
                self.protocol.get_release_receipt, self.binding)

    def test_release_receipt_cleanup_outcome_matrix(self):
        valid = (
            ("cephext", "detach", "normalized"),
            ("cephext", "detach", "detached"),
            ("ceph", "delete", "deleted"),
            ("ceph", "delete", "detached"),
            ("ceph", "detach", "detached"),
            ("ceph", "handover", "deleted"),
            ("ceph", "handover", "detached"),
            ("dir", "delete", "deleted"),
        )
        for storage_driver, disposition, outcome in valid:
            with self.subTest(
                    storage_driver=storage_driver,
                    disposition=disposition, outcome=outcome):
                receipt = self._release_receipt(
                    storage_driver, disposition, outcome)
                idmap.validate_rootfs_release_receipt(receipt)

        invalid = (
            ("cephext", "detach", "deleted"),
            ("ceph", "delete", "normalized"),
            ("ceph", "detach", "deleted"),
            ("ceph", "detach", "normalized"),
            ("ceph", "handover", "normalized"),
            ("dir", "delete", "detached"),
            ("dir", "delete", "normalized"),
        )
        for storage_driver, disposition, outcome in invalid:
            with self.subTest(
                    storage_driver=storage_driver,
                    disposition=disposition, outcome=outcome):
                receipt = self._release_receipt(
                    storage_driver, disposition, outcome)
                self.assertRaises(
                    idmap.IDMapConfigurationError,
                    idmap.validate_rootfs_release_receipt, receipt)

    def test_handover_target_final_delete_receipt_matches_binding(self):
        binding = dataclasses.replace(
            self.binding, cleanup_disposition="handover")
        receipt = self._release_receipt("ceph", "handover", "deleted")
        endpoint = self._receipt_endpoint(binding)
        endpoint.get.return_value = self._response(dict(receipt.__dict__))

        self.assertEqual(
            receipt, self.protocol.get_release_receipt(binding))
