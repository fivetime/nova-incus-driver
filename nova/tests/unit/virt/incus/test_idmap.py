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

import base64
from concurrent import futures
import dataclasses
import os
import threading
from unittest import mock
import uuid

import fixtures
from nova import test
from oslo_serialization import jsonutils

from nova.virt.incus import idmap


def _decode(value):
    return base64.b64decode(value.encode("ascii"))


class _FakeLease:

    def __init__(self, lease_id, client, ttl):
        self.id = lease_id
        self.client = client
        self._ttl = ttl
        self.refresh_count = 0

    def refresh(self):
        self.refresh_count += 1
        with self.client.lock:
            if self.id not in self.client.leases:
                return -1
        return self._ttl

    def revoke(self):
        self.client.revoke_lease(self.id)
        return True


class _FakeEtcd:
    """Thread-safe subset of the etcd v3 gateway used by allocator tests."""

    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()
        self.fail_get = 0
        self.fail_transaction_before = 0
        self.fail_transaction_after = 0
        self.before_transaction = None
        self.transaction_barrier = None
        self.transaction_barrier_remaining = 0
        self.hook_lock = threading.Lock()
        self.get_calls = []
        self.get_prefix_calls = []
        self.transactions = []
        self.next_lease_id = 1
        self.leases = {}
        self.key_leases = {}

    def lease(self, ttl):
        with self.lock:
            lease = _FakeLease(self.next_lease_id, self, ttl)
            self.next_lease_id += 1
            self.leases[lease.id] = lease
            return lease

    def revoke_lease(self, lease_id):
        with self.lock:
            self.leases.pop(lease_id, None)
            for key, attached in list(self.key_leases.items()):
                if attached == lease_id:
                    self.values.pop(key, None)
                    self.key_leases.pop(key, None)

    def expire_lease(self, lease_id):
        self.revoke_lease(lease_id)

    def get(self, key):
        with self.lock:
            self.get_calls.append(key)
            if self.fail_get:
                self.fail_get -= 1
                raise OSError("read unavailable")
            value = self.values.get(key.encode("utf-8"))
            return [] if value is None else [value]

    def get_prefix(self, key_prefix):
        with self.lock:
            self.get_prefix_calls.append(key_prefix)
            if self.fail_get:
                self.fail_get -= 1
                raise OSError("read unavailable")
            prefix = key_prefix.encode("utf-8")
            return [
                (value, {"key": key})
                for key, value in sorted(self.values.items())
                if key.startswith(prefix)
            ]

    def transaction(self, transaction):
        callback = self.before_transaction
        self.before_transaction = None
        if callback is not None:
            callback(self, transaction)
        barrier = None
        with self.hook_lock:
            if self.transaction_barrier_remaining:
                self.transaction_barrier_remaining -= 1
                barrier = self.transaction_barrier
        if barrier is not None:
            barrier.wait()
        with self.lock:
            self.transactions.append(transaction)
            requests = (
                list(transaction.get("success", [])) +
                list(transaction.get("failure", [])))
            mutating = any(
                "request_put" in request or
                "request_delete_range" in request
                for request in requests)
            if self.fail_transaction_before and mutating:
                self.fail_transaction_before -= 1
                raise OSError("write unavailable")

            succeeded = True
            for comparison in transaction.get("compare", []):
                key = _decode(comparison["key"])
                current = self.values.get(key)
                if comparison["target"] == "CREATE":
                    matches = current is None
                elif comparison["target"] == "VALUE":
                    matches = current == _decode(comparison["value"])
                elif comparison["target"] == "LEASE":
                    matches = self.key_leases.get(key, 0) == int(
                        comparison["lease"])
                else:
                    raise AssertionError("unsupported comparison")
                if comparison["result"] != "EQUAL":
                    raise AssertionError("unsupported comparison result")
                succeeded = succeeded and matches

            branch = "success" if succeeded else "failure"
            responses = []
            for request in transaction.get(branch, []):
                if "request_put" in request:
                    item = request["request_put"]
                    key = _decode(item["key"])
                    self.values[key] = _decode(item["value"])
                    lease_id = item.get("lease")
                    if lease_id is None:
                        self.key_leases.pop(key, None)
                    else:
                        if lease_id not in self.leases:
                            raise AssertionError("put uses unknown lease")
                        self.key_leases[key] = lease_id
                elif "request_delete_range" in request:
                    item = request["request_delete_range"]
                    key = _decode(item["key"])
                    self.values.pop(key, None)
                    self.key_leases.pop(key, None)
                elif "request_range" in request:
                    item = request["request_range"]
                    key = _decode(item["key"])
                    range_end = item.get("range_end")
                    if range_end is not None:
                        end = _decode(range_end)
                        matched = [
                            (stored_key, stored)
                            for stored_key, stored in sorted(
                                self.values.items())
                            if key <= stored_key < end
                        ]
                    else:
                        value = self.values.get(key)
                        matched = (
                            [] if value is None else [(key, value)])
                    if item.get("count_only"):
                        kvs = []
                    else:
                        kvs = [{
                            "key": base64.b64encode(
                                stored_key).decode("ascii"),
                            "lease": str(
                                self.key_leases.get(stored_key, 0)),
                            "value": base64.b64encode(
                                stored).decode("ascii"),
                        } for stored_key, stored in matched]
                    responses.append({
                        "response_range": {
                            "count": str(len(matched)),
                            "kvs": kvs,
                        },
                    })
                else:
                    raise AssertionError("unsupported request")

            if self.fail_transaction_after and mutating:
                self.fail_transaction_after -= 1
                raise OSError("response unavailable")
            return {"succeeded": succeeded, "responses": responses}


class _AuthenticatedFakeEtcd(_FakeEtcd):

    class _GatewayError(Exception):
        def __init__(self, detail_text):
            super().__init__("gateway request failed")
            self.detail_text = detail_text

    class _Session:
        def __init__(self):
            self.headers = {}

    def __init__(self):
        super().__init__()
        self.session = self._Session()
        self.authentication_requests = []
        self.next_get_error = None
        self.next_transaction_error = None
        self.invalid_token_barrier = None

    @staticmethod
    def get_url(path):
        return "https://etcd.example:2379/v3/{}".format(path.lstrip("/"))

    def post(self, url, json, headers=None):
        if headers != {"Authorization": None}:
            raise AssertionError("authentication must omit the shared token")
        self.authentication_requests.append((url, json))
        count = len(self.authentication_requests)
        token = (
            "allocator-token" if count == 1 else
            "allocator-token-%d" % count)
        return {"token": token}

    def get(self, key):
        if (self.invalid_token_barrier is not None and
                self.session.headers.get("Authorization") ==
                "allocator-token"):
            self.invalid_token_barrier.wait()
            raise self._GatewayError(jsonutils.dumps({
                "code": 16,
                "message": "etcdserver: invalid auth token",
            }))
        if self.next_get_error is not None:
            error = self.next_get_error
            self.next_get_error = None
            raise self._GatewayError(error)
        return super().get(key)

    def transaction(self, transaction):
        if self.next_transaction_error is not None:
            error = self.next_transaction_error
            self.next_transaction_error = None
            return error
        return super().transaction(transaction)


class IDMapAllocatorV3Test(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.etcd = _FakeEtcd()
        self.allocator = self._allocator()
        self.allocator.bootstrap()
        self.allocator.run_coordinated_audit(full=True)

    def _allocator(self, client=None, namespace="cell1", count=8):
        return idmap.IDMapAllocator(
            endpoint="http://etcd.example:2379",
            namespace=namespace,
            base=500000000,
            size=65536,
            count=count,
            allow_insecure=True,
            client=client or self.etcd,
        )

    def test_production_requires_namespace_rbac_credentials(self):
        self.assertRaisesRegex(
            idmap.IDMapConfigurationError,
            "username and password file",
            idmap.IDMapAllocator,
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            client=self.etcd,
        )

    def _password_allocator(self, mode):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("correct horse battery staple\n")
        os.chmod(password_file, mode)
        return idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1", base=500000000, size=65536, count=8,
            ca_cert="ca.crt", cert_cert="client.crt", cert_key="client.key",
            username="nova-incus", password_file=password_file,
            client=_AuthenticatedFakeEtcd())

    def test_a_world_readable_password_file_is_refused(self):
        """The credential that guards every ID-map mutation.

        It authorizes the writes that keep two hosts from ever sharing a
        range, so a file any local account can read is an exposure, not
        an untidiness.
        """
        allocator = self._password_allocator(0o644)

        self.assertRaisesRegex(
            idmap.IDMapBackendError, "accessible by other",
            allocator._read_password)

    def test_a_group_readable_password_file_is_accepted(self):
        # nova-compute does not run as root and reads this through its
        # group, so demanding 0600 would reject the supported deployment
        # rather than protect it.
        allocator = self._password_allocator(0o640)

        self.assertEqual(
            "correct horse battery staple", allocator._read_password())

    def test_authenticates_once_before_registry_access(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("correct horse battery staple\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )

        allocator.bootstrap()
        allocator.audit()

        self.assertEqual([(
            "https://etcd.example:2379/v3/auth/authenticate",
            {
                "name": "nova-incus",
                "password": "correct horse battery staple",
            },
        )], etcd.authentication_requests)
        self.assertEqual(
            "allocator-token", etcd.session.headers["Authorization"])

    def test_reauthenticates_once_after_gateway_token_is_invalid(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("first-password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("rotated-password\n")
        os.chmod(password_file, 0o600)
        etcd.next_get_error = jsonutils.dumps({
            "code": 16,
            "message": "etcdserver: invalid auth token",
        })

        self.assertIsNotNone(allocator._get_raw(allocator.configuration_key))

        self.assertEqual(2, len(etcd.authentication_requests))
        self.assertEqual(
            "rotated-password",
            etcd.authentication_requests[1][1]["password"])
        self.assertEqual(
            "allocator-token-2", etcd.session.headers["Authorization"])

    def test_reauthenticates_after_transaction_returns_invalid_token(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        etcd.next_transaction_error = {
            "code": 16,
            "message": "etcdserver: invalid auth token",
        }

        self.assertIsNone(
            allocator.get("00000000-0000-0000-0000-000000000001"))

        self.assertEqual(2, len(etcd.authentication_requests))
        self.assertEqual(
            "allocator-token-2", etcd.session.headers["Authorization"])

    def test_reauthenticates_after_transaction_returns_code_16(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        etcd.next_transaction_error = {
            "code": 16,
            "message": "etcdserver: user name is empty",
        }

        self.assertIsNone(
            allocator.get("00000000-0000-0000-0000-000000000002"))

        self.assertEqual(2, len(etcd.authentication_requests))
        self.assertEqual(
            "allocator-token-2", etcd.session.headers["Authorization"])

    def test_reauthenticates_after_transaction_returns_empty_body(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        etcd.next_transaction_error = {}

        self.assertIsNone(
            allocator.get("00000000-0000-0000-0000-000000000003"))

        self.assertEqual(2, len(etcd.authentication_requests))
        self.assertEqual(
            "allocator-token-2", etcd.session.headers["Authorization"])

    def test_concurrent_invalid_token_refresh_authenticates_once(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        etcd.invalid_token_barrier = threading.Barrier(16)

        with futures.ThreadPoolExecutor(max_workers=16) as executor:
            reads = [
                executor.submit(
                    allocator._get_raw, allocator.configuration_key)
                for unused_index in range(16)
            ]
            for read in reads:
                self.assertIsNotNone(read.result())

        self.assertEqual(2, len(etcd.authentication_requests))
        self.assertEqual(
            "allocator-token-2", etcd.session.headers["Authorization"])

    def test_permission_denied_does_not_reauthenticate(self):
        directory = self.useFixture(fixtures.TempDir()).path
        password_file = os.path.join(directory, "etcd-password")
        with open(password_file, "w", encoding="utf-8") as stream:
            stream.write("password\n")
        os.chmod(password_file, 0o600)
        etcd = _AuthenticatedFakeEtcd()
        allocator = idmap.IDMapAllocator(
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file=password_file,
            client=etcd,
        )
        allocator.bootstrap()
        etcd.next_get_error = jsonutils.dumps({
            "code": 7,
            "message": "etcdserver: permission denied",
        })

        self.assertRaisesRegex(
            idmap.IDMapBackendError, "permission denied",
            allocator._get_raw, allocator.configuration_key)
        self.assertEqual(1, len(etcd.authentication_requests))

    def test_production_rejects_relative_password_file(self):
        self.assertRaisesRegex(
            idmap.IDMapConfigurationError,
            "password file path must be absolute",
            idmap.IDMapAllocator,
            endpoint="https://etcd.example:2379",
            namespace="cell1",
            base=500000000,
            size=65536,
            count=8,
            ca_cert="ca.crt",
            cert_cert="client.crt",
            cert_key="client.key",
            username="nova-incus",
            password_file="relative-password",
            client=self.etcd,
        )

    @staticmethod
    def _uuid(number):
        return str(uuid.UUID(int=number))

    @staticmethod
    def _host(number):
        return str(uuid.UUID(int=10000 + number))

    @staticmethod
    def _materialization(number):
        return str(uuid.UUID(int=20000 + number))

    def _claim(self, assignment, host_number=1, token_number=1):
        host_id = self._host(host_number)
        token = self._materialization(token_number)
        assignment = self.allocator.claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        return assignment, host_id, token

    def _materialization_proof(self, assignment, host_id, token,
                               outcome="reconciled-clean",
                               driver="ceph", disposition="delete"):
        never_started = outcome == "not-materialized"
        proof = idmap.IDMapMaterializationProof(
            token=token,
            allocation_id=assignment.allocation_id,
            compute_id=host_id,
            owner=assignment.instance_uuid,
            project="nova",
            instance_name="instance-00000001",
            idmap_base=assignment.base,
            idmap_size=assignment.size,
            storage_driver=driver,
            storage_pool="root-pool",
            storage_volume="nova_instance-00000001",
            rbd_image="container_nova_instance-00000001",
            storage_identity="" if never_started else "rbd-image-id",
            baseline_clean=True,
            cleanup_disposition=disposition,
            state="aborted" if never_started else "clean",
            storage_phase="none" if never_started else "clean",
            started=not never_started,
            finished=True,
            outcome=outcome,
            digest="",
        )
        return dataclasses.replace(
            proof, digest=self.allocator._materialization_digest(proof))

    def _receipt(self, assignment, host_id, token, outcome="deleted",
                 driver="ceph"):
        disposition = "delete" if outcome == "deleted" else "detach"
        receipt = idmap.IDMapRootfsReleaseReceipt(
            token=token,
            allocation_id=assignment.allocation_id,
            compute_id=host_id,
            materialization_id=token,
            owner=assignment.instance_uuid,
            project="nova",
            instance_name="instance-00000001",
            idmap_base=assignment.base,
            idmap_size=assignment.size,
            storage_driver=driver,
            storage_pool="root-pool",
            storage_volume="nova_instance-00000001",
            storage_identity=(
                "rbd-image-id" if driver in ("ceph", "cephext") else ""),
            rbd_image="volume-%s" % assignment.instance_uuid,
            baseline_clean=True,
            cleanup_disposition=disposition,
            outcome=outcome,
            state="complete",
            digest="",
            created_at=100,
            completed_at=101,
        )
        proof = idmap.IDMapRootfsReleaseProof(**receipt.__dict__)
        return dataclasses.replace(
            receipt, digest=self.allocator._release_digest(proof))

    def _clean_and_retire(self, assignment, host_id, token,
                          proof_kind="attempt"):
        claim = self.allocator.get_host_claim(
            assignment.instance_uuid, host_id)
        if claim.state == "unmaterialized":
            claim = self.allocator.mark_materialization_possible(
                assignment.instance_uuid, host_id, token,
                assignment=assignment)
        if proof_kind == "attempt":
            proof = self._materialization_proof(
                assignment, host_id, token)
            self.allocator.record_materialization_proof(
                assignment.instance_uuid, host_id, token, proof,
                assignment=assignment)
        else:
            if claim.state == "possible":
                self.allocator.mark_materialization_committed(
                    assignment.instance_uuid, host_id, token,
                    assignment=assignment)
            receipt = self._receipt(assignment, host_id, token)
            self.allocator.record_rootfs_release_proof(
                assignment.instance_uuid, host_id, token, receipt,
                assignment=assignment)
        return self.allocator.retire_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)

    def test_schema_and_keyspace_are_explicit_v3(self):
        config = jsonutils.loads(
            self.etcd.values[
                self.allocator.configuration_key.encode("utf-8")])
        self.assertEqual(3, config["schema"])
        self.assertIn("/idmaps/v3/", self.allocator.configuration_key)
        self.assertNotIn("/idmaps/v2/", self.allocator.configuration_key)

    def test_digests_match_incus_go_canonical_structs(self):
        release = idmap.IDMapRootfsReleaseProof(
            token="06ada2e7-67f9-4c4e-b071-da45f25cfc67",
            allocation_id="22222222-2222-2222-2222-222222222222",
            compute_id="33333333-3333-3333-3333-333333333333",
            materialization_id="06ada2e7-67f9-4c4e-b071-da45f25cfc67",
            owner="11111111-1111-1111-1111-111111111111",
            project="nova", instance_name="instance-00000001",
            idmap_base=1000000, idmap_size=65536,
            storage_driver="cephext", storage_pool="cinder-bfv",
            storage_volume="nova_instance-00000001",
            rbd_image=(
                "volume-11111111-1111-1111-1111-111111111111"),
            storage_identity="rbd_data.1234567890abcdef",
            baseline_clean=True, cleanup_disposition="detach",
            outcome="normalized", state="complete", digest="",
            created_at=1722499200, completed_at=1722499260)
        self.assertEqual(
            "sha256:55ec35e696e6e75f855daef4fda1d536a904e296380c29ab"
            "a12fc202032f9429",
            idmap.rootfs_release_proof_digest(release))

        attempt = idmap.IDMapMaterializationProof(
            token=self._uuid(20001), allocation_id=self._uuid(30001),
            compute_id=self._uuid(10001), owner=self._uuid(1),
            project="nova", instance_name="instance-00000001",
            idmap_base=500000000, idmap_size=65536,
            storage_driver="ceph", storage_pool="root-pool",
            storage_volume="nova_instance-00000001",
            rbd_image="container-test", storage_identity="rbd-image-id",
            baseline_clean=True,
            cleanup_disposition="detach", state="clean",
            storage_phase="clean", started=True, finished=True,
            outcome="reconciled-clean", digest="")
        self.assertEqual(
            "sha256:e20aa303e23d6f9c7a7291d925e27be71b8ae40948dc2f26"
            "c211d4a7e5589f09",
            idmap.materialization_proof_digest(attempt))

    def test_runtime_does_not_bootstrap_or_upgrade_v2(self):
        etcd = _FakeEtcd()
        etcd.values[b"/openstack-incus/idmaps/v2/cell1/config"] = b"{}"
        allocator = self._allocator(client=etcd)
        self.assertRaises(idmap.IDMapIntegrityError, allocator.initialize)
        self.assertNotIn(allocator.configuration_key.encode(), etcd.values)

    def test_allocate_is_bidirectional_and_auditable(self):
        assignment = self.allocator.allocate(self._uuid(1))
        self.assertEqual(assignment, self.allocator.get(self._uuid(1)))
        self.assertEqual([assignment], self.allocator.audit())
        self.assertEqual((), assignment.host_ids)
        self.assertEqual(
            self.etcd.values[self.allocator.instance_key(
                assignment.instance_uuid).encode()],
            self.etcd.values[self.allocator.slot_key(
                assignment.slot).encode()])

    def test_assignment_has_no_global_materialization_state(self):
        assignment = self.allocator.allocate(self._uuid(2))
        self.assertNotIn("rootfs_materialized", assignment.__dict__)
        raw = self.etcd.values[
            self.allocator.instance_key(assignment.instance_uuid).encode()]
        self.assertNotIn("rootfs_materialized", jsonutils.loads(raw))

    def test_same_materialization_claim_is_idempotent(self):
        assignment = self.allocator.allocate(self._uuid(3))
        first, host_id, token = self._claim(assignment)
        second = self.allocator.claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual(first, second)
        claim = self.allocator.get_host_claim(
            assignment.instance_uuid, host_id)
        self.assertEqual(token, claim.materialization_id)
        self.assertEqual("unmaterialized", claim.state)
        self.assertIsNone(claim.proof)

    def test_different_materialization_on_same_host_conflicts(self):
        assignment = self.allocator.allocate(self._uuid(4))
        assignment, host_id, unused_token = self._claim(assignment)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.claim,
            assignment.instance_uuid, host_id, self._materialization(2),
            assignment=assignment)

    def test_cleaned_materialization_is_atomically_replaced(self):
        assignment = self.allocator.allocate(self._uuid(40))
        assignment, host_id, old_token = self._claim(assignment)
        proof = self._materialization_proof(
            assignment, host_id, old_token, outcome="not-materialized")
        self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, old_token, proof,
            assignment=assignment)

        new_token = self._materialization(40)
        replaced = self.allocator.claim(
            assignment.instance_uuid, host_id, new_token,
            assignment=assignment)

        self.assertEqual((host_id,), replaced.host_ids)
        claim = self.allocator.get_host_claim(
            assignment.instance_uuid, host_id)
        self.assertEqual(new_token, claim.materialization_id)
        self.assertEqual("unmaterialized", claim.state)
        self.assertIsNone(claim.proof)

    def test_cleaned_replace_and_retire_race_preserves_new_claim_50_rounds(
            self):
        assignment = self.allocator.allocate(self._uuid(41))
        assignment, host_id, old_token = self._claim(assignment)

        for index in range(50):
            proof = self._materialization_proof(
                assignment, host_id, old_token,
                outcome="not-materialized")
            self.allocator.record_materialization_proof(
                assignment.instance_uuid, host_id, old_token, proof,
                assignment=assignment)
            new_token = self._materialization(100 + index)
            self.etcd.transaction_barrier = threading.Barrier(2)
            self.etcd.transaction_barrier_remaining = 2

            with futures.ThreadPoolExecutor(max_workers=2) as executor:
                replace_future = executor.submit(
                    self.allocator.claim, assignment.instance_uuid,
                    host_id, new_token, assignment)
                retire_future = executor.submit(
                    self.allocator.retire_claim,
                    assignment.instance_uuid, host_id, old_token,
                    assignment)
                replaced = replace_future.result()
                try:
                    retire_future.result()
                except idmap.IDMapConflict:
                    # Replacement linearized first; exact-token retirement
                    # must fail rather than deleting the new claim.
                    pass

            assignment = self.allocator.get(assignment.instance_uuid)
            claim = self.allocator.get_host_claim(
                assignment.instance_uuid, host_id)
            self.assertEqual((host_id,), assignment.host_ids)
            self.assertEqual(new_token, claim.materialization_id)
            self.assertEqual("unmaterialized", claim.state)
            self.assertIsNone(claim.proof)
            self.assertEqual(assignment, replaced)
            old_token = new_token

    def test_mark_possible_is_one_way_and_idempotent(self):
        assignment = self.allocator.allocate(self._uuid(5))
        assignment, host_id, token = self._claim(assignment)
        first = self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        second = self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual(first, second)
        self.assertEqual("possible", first.state)

    def test_mark_possible_lost_ack_is_verified(self):
        assignment = self.allocator.allocate(self._uuid(6))
        assignment, host_id, token = self._claim(assignment)
        self.etcd.fail_transaction_after = 1
        claim = self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual("possible", claim.state)

    def test_mark_committed_is_one_way_idempotent_and_lost_ack_safe(self):
        assignment = self.allocator.allocate(self._uuid(61))
        assignment, host_id, token = self._claim(
            assignment, host_number=61, token_number=61)
        self.assertRaises(
            idmap.IDMapConflict,
            self.allocator.mark_materialization_committed,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.etcd.fail_transaction_after = 1
        first = self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        second = self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual(first, second)
        self.assertEqual("committed", first.state)

    def test_commit_truth_wins_after_release_intent_race(self):
        assignment = self.allocator.allocate(self._uuid(62))
        assignment, host_id, token = self._claim(
            assignment, host_number=62, token_number=62)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.request_release(
            assignment.instance_uuid, "instance-00000001",
            assignment=assignment)
        claim = self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual("committed", claim.state)

    def test_release_intent_blocks_new_claim_and_possible(self):
        first = self.allocator.allocate(self._uuid(7))
        first, host_id, token = self._claim(first)
        self.allocator.request_release(
            first.instance_uuid, "instance-00000001", assignment=first)
        self.assertRaises(
            idmap.IDMapConflict,
            self.allocator.mark_materialization_possible,
            first.instance_uuid, host_id, token, assignment=first)

        second = self.allocator.allocate(self._uuid(8))
        self.allocator.request_release(
            second.instance_uuid, "instance-00000002", assignment=second)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.claim,
            second.instance_uuid, self._host(2), self._materialization(2),
            assignment=second)

    def test_claim_and_release_race_has_one_linearizable_winner(self):
        assignment = self.allocator.allocate(self._uuid(9))
        barrier = threading.Barrier(2)
        self.etcd.transaction_barrier = barrier
        self.etcd.transaction_barrier_remaining = 2

        with futures.ThreadPoolExecutor(max_workers=2) as executor:
            claim_future = executor.submit(
                self.allocator.claim, assignment.instance_uuid,
                self._host(1), self._materialization(1), assignment)
            release_future = executor.submit(
                self.allocator.request_release, assignment.instance_uuid,
                "instance-00000001", assignment)
            results = []
            for future in (claim_future, release_future):
                try:
                    results.append(future.result())
                except idmap.IDMapConflict:
                    results.append(None)
        current = self.allocator.get(assignment.instance_uuid)
        intent = self.allocator.get_release_intent(assignment.instance_uuid)
        self.assertIsNotNone(intent)
        self.assertIsNotNone(results[1])
        # If the claim linearized first, both calls succeed and the release
        # barrier preserves that existing claim. If release won, claim fails.
        self.assertEqual(results[0] is not None, bool(current.host_ids))

    def test_ensure_initialized_caches_config_validation_briefly(self):
        # Every ownership-changing transaction still carries its own
        # _compare_config() CAS; the per-operation config re-read is a
        # read-side check whose 5s reuse removes a hot-path etcd round trip.
        calls = []
        orig = self.allocator._get_raw

        def spy(key):
            calls.append(key)
            return orig(key)

        with mock.patch.object(self.allocator, '_get_raw', side_effect=spy):
            self.allocator._ensure_initialized()
            self.allocator._ensure_initialized()
            config_reads = [
                k for k in calls if k == self.allocator.configuration_key]
            self.assertEqual(1, len(config_reads))
            # An expired window revalidates.
            self.allocator._initialized_checked_at -= 60
            self.allocator._ensure_initialized()
            config_reads = [
                k for k in calls if k == self.allocator.configuration_key]
            self.assertEqual(2, len(config_reads))

    @staticmethod
    def _fence_proof(assignment, host_id):
        return idmap.IDMapFenceProof(
            instance_uuid=assignment.instance_uuid,
            host_id=host_id,
            allocation_id=assignment.allocation_id,
            fence_agent="fence_virsh",
            fenced_at="2026-08-07T00:00:00Z",
            operator="ops@example.com",
            evidence="virsh destroy incus-node-02 rc=0")

    def test_fence_retires_a_committed_claim_of_a_dead_host(self):
        """Evacuation's unblocking primitive.

        A host that STONITH powered off can never produce a release
        receipt, so its committed claim blocked every rescheduled spawn of
        the generation. External fence evidence disposes of it.
        """
        assignment = self.allocator.allocate(self._uuid(300))
        assignment, host_id, token = self._claim(
            assignment, host_number=300, token_number=300)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token, assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token, assignment=assignment)

        current = self.allocator.fence_retire_claim(
            assignment.instance_uuid, host_id,
            self._fence_proof(assignment, host_id), assignment=assignment)

        self.assertNotIn(host_id, current.host_ids)
        self.assertIsNone(self.allocator.get_host_claim(
            assignment.instance_uuid, host_id))

    def test_fence_retirement_is_recorded_in_the_audit_ledger(self):
        assignment = self.allocator.allocate(self._uuid(301))
        assignment, host_id, token = self._claim(
            assignment, host_number=301, token_number=301)
        proof = self._fence_proof(assignment, host_id)

        self.allocator.fence_retire_claim(
            assignment.instance_uuid, host_id, proof, assignment=assignment)

        recorded = self.allocator.get_fence_proof(
            assignment.instance_uuid, host_id)
        self.assertEqual(proof, recorded)
        # The ledger entry is a first-class allocator record: the full
        # audit scan must accept it, not report an unexpected key.
        assignments, intents, unused_claims = self.allocator.audit_state()
        self.assertIn(
            assignment.instance_uuid,
            [a.instance_uuid for a in assignments])

    def test_audit_rejects_fence_entry_beside_the_claim_it_disposed(self):
        # fence_retire_claim deletes the claim in the same transaction
        # that writes the ledger, so both records for one generation can
        # only mean outside mutation.
        assignment = self.allocator.allocate(self._uuid(306))
        assignment, host_id, token = self._claim(
            assignment, host_number=306, token_number=306)
        proof = self._fence_proof(assignment, host_id)
        raw = self.allocator._fence_proof_raw(proof, token)
        key = self.allocator.fence_key(host_id, assignment.instance_uuid)
        self.etcd.values[key.encode("utf-8")] = raw

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "the live host claim it disposed of",
            self.allocator.audit_state)

    def test_audit_accepts_a_ledger_entry_written_before_tokens(self):
        """A schema addition must not latch an existing registry.

        Entries written before the disposed token was recorded cannot
        answer the coexistence question, so they stay out of it rather
        than failing every allocator operation on every compute.
        """
        assignment = self.allocator.allocate(self._uuid(308))
        assignment, host_id, token = self._claim(
            assignment, host_number=308, token_number=308)
        proof = self._fence_proof(assignment, host_id)
        legacy = jsonutils.loads(
            self.allocator._fence_proof_raw(proof, token).decode("utf-8"))
        del legacy["materialization_id"]
        key = self.allocator.fence_key(host_id, assignment.instance_uuid)
        self.etcd.values[key.encode("utf-8")] = jsonutils.dumps(
            legacy, sort_keys=True).encode("utf-8")

        assignments, unused_intents, unused_claims = (
            self.allocator.audit_state())

        self.assertIn(
            assignment.instance_uuid,
            [a.instance_uuid for a in assignments])

    def test_instance_may_return_to_a_host_it_was_fenced_off(self):
        """A repaired host must be able to host the instance again.

        The ledger entry is permanent audit evidence, so a later
        generation's claim necessarily sits beside it. Treating that as
        corruption would latch every allocator operation fleet-wide the
        first time an instance came home.
        """
        assignment = self.allocator.allocate(self._uuid(307))
        assignment, host_id, token = self._claim(
            assignment, host_number=307, token_number=307)
        assignment = self.allocator.fence_retire_claim(
            assignment.instance_uuid, host_id,
            self._fence_proof(assignment, host_id), assignment=assignment)

        # A new materialization on the same host is a new generation.
        current = self.allocator.claim(
            assignment.instance_uuid, host_id, self._materialization(317),
            assignment=assignment)

        self.assertIn(host_id, current.host_ids)
        assignments, unused_intents, unused_claims = (
            self.allocator.audit_state())
        self.assertIn(
            assignment.instance_uuid,
            [a.instance_uuid for a in assignments])
        # The disposal stays auditable after the reclaim.
        self.assertIsNotNone(self.allocator.get_fence_proof(
            assignment.instance_uuid, host_id))

    def test_fence_retirement_refuses_a_cleaned_claim(self):
        # A claim with its own cleanup proof must retire through the
        # ordinary path; fence evidence is only for hosts that cannot
        # produce one.
        assignment = self.allocator.allocate(self._uuid(302))
        assignment, host_id, token = self._claim(
            assignment, host_number=302, token_number=302)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token, assignment=assignment)
        proof = self._materialization_proof(assignment, host_id, token)
        self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)

        self.assertRaisesRegex(
            idmap.IDMapConflict, "cleanup proof",
            self.allocator.fence_retire_claim,
            assignment.instance_uuid, host_id,
            self._fence_proof(assignment, host_id), assignment=assignment)

    def test_fence_retirement_rejects_mismatched_evidence(self):
        assignment = self.allocator.allocate(self._uuid(303))
        assignment, host_id, token = self._claim(
            assignment, host_number=303, token_number=303)

        for overrides, expected in (
                ({"host_id": self._host(399)}, "another host"),
                ({"instance_uuid": self._uuid(399)}, "another instance"),
                ({"allocation_id": self._uuid(398)},
                 "another allocation generation"),
                ({"evidence": "  "}, "field evidence is missing")):
            values = dict(
                instance_uuid=assignment.instance_uuid,
                host_id=host_id,
                allocation_id=assignment.allocation_id,
                fence_agent="fence_virsh",
                fenced_at="2026-08-07T00:00:00Z",
                operator="ops@example.com",
                evidence="virsh destroy rc=0")
            values.update(overrides)
            self.assertRaises(
                idmap.IDMapError,
                self.allocator.fence_retire_claim,
                assignment.instance_uuid, host_id,
                idmap.IDMapFenceProof(**values),
                assignment=assignment)
        # The claim survives every rejected attempt.
        self.assertIsNotNone(self.allocator.get_host_claim(
            assignment.instance_uuid, host_id))

    def test_fence_retired_generation_can_be_reclaimed_elsewhere(self):
        """The evacuation destination can claim the freed generation."""
        assignment = self.allocator.allocate(self._uuid(304))
        assignment, dead_host, token = self._claim(
            assignment, host_number=304, token_number=304)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, dead_host, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, dead_host, token,
            assignment=assignment)
        assignment = self.allocator.fence_retire_claim(
            assignment.instance_uuid, dead_host,
            self._fence_proof(assignment, dead_host), assignment=assignment)

        survivor = self._host(305)
        current = self.allocator.claim(
            assignment.instance_uuid, survivor, self._materialization(305),
            assignment=assignment)

        self.assertIn(survivor, current.host_ids)

    def test_unproven_claim_cannot_retire(self):
        assignment = self.allocator.allocate(self._uuid(10))
        assignment, host_id, token = self._claim(assignment)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.retire_claim,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)

    def test_abandon_removes_unmaterialized_claim_without_proof(self):
        assignment = self.allocator.allocate(self._uuid(210))
        assignment, host_id, token = self._claim(
            assignment, host_number=210, token_number=210)
        current = self.allocator.abandon_unregistered_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertNotIn(host_id, current.host_ids)
        self.assertIsNone(self.allocator.get_host_claim(
            assignment.instance_uuid, host_id))

    def test_abandon_rejects_possible_claim(self):
        assignment = self.allocator.allocate(self._uuid(211))
        assignment, host_id, token = self._claim(
            assignment, host_number=211, token_number=211)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertRaisesRegex(
            idmap.IDMapConflict, "unmaterialized",
            self.allocator.abandon_unregistered_claim,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)

    def test_abandon_rejects_another_materialization_token(self):
        assignment = self.allocator.allocate(self._uuid(212))
        assignment, host_id, token = self._claim(
            assignment, host_number=212, token_number=212)
        self.assertRaisesRegex(
            idmap.IDMapConflict, "another materialization",
            self.allocator.abandon_unregistered_claim,
            assignment.instance_uuid, host_id,
            self._materialization(213),
            assignment=assignment)

    def test_abandon_is_idempotent_when_claim_is_gone(self):
        assignment = self.allocator.allocate(self._uuid(214))
        assignment, host_id, token = self._claim(
            assignment, host_number=214, token_number=214)
        self.allocator.abandon_unregistered_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        current = self.allocator.abandon_unregistered_claim(
            assignment.instance_uuid, host_id, token)
        self.assertNotIn(host_id, current.host_ids)

    def test_abandoned_generation_can_release(self):
        assignment = self.allocator.allocate(self._uuid(215))
        assignment, host_id, token = self._claim(
            assignment, host_number=215, token_number=215)
        self.allocator.abandon_unregistered_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        intent = self.allocator.request_release(
            assignment.instance_uuid, 'instance-abandon215')
        self.assertTrue(self.allocator.release(intent))
        self.assertIsNone(self.allocator.get(assignment.instance_uuid))

    def test_exact_not_materialized_proof_cleans_unmaterialized_claim(self):
        assignment = self.allocator.allocate(self._uuid(11))
        assignment, host_id, token = self._claim(assignment)
        proof = self._materialization_proof(
            assignment, host_id, token, outcome="not-materialized")
        claim = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.assertEqual("cleaned", claim.state)
        self.assertEqual(proof, claim.proof)

    def test_not_materialized_proof_rejects_possible_claim(self):
        assignment = self.allocator.allocate(self._uuid(111))
        assignment, host_id, token = self._claim(
            assignment, host_number=111, token_number=111)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        proof = self._materialization_proof(
            assignment, host_id, token, outcome="not-materialized")
        self.assertRaises(
            idmap.IDMapConflict,
            self.allocator.record_materialization_proof,
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)

    def test_reconciled_clean_proof_requires_possible_claim(self):
        assignment = self.allocator.allocate(self._uuid(112))
        assignment, host_id, token = self._claim(
            assignment, host_number=112, token_number=112)
        proof = self._materialization_proof(
            assignment, host_id, token)
        self.assertRaises(
            idmap.IDMapConflict,
            self.allocator.record_materialization_proof,
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        claim = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.assertEqual("cleaned", claim.state)

    def test_materialization_proof_requires_exact_digest_and_binding(self):
        assignment = self.allocator.allocate(self._uuid(12))
        assignment, host_id, token = self._claim(assignment)
        proof = self._materialization_proof(
            assignment, host_id, token)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertRaises(
            idmap.IDMapConfigurationError,
            self.allocator.record_materialization_proof,
            assignment.instance_uuid, host_id, token,
            dataclasses.replace(proof, digest="sha256:%s" % ("f" * 64)),
            assignment=assignment)
        wrong = dataclasses.replace(proof, compute_id=self._host(2))
        wrong = dataclasses.replace(
            wrong, digest=self.allocator._materialization_digest(wrong))
        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.allocator.record_materialization_proof,
            assignment.instance_uuid, host_id, token, wrong,
            assignment=assignment)

    def test_materialization_proof_requires_clean_baseline(self):
        assignment = self.allocator.allocate(self._uuid(120))
        assignment, host_id, token = self._claim(
            assignment, host_number=120, token_number=120)
        proof = self._materialization_proof(
            assignment, host_id, token)
        unproven = dataclasses.replace(proof, baseline_clean=False, digest="")
        unproven = dataclasses.replace(
            unproven,
            digest=self.allocator._materialization_digest(unproven))
        self.assertRaises(
            idmap.IDMapConfigurationError,
            self.allocator.record_materialization_proof,
            assignment.instance_uuid, host_id, token, unproven,
            assignment=assignment)

    def test_cleanup_proof_lost_ack_is_replayed_idempotently(self):
        assignment = self.allocator.allocate(self._uuid(13))
        assignment, host_id, token = self._claim(assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        proof = self._materialization_proof(
            assignment, host_id, token)
        self.etcd.fail_transaction_after = 1
        first = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        second = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.assertEqual(first, second)

    def test_release_receipt_deleted_cleans_claim(self):
        assignment = self.allocator.allocate(self._uuid(14))
        assignment, host_id, token = self._claim(assignment)
        receipt = self._receipt(assignment, host_id, token)
        self.assertRaises(
            idmap.IDMapConflict,
            self.allocator.record_rootfs_release_proof,
            assignment.instance_uuid, host_id, token, receipt,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        claim = self.allocator.record_rootfs_release_proof(
            assignment.instance_uuid, host_id, token, receipt,
            assignment=assignment)
        self.assertEqual("cleaned", claim.state)
        self.assertIsInstance(claim.proof, idmap.IDMapRootfsReleaseProof)

    def test_release_receipt_normalized_and_detached_require_identity(self):
        for number, outcome, driver in (
                (15, "normalized", "cephext"),
                (16, "detached", "ceph"),
                (17, "detached", "cephext")):
            assignment = self.allocator.allocate(self._uuid(number))
            assignment, host_id, token = self._claim(
                assignment, host_number=number, token_number=number)
            self.allocator.mark_materialization_possible(
                assignment.instance_uuid, host_id, token,
                assignment=assignment)
            self.allocator.mark_materialization_committed(
                assignment.instance_uuid, host_id, token,
                assignment=assignment)
            receipt = self._receipt(
                assignment, host_id, token, outcome=outcome, driver=driver)
            claim = self.allocator.record_rootfs_release_proof(
                assignment.instance_uuid, host_id, token, receipt,
                assignment=assignment)
            self.assertEqual(outcome, claim.proof.outcome)

        assignment = self.allocator.allocate(self._uuid(18))
        assignment, host_id, token = self._claim(
            assignment, host_number=18, token_number=18)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        receipt = self._receipt(
            assignment, host_id, token, outcome="detached")
        invalid = dataclasses.replace(receipt, storage_identity="")
        invalid_proof = idmap.IDMapRootfsReleaseProof(**invalid.__dict__)
        invalid = dataclasses.replace(
            invalid, digest=self.allocator._release_digest(invalid_proof))
        self.assertRaises(
            idmap.IDMapConfigurationError,
            self.allocator.record_rootfs_release_proof,
            assignment.instance_uuid, host_id, token, invalid,
            assignment=assignment)

    def test_release_receipt_requires_clean_baseline_and_disposition(self):
        assignment = self.allocator.allocate(self._uuid(180))
        assignment, host_id, token = self._claim(
            assignment, host_number=180, token_number=180)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        receipt = self._receipt(assignment, host_id, token)
        for changes in (
                {"baseline_clean": False},
                {"cleanup_disposition": "retain"},
                {"cleanup_disposition": "detach"}):
            invalid = dataclasses.replace(receipt, **changes, digest="")
            proof = idmap.IDMapRootfsReleaseProof(**invalid.__dict__)
            invalid = dataclasses.replace(
                invalid, digest=idmap.rootfs_release_proof_digest(proof))
            self.assertRaises(
                idmap.IDMapConfigurationError,
                self.allocator.record_rootfs_release_proof,
                assignment.instance_uuid, host_id, token, invalid,
                assignment=assignment)

    def test_detach_abort_before_start_retains_baseline_identity(self):
        assignment = self.allocator.allocate(self._uuid(181))
        assignment, host_id, token = self._claim(
            assignment, host_number=181, token_number=181)
        proof = self._materialization_proof(
            assignment, host_id, token, outcome="not-materialized",
            driver="cephext", disposition="detach")
        proof = dataclasses.replace(
            proof, storage_identity="rbd-image-id", digest="")
        proof = dataclasses.replace(
            proof, digest=idmap.materialization_proof_digest(proof))
        claim = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.assertEqual("cleaned", claim.state)

    def test_release_receipt_requires_exact_a_h_t_u(self):
        assignment = self.allocator.allocate(self._uuid(19))
        assignment, host_id, token = self._claim(assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        receipt = self._receipt(assignment, host_id, token)
        wrong = dataclasses.replace(receipt, compute_id=self._host(2))
        wrong_proof = idmap.IDMapRootfsReleaseProof(**wrong.__dict__)
        wrong = dataclasses.replace(
            wrong, digest=self.allocator._release_digest(wrong_proof))
        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.allocator.record_rootfs_release_proof,
            assignment.instance_uuid, host_id, token, wrong,
            assignment=assignment)

    def test_retire_lost_ack_is_idempotent(self):
        assignment = self.allocator.allocate(self._uuid(20))
        assignment, host_id, token = self._claim(assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        proof = self._materialization_proof(
            assignment, host_id, token)
        self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)
        self.etcd.fail_transaction_after = 1
        retired = self.allocator.retire_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        repeated = self.allocator.retire_claim(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual(retired, repeated)
        self.assertEqual((), retired.host_ids)

    def test_final_release_requires_every_claim_retired(self):
        assignment = self.allocator.allocate(self._uuid(21))
        assignment, first_host, first_token = self._claim(
            assignment, host_number=1, token_number=1)
        assignment, second_host, second_token = self._claim(
            assignment, host_number=2, token_number=2)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, first_host, first_token,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, second_host, second_token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, second_host, second_token,
            assignment=assignment)
        intent = self.allocator.request_release(
            assignment.instance_uuid, "instance-00000001",
            assignment=assignment)
        self.assertFalse(self.allocator.release(intent))
        assignment = self._clean_and_retire(
            assignment, first_host, first_token)
        self.assertFalse(self.allocator.release(intent))
        assignment = self._clean_and_retire(
            assignment, second_host, second_token, proof_kind="receipt")
        self.assertTrue(self.allocator.release(intent))
        self.assertTrue(self.allocator.release(intent))

    def test_start_requires_exact_possible_claim_and_no_release(self):
        assignment = self.allocator.allocate(self._uuid(22))
        assignment, host_id, token = self._claim(assignment)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.assert_startable,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.assert_startable,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.allocator.mark_materialization_committed(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual(
            assignment,
            self.allocator.assert_startable(
                assignment.instance_uuid, host_id, token,
                assignment=assignment))
        self.allocator.request_release(
            assignment.instance_uuid, "instance-00000001",
            assignment=assignment)
        self.assertRaises(
            idmap.IDMapConflict, self.allocator.assert_startable,
            assignment.instance_uuid, host_id, token,
            assignment=assignment)

    def test_probe_counts_every_key_family_at_one_revision(self):
        first = self.allocator.allocate(self._uuid(60))
        second = self.allocator.allocate(self._uuid(61))
        self._claim(first)
        self.allocator.request_release(
            second.instance_uuid, "instance-00000061",
            assignment=self.allocator.get(second.instance_uuid))

        self.etcd.transactions.clear()
        counts = self.allocator.probe()

        self.assertEqual(
            {'instances': 2, 'slots': 2, 'releases': 1, 'hosts': 1}, counts)
        # One exact health read plus one count-only transaction.
        self.assertEqual(2, len(self.etcd.transactions))
        for request in self.etcd.transactions[-1]['success']:
            self.assertTrue(request['request_range']['count_only'])

    def test_release_intents_for_instances_are_one_exact_transaction(self):
        first = self.allocator.allocate(self._uuid(160))
        second = self.allocator.allocate(self._uuid(161))
        intent = self.allocator.request_release(
            first.instance_uuid, "instance-00000160",
            assignment=self.allocator.get(first.instance_uuid))
        self.etcd.transactions.clear()
        self.etcd.get_prefix_calls.clear()

        intents = self.allocator.list_release_intents_for_instances([
            second.instance_uuid, first.instance_uuid,
            first.instance_uuid,
        ])

        self.assertEqual([intent], intents)
        self.assertEqual([], self.etcd.get_prefix_calls)
        self.assertEqual(2, len(self.etcd.transactions))
        keys = {
            _decode(request['request_range']['key'])
            for request in self.etcd.transactions[-1]['success']
        }
        self.assertEqual({
            self.allocator.release_key(first.instance_uuid).encode(),
            self.allocator.release_key(second.instance_uuid).encode(),
        }, keys)

    def test_probe_does_not_read_the_namespace(self):
        """The probe's whole point is not paying for a full scan."""
        self.allocator.allocate(self._uuid(62))
        self.etcd.get_prefix_calls.clear()

        self.allocator.probe()

        self.assertEqual([], self.etcd.get_prefix_calls)

    def test_audit_control_is_outside_legacy_audited_prefix(self):
        self.assertFalse(self.allocator.audit_coordinator_key.startswith(
            self.allocator._prefix + '/'))
        self.assertFalse(self.allocator.audit_failure_key.startswith(
            self.allocator._prefix + '/'))
        self.assertEqual([], self.allocator.audit())

    def test_lease_expiry_elects_one_new_full_auditor(self):
        follower = self._allocator(client=self.etcd)
        follower.initialize()
        owner, snapshot = follower.run_coordinated_audit(full=False)
        self.assertFalse(owner)
        self.assertIsNone(snapshot)

        old_lease_id = self.allocator._audit_lease.id
        self.etcd.expire_lease(old_lease_id)
        owner, snapshot = follower.run_coordinated_audit(full=False)

        self.assertTrue(owner)
        self.assertEqual(([], [], []), snapshot)
        coordinator = follower._parse_audit_coordinator(
            self.etcd.values[follower.audit_coordinator_key.encode()])
        self.assertEqual(follower._coordinator_token, coordinator['token'])
        self.assertEqual('healthy', coordinator['state'])

        losing_follower = self._allocator(client=self.etcd)
        losing_follower.initialize()
        self.assertEqual(
            (False, None),
            losing_follower.run_coordinated_audit(full=True))

    def test_concurrent_lease_takeover_elects_exactly_one_owner(self):
        self.etcd.expire_lease(self.allocator._audit_lease.id)
        contenders = [self._allocator(client=self.etcd) for unused in range(2)]
        for contender in contenders:
            contender.initialize()
        self.etcd.transaction_barrier = threading.Barrier(2)
        self.etcd.transaction_barrier_remaining = 2

        with futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda allocator: allocator.run_coordinated_audit(full=True),
                contenders))

        self.assertEqual(1, sum(owner for owner, unused in results))
        self.assertEqual(1, sum(
            snapshot is not None for unused, snapshot in results))

    def test_owner_refuses_a_coordinator_rebound_to_another_lease(self):
        coordinator_key = self.allocator.audit_coordinator_key.encode()
        replacement = self.etcd.lease(self.allocator.audit_lease_ttl)
        self.etcd.key_leases[coordinator_key] = replacement.id

        self.assertRaisesRegex(
            idmap.IDMapBackendError, 'lost its owning lease',
            self.allocator.run_coordinated_audit, full=False)

    def test_exact_guard_rejects_lease_replacement_with_same_value(self):
        coordinator_key = self.allocator.audit_coordinator_key.encode()
        replacement = self.etcd.lease(self.allocator.audit_lease_ttl)

        def replace_lease(client, unused_transaction):
            client.key_leases[coordinator_key] = replacement.id

        self.etcd.before_transaction = replace_lease
        self.assertRaisesRegex(
            idmap.IDMapBackendError, 'compare failed',
            self.allocator._read_exact,
            (self.allocator.instance_key(self._uuid(76)),))

    def test_pending_coordinator_fails_sensitive_reads_closed(self):
        token = str(uuid.uuid4())
        self.etcd.values[self.allocator.audit_coordinator_key.encode()] = (
            self.allocator._audit_coordinator_raw(
                token, 'pending', str(uuid.uuid4())))

        self.assertRaisesRegex(
            idmap.IDMapBackendError, 'audit is in progress',
            self.allocator.get, self._uuid(70))

    def test_routine_full_audit_keeps_previous_generation_available(self):
        assignment = self.allocator.allocate(self._uuid(78))
        follower = self._allocator(client=self.etcd)
        follower.initialize()
        original = self.allocator._audit_with_latch

        def audit_while_reading():
            self.assertEqual(assignment, follower.get(
                assignment.instance_uuid))
            coordinator = self.allocator._parse_audit_coordinator(
                self.etcd.values[
                    self.allocator.audit_coordinator_key.encode()])
            self.assertEqual('healthy', coordinator['state'])
            return original()

        with mock.patch.object(
                self.allocator, '_audit_with_latch',
                side_effect=audit_while_reading):
            owner, snapshot = self.allocator.run_coordinated_audit(full=True)

        self.assertTrue(owner)
        self.assertIsNotNone(snapshot)

    def test_sticky_audit_failure_propagates_to_another_process(self):
        assignment = self.allocator.allocate(self._uuid(71))
        self.etcd.values.pop(
            self.allocator.slot_key(assignment.slot).encode())

        self.assertRaises(
            idmap.IDMapIntegrityError,
            self.allocator.run_coordinated_audit, full=True)
        self.assertIn(
            self.allocator.audit_failure_key.encode(), self.etcd.values)

        follower = self._allocator(client=self.etcd)
        follower.initialize()
        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, 'fleet audit failure is sticky',
            follower.get, assignment.instance_uuid)
        self.assertIsNotNone(follower._integrity_latch)

    def test_leased_failure_record_is_rejected_and_latched(self):
        failure_key = self.allocator.audit_failure_key.encode()
        lease = self.etcd.lease(self.allocator.audit_lease_ttl)
        self.etcd.values[failure_key] = self.allocator._audit_failure_raw(
            'operator miswrite')
        self.etcd.key_leases[failure_key] = lease.id
        follower = self._allocator(client=self.etcd)
        follower.initialize()

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, 'must not have a lease',
            follower.get, self._uuid(77))
        self.assertIsNotNone(follower._integrity_latch)

    def test_failure_publication_error_leaves_pending_until_takeover(self):
        assignment = self.allocator.allocate(self._uuid(73))
        self.etcd.values.pop(
            self.allocator.slot_key(assignment.slot).encode())

        with mock.patch.object(
                self.allocator, '_publish_integrity_failure',
                side_effect=idmap.IDMapBackendError(
                    reason='failure key unavailable')):
            self.assertRaises(
                idmap.IDMapIntegrityError,
                self.allocator.run_coordinated_audit, full=True)

        coordinator_raw = self.etcd.values[
            self.allocator.audit_coordinator_key.encode()]
        self.assertEqual(
            'pending',
            self.allocator._parse_audit_coordinator(
                coordinator_raw)['state'])
        self.assertNotIn(
            self.allocator.audit_failure_key.encode(), self.etcd.values)

        follower = self._allocator(client=self.etcd)
        follower.initialize()
        self.assertRaisesRegex(
            idmap.IDMapBackendError, 'audit is in progress',
            follower.get, assignment.instance_uuid)

        self.etcd.expire_lease(self.allocator._audit_lease.id)
        self.assertRaises(
            idmap.IDMapIntegrityError,
            follower.run_coordinated_audit, full=False)
        self.assertIn(
            follower.audit_failure_key.encode(), self.etcd.values)

    def test_mutation_cas_compares_fleet_health_generation(self):
        self.etcd.transactions.clear()
        self.allocator.allocate(self._uuid(72))

        mutations = [
            transaction for transaction in self.etcd.transactions
            if any('request_put' in request for request in
                   transaction.get('success', []))
        ]
        self.assertTrue(mutations)
        compared_keys = {
            _decode(comparison['key'])
            for comparison in mutations[-1]['compare']
        }
        self.assertIn(
            self.allocator.audit_coordinator_key.encode(), compared_keys)
        self.assertIn(
            self.allocator.audit_failure_key.encode(), compared_keys)
        health_compares = [
            comparison for comparison in mutations[-1]['compare']
            if (_decode(comparison['key']) ==
                self.allocator.audit_coordinator_key.encode())
        ]
        self.assertEqual({'LEASE', 'VALUE'}, {
            comparison['target'] for comparison in health_compares})

    def test_unleased_healthy_record_forces_a_new_full_audit(self):
        coordinator_key = self.allocator.audit_coordinator_key.encode()
        stale_raw = self.etcd.values[coordinator_key]
        self.etcd.key_leases.pop(coordinator_key)
        follower = self._allocator(client=self.etcd)
        follower.initialize()

        with mock.patch.object(
                follower, '_audit_with_latch',
                wraps=follower._audit_with_latch) as full_audit:
            self.assertIsNone(follower.get(self._uuid(74)))

        full_audit.assert_called_once_with()
        self.assertNotEqual(stale_raw, self.etcd.values[coordinator_key])
        self.assertGreater(self.etcd.key_leases[coordinator_key], 0)
        coordinator = follower._parse_audit_coordinator(
            self.etcd.values[coordinator_key])
        self.assertEqual('healthy', coordinator['state'])

    def test_expired_lease_residual_cannot_authorize_a_sensitive_read(self):
        coordinator_key = self.allocator.audit_coordinator_key.encode()
        stale_raw = self.etcd.values[coordinator_key]
        old_lease_id = self.etcd.key_leases[coordinator_key]
        self.etcd.expire_lease(old_lease_id)
        # Model a stale backup/operator write that restored the value without
        # restoring its expired lease. The raw value alone is not authority.
        self.etcd.values[coordinator_key] = stale_raw
        follower = self._allocator(client=self.etcd)
        follower.initialize()

        with mock.patch.object(
                follower, '_audit_with_latch',
                wraps=follower._audit_with_latch) as full_audit:
            self.assertIsNone(follower.get(self._uuid(75)))

        full_audit.assert_called_once_with()
        self.assertNotEqual(
            old_lease_id, self.etcd.key_leases[coordinator_key])

    def test_audit_generation_prevents_cross_audit_aba(self):
        stale_compares = list(self.allocator._guard_compares())
        stale_raw = self.allocator._fleet_health_raw

        owner, snapshot = self.allocator.run_coordinated_audit(full=False)

        self.assertTrue(owner)
        self.assertIsNone(snapshot)
        self.assertNotEqual(stale_raw, self.allocator._fleet_health_raw)
        self.assertFalse(self.allocator._transaction({
            'compare': stale_compares,
            'success': [],
            'failure': [],
        }))

    def test_probe_rejects_an_orphan_slot_record(self):
        assignment = self.allocator.allocate(self._uuid(63))
        self.etcd.values.pop(
            self.allocator.instance_key(assignment.instance_uuid).encode())

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "record counts differ",
            self.allocator.probe)

    def test_probe_rejects_an_orphan_allocation_record(self):
        assignment = self.allocator.allocate(self._uuid(64))
        self.etcd.values.pop(
            self.allocator.slot_key(assignment.slot).encode())

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "record counts differ",
            self.allocator.probe)

    def test_probe_rejects_more_intents_than_allocations(self):
        assignment = self.allocator.allocate(self._uuid(65))
        self.allocator.request_release(
            assignment.instance_uuid, "instance-00000065",
            assignment=self.allocator.get(assignment.instance_uuid))
        intent_raw = self.etcd.values[
            self.allocator.release_key(assignment.instance_uuid).encode()]
        self.etcd.values[
            self.allocator.release_key(self._uuid(66)).encode()] = intent_raw

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "exceeds allocation count",
            self.allocator.probe)

    def test_probe_rejects_claims_without_any_allocation(self):
        assignment = self.allocator.allocate(self._uuid(67))
        assignment, host_id, token = self._claim(assignment)
        self.etcd.values.pop(
            self.allocator.instance_key(assignment.instance_uuid).encode())
        self.etcd.values.pop(
            self.allocator.slot_key(assignment.slot).encode())

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "without any allocation",
            self.allocator.probe)

    def test_probe_rejects_a_replaced_configuration_record(self):
        self.etcd.values[
            self.allocator.configuration_key.encode()] = b'{"replaced": true}'

        self.assertRaises(idmap.IDMapIntegrityError, self.allocator.probe)

    def test_probe_rejects_a_configuration_replaced_mid_flight(self):
        """Counts from another namespace generation prove nothing."""
        def replace(etcd, unused_transaction):
            etcd.values[
                self.allocator.configuration_key.encode()] = (
                    b'{"replaced": true}')

        self.etcd.before_transaction = replace

        self.assertRaisesRegex(
            idmap.IDMapIntegrityError, "configuration record changed",
            self.allocator._probe_snapshot, guarded=True)

    def test_probe_does_not_latch_on_its_own(self):
        """Counts escalate to the audit; only a scan may latch."""
        assignment = self.allocator.allocate(self._uuid(68))
        self.etcd.values.pop(
            self.allocator.slot_key(assignment.slot).encode())

        self.assertRaises(idmap.IDMapIntegrityError, self.allocator.probe)
        self.assertIsNone(self.allocator._integrity_latch)

    def test_audit_rejects_orphan_and_mismatched_claim(self):
        assignment = self.allocator.allocate(self._uuid(23))
        assignment, host_id, token = self._claim(assignment)
        key = self.allocator.host_claim_key(
            host_id, assignment.instance_uuid).encode()
        self.etcd.values.pop(key)
        self.assertRaises(idmap.IDMapIntegrityError, self.allocator.audit)

        etcd = _FakeEtcd()
        allocator = self._allocator(client=etcd, namespace="cell2")
        allocator.bootstrap()
        assignment = allocator.allocate(self._uuid(24))
        raw = allocator._host_claim_raw(
            assignment, self._host(1), token)
        etcd.values[allocator.host_claim_key(
            self._host(1), assignment.instance_uuid).encode()] = raw
        self.assertRaises(idmap.IDMapIntegrityError, allocator.audit)

    def test_restore_exact_claim_state_and_proof_is_idempotent(self):
        assignment = self.allocator.allocate(self._uuid(25))
        assignment, host_id, token = self._claim(assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        proof = self._materialization_proof(
            assignment, host_id, token)
        claim = self.allocator.record_materialization_proof(
            assignment.instance_uuid, host_id, token, proof,
            assignment=assignment)

        target_etcd = _FakeEtcd()
        target = self._allocator(client=target_etcd, namespace="cell1")
        target.bootstrap()
        restored = target.restore(
            assignment.instance_uuid, assignment.base, assignment.size,
            assignment.allocation_id, assignment.fingerprint, [claim])
        repeated = target.restore(
            assignment.instance_uuid, assignment.base, assignment.size,
            assignment.allocation_id, assignment.fingerprint, [claim])
        self.assertEqual(restored, repeated)
        self.assertEqual(
            claim, target.get_host_claim(assignment.instance_uuid, host_id))

    def test_restore_rejects_noncanonical_claim_order(self):
        assignment = self.allocator.allocate(self._uuid(26))
        assignment, first_host, first_token = self._claim(
            assignment, host_number=1, token_number=1)
        assignment, second_host, second_token = self._claim(
            assignment, host_number=2, token_number=2)
        claims = [
            self.allocator.get_host_claim(assignment.instance_uuid, host)
            for host in (second_host, first_host)
        ]
        self.assertRaises(
            idmap.IDMapConfigurationError, self.allocator.validate_restore,
            assignment.instance_uuid, assignment.base, assignment.size,
            assignment.allocation_id, assignment.fingerprint, claims)

    def test_noncanonical_uuids_fail_closed(self):
        assignment = self.allocator.allocate(
            str(uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")))
        host_id = str(uuid.UUID(
            "bbbbbbbb-0000-0000-0000-000000000001"))
        token = str(uuid.UUID(
            "cccccccc-0000-0000-0000-000000000001"))
        self.assertRaises(
            idmap.IDMapConfigurationError, self.allocator.claim,
            assignment.instance_uuid.upper(), host_id,
            token, assignment=assignment)
        self.assertRaises(
            idmap.IDMapConfigurationError, self.allocator.claim,
            assignment.instance_uuid, host_id.upper(),
            token, assignment=assignment)
        self.assertRaises(
            idmap.IDMapConfigurationError, self.allocator.claim,
            assignment.instance_uuid, host_id,
            token.upper(), assignment=assignment)

    def test_mutations_use_exact_reads_not_full_namespace_scans(self):
        self.etcd.get_prefix_calls.clear()
        assignment = self.allocator.allocate(self._uuid(28))
        assignment, host_id, token = self._claim(assignment)
        self.allocator.mark_materialization_possible(
            assignment.instance_uuid, host_id, token,
            assignment=assignment)
        self.assertEqual([], self.etcd.get_prefix_calls)
