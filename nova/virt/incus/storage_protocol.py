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

from dataclasses import dataclass
import re
import uuid

from nova.virt.incus import idmap


STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION = (
    "storage_materialization_attempt_v1")
STORAGE_RELEASE_RECEIPT_EXTENSION = "storage_release_receipt_v2"
STORAGE_OWNERSHIP_EXTENSIONS = frozenset((
    STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,
    STORAGE_RELEASE_RECEIPT_EXTENSION,
))

_DRIVER_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_STORAGE_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,255}$")
_ATTEMPT_STATES = frozenset((
    "active", "aborted", "committed", "clean", "retired"))
_STORAGE_PHASES = frozenset((
    "none", "pending", "materialized", "clean"))


@dataclass(frozen=True)
class StorageMaterializationBinding:
    """One immutable A/H/T/U and root-storage materialization binding."""

    token: str
    allocation_id: str
    compute_id: str
    owner: str
    project: str
    instance_name: str
    idmap_base: int
    idmap_size: int
    storage_driver: str
    storage_pool: str
    storage_volume: str
    cleanup_disposition: str
    rbd_image: str = ""

    def __post_init__(self):
        _validate_binding(self)


@dataclass(frozen=True)
class StorageMaterializationIdentity:
    """The exact allocator and compute identity of one materialization."""

    token: str
    allocation_id: str
    compute_id: str
    owner: str
    project: str
    instance_name: str
    idmap_base: int
    idmap_size: int

    def __post_init__(self):
        _validate_identity(self)


@dataclass(frozen=True)
class StorageMaterializationAttempt:
    """A validated Incus storage_materialization_attempt_v1 response."""

    binding: StorageMaterializationBinding
    storage_identity: str
    baseline_clean: bool
    state: str
    storage_phase: str
    started: bool
    finished: bool
    operation_uuid: str
    daemon_start: int
    proof: object = None


def require_storage_ownership_extensions(client, extensions=None):
    """Fail closed unless Incus advertises the selected protocol versions."""
    required = STORAGE_OWNERSHIP_EXTENSIONS if extensions is None else set(
        extensions)
    host_info = getattr(client, "host_info", None)
    advertised = host_info.get("api_extensions") if isinstance(
        host_info, dict) else None
    if not isinstance(advertised, (list, tuple, set, frozenset)):
        raise idmap.IDMapConfigurationError(
            reason="Incus did not return a valid API extension list")
    missing = sorted(set(required) - set(advertised))
    if missing:
        raise idmap.IDMapConfigurationError(
            reason="Incus lacks required storage ownership extensions: %s" %
                   ", ".join(missing))


class StorageOwnershipClient:
    """Thin, fail-closed client for Incus root-storage ownership proofs."""

    def __init__(self, client):
        self.client = client

    def register_materialization(self, binding):
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        response = self._materialization_endpoint(binding).put(
            params={"project": binding.project},
            json={
                "state": "active",
                "allocation_id": binding.allocation_id,
                "compute_id": binding.compute_id,
                "owner": binding.owner,
                "instance_name": binding.instance_name,
                "idmap_base": binding.idmap_base,
                "idmap_size": binding.idmap_size,
                "storage_driver": binding.storage_driver,
                "storage_pool": binding.storage_pool,
                "storage_volume": binding.storage_volume,
                "rbd_image": binding.rbd_image,
                "cleanup_disposition": binding.cleanup_disposition,
            })
        attempt = self._parse_attempt(response, binding)
        if (attempt.state != "active" or attempt.started or
                attempt.finished or attempt.storage_phase != "none" or
                attempt.proof is not None):
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization registration is not pristine")
        return attempt

    def get_materialization(self, binding):
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        response = self._materialization_endpoint(binding).get(
            params={"project": binding.project})
        return self._parse_attempt(response, binding)

    def discover_materialization(self, identity):
        """Load an immutable server binding for one exact A/H/T/U claim."""
        _validate_identity(identity)
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        response = self.client.api["storage-materialization-attempts"][
            identity.token].get(params={"project": identity.project})
        metadata = _response_metadata(response, "materialization attempt")
        binding = _binding_from_metadata(metadata, identity)
        return self._parse_attempt(response, binding)

    def observe_materialization_start(self, binding):
        """GET and prove that Incus crossed its durable start boundary."""
        attempt = self.get_materialization(binding)
        if not attempt.started or attempt.storage_phase not in (
                "pending", "materialized", "clean"):
            raise idmap.IDMapConflict(
                reason="Incus materialization has not durably started")
        return attempt

    def abort_materialization(self, binding):
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        response = self._materialization_endpoint(binding).put(
            params={"project": binding.project}, json={"state": "aborted"})
        attempt = self._parse_attempt(response, binding)
        if attempt.state not in ("aborted", "clean", "retired"):
            raise idmap.IDMapIntegrityError(
                reason="Incus did not fence the materialization attempt")
        return attempt

    def settle_materialization(self, binding):
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        response = self._materialization_endpoint(binding).put(
            params={"project": binding.project}, json={"state": "settled"})
        return self._parse_attempt(response, binding)

    def acknowledge_materialization_proof(self, binding, proof):
        """Retire a proof only after the caller has durably persisted it."""
        idmap.validate_materialization_proof(proof)
        _validate_proof_binding(binding, proof)
        require_storage_ownership_extensions(
            self.client, (STORAGE_MATERIALIZATION_ATTEMPT_EXTENSION,))
        return self._materialization_endpoint(binding).delete(params={
            "project": binding.project,
            "proof-digest": proof.digest,
            "allocation-id": binding.allocation_id,
            "compute-id": binding.compute_id,
            "owner": binding.owner,
            "instance": binding.instance_name,
            "idmap-base": str(binding.idmap_base),
            "idmap-size": str(binding.idmap_size),
        })

    def get_release_receipt(self, binding):
        require_storage_ownership_extensions(
            self.client, (STORAGE_RELEASE_RECEIPT_EXTENSION,))
        response = self._receipt_endpoint(binding).get(
            params=_release_receipt_params(binding))
        return self._parse_receipt(response, binding)

    def discover_release_receipt(self, identity):
        """Load a v2 receipt and its immutable storage binding."""
        _validate_identity(identity)
        require_storage_ownership_extensions(
            self.client, (STORAGE_RELEASE_RECEIPT_EXTENSION,))
        response = self.client.api["storage-release-receipts"][
            identity.token].get(params=_release_receipt_identity_params(
                identity))
        metadata = _response_metadata(response, "storage release receipt")
        binding = _binding_from_metadata(metadata, identity)
        return binding, self._parse_receipt(response, binding)

    def acknowledge_release_receipt(self, binding, receipt):
        """Retire a receipt only after the caller has durably persisted it."""
        idmap.validate_rootfs_release_receipt(receipt)
        _validate_receipt_binding(binding, receipt)
        require_storage_ownership_extensions(
            self.client, (STORAGE_RELEASE_RECEIPT_EXTENSION,))
        params = _release_receipt_params(binding)
        params["receipt-digest"] = receipt.digest
        return self._receipt_endpoint(binding).delete(params=params)

    def _materialization_endpoint(self, binding):
        return self.client.api["storage-materialization-attempts"][
            binding.token]

    def _receipt_endpoint(self, binding):
        return self.client.api["storage-release-receipts"][binding.token]

    @staticmethod
    def _parse_attempt(response, binding):
        metadata = _response_metadata(response, "materialization attempt")
        expected = {
            "token": binding.token,
            "allocation_id": binding.allocation_id,
            "compute_id": binding.compute_id,
            "owner": binding.owner,
            "project": binding.project,
            "instance_name": binding.instance_name,
            "idmap_base": binding.idmap_base,
            "idmap_size": binding.idmap_size,
            "storage_driver": binding.storage_driver,
            "storage_pool": binding.storage_pool,
            "storage_volume": binding.storage_volume,
            "rbd_image": binding.rbd_image,
            "cleanup_disposition": binding.cleanup_disposition,
        }
        for name, value in expected.items():
            actual = metadata.get(name, "" if name == "rbd_image" else None)
            if type(actual) is not type(value) or actual != value:
                raise idmap.IDMapIntegrityError(
                    reason="Incus materialization %s binding mismatches" %
                           name)

        storage_identity = metadata.get("storage_identity", "")
        _safe_text(storage_identity, "storage identity", allow_empty=True)
        baseline_clean = metadata.get("baseline_clean")
        state = metadata.get("state")
        storage_phase = metadata.get("storage_phase")
        started = metadata.get("started")
        finished = metadata.get("finished")
        operation_uuid = metadata.get("operation_uuid", "")
        daemon_start = metadata.get("daemon_start")
        if baseline_clean is not True:
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization lacks a clean baseline")
        invalid_state = (
            state not in _ATTEMPT_STATES or
            storage_phase not in _STORAGE_PHASES)
        if invalid_state:
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization state is invalid")
        if type(started) is not bool or type(finished) is not bool:
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization flags are invalid")
        if type(daemon_start) is not int or daemon_start < 0:
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization daemon generation is invalid")
        _safe_text(operation_uuid, "operation UUID", allow_empty=True)
        if operation_uuid:
            _canonical_uuid(operation_uuid, "operation UUID")
        if (binding.cleanup_disposition in ("detach", "handover") and
                not storage_identity):
            raise idmap.IDMapIntegrityError(
                reason="Incus detached materialization lacks identity")
        # A materialized volume must be identifiable, but "clean" only
        # states that no volume is left behind: a build that failed before
        # materialization never created one, so it has nothing to identify.
        # Requiring an identity there strands exactly the claims that this
        # cleanup exists to release.
        if (storage_phase == "materialized" and
                binding.storage_driver in ("ceph", "cephext") and
                not storage_identity):
            raise idmap.IDMapIntegrityError(
                reason="Incus Ceph materialization lacks identity")

        proof = _attempt_proof(metadata, binding, storage_identity)
        attempt = StorageMaterializationAttempt(
            binding=binding,
            storage_identity=storage_identity,
            baseline_clean=baseline_clean,
            state=state,
            storage_phase=storage_phase,
            started=started,
            finished=finished,
            operation_uuid=operation_uuid,
            daemon_start=daemon_start,
            proof=proof,
        )
        _validate_attempt_state(attempt)
        return attempt

    @staticmethod
    def _parse_receipt(response, binding):
        metadata = _response_metadata(response, "storage release receipt")
        try:
            receipt = idmap.IDMapRootfsReleaseReceipt(
                token=metadata["token"],
                allocation_id=metadata["allocation_id"],
                compute_id=metadata["compute_id"],
                materialization_id=metadata["materialization_id"],
                owner=metadata["owner"],
                project=metadata["project"],
                instance_name=metadata["instance_name"],
                idmap_base=metadata["idmap_base"],
                idmap_size=metadata["idmap_size"],
                storage_driver=metadata["storage_driver"],
                storage_pool=metadata["storage_pool"],
                storage_volume=metadata["storage_volume"],
                rbd_image=metadata.get("rbd_image", ""),
                storage_identity=metadata.get("storage_identity", ""),
                baseline_clean=metadata["baseline_clean"],
                cleanup_disposition=metadata["cleanup_disposition"],
                outcome=metadata["outcome"],
                state=metadata["state"],
                digest=metadata["digest"],
                created_at=metadata["created_at"],
                completed_at=metadata["completed_at"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise idmap.IDMapIntegrityError(
                reason="Incus returned a malformed storage release receipt"
            ) from exc
        idmap.validate_rootfs_release_receipt(receipt)
        _validate_receipt_binding(binding, receipt)
        return receipt


def _validate_binding(binding):
    if not isinstance(binding, StorageMaterializationBinding):
        raise idmap.IDMapConfigurationError(
            reason="storage materialization binding has an invalid type")
    for name in ("token", "allocation_id", "compute_id", "owner"):
        _canonical_uuid(getattr(binding, name), name.replace("_", " "))
    _safe_text(binding.project, "project", max_length=255)
    _safe_text(binding.instance_name, "instance name", max_length=255)
    if "/" in binding.project or "/" in binding.instance_name:
        raise idmap.IDMapConfigurationError(
            reason="project and instance names cannot contain slash")
    if type(binding.idmap_base) is not int or type(
            binding.idmap_size) is not int:
        raise idmap.IDMapConfigurationError(
            reason="storage materialization ID map is invalid")
    if (binding.idmap_base < 0 or binding.idmap_size <= 0 or
            binding.idmap_size > 2 ** 32 or
            binding.idmap_base > 2 ** 32 - binding.idmap_size):
        raise idmap.IDMapConfigurationError(
            reason="storage materialization ID map is invalid")
    if not _DRIVER_RE.fullmatch(binding.storage_driver):
        raise idmap.IDMapConfigurationError(
            reason="storage driver is invalid")
    for name in ("storage_pool", "storage_volume"):
        if not _STORAGE_NAME_RE.fullmatch(getattr(binding, name)):
            raise idmap.IDMapConfigurationError(
                reason="%s is invalid" % name.replace("_", " "))
    if binding.rbd_image and not _STORAGE_NAME_RE.fullmatch(
            binding.rbd_image):
        raise idmap.IDMapConfigurationError(reason="RBD image is invalid")
    if binding.cleanup_disposition not in (
            "delete", "detach", "handover"):
        raise idmap.IDMapConfigurationError(
            reason="cleanup disposition must be delete, detach or handover")
    if (binding.storage_driver == "cephext" and
            binding.cleanup_disposition != "detach"):
        raise idmap.IDMapConfigurationError(
            reason="cephext materialization must use detach cleanup")
    if (binding.cleanup_disposition == "detach" and
            binding.storage_driver not in ("ceph", "cephext")):
        raise idmap.IDMapConfigurationError(
            reason="detach cleanup requires a Ceph storage driver")
    if (binding.cleanup_disposition == "handover" and
            binding.storage_driver != "ceph"):
        raise idmap.IDMapConfigurationError(
            reason="handover cleanup requires the Ceph storage driver")


def _validate_identity(identity):
    if not isinstance(identity, StorageMaterializationIdentity):
        raise idmap.IDMapConfigurationError(
            reason="storage materialization identity has an invalid type")
    for name in ("token", "allocation_id", "compute_id", "owner"):
        _canonical_uuid(getattr(identity, name), name.replace("_", " "))
    _safe_text(identity.project, "project", max_length=255)
    _safe_text(identity.instance_name, "instance name", max_length=255)
    if "/" in identity.project or "/" in identity.instance_name:
        raise idmap.IDMapConfigurationError(
            reason="project and instance names cannot contain slash")
    if type(identity.idmap_base) is not int or type(
            identity.idmap_size) is not int:
        raise idmap.IDMapConfigurationError(
            reason="storage materialization ID map is invalid")
    if (identity.idmap_base < 0 or identity.idmap_size <= 0 or
            identity.idmap_size > 2 ** 32 or
            identity.idmap_base > 2 ** 32 - identity.idmap_size):
        raise idmap.IDMapConfigurationError(
            reason="storage materialization ID map is invalid")


def _binding_from_metadata(metadata, identity):
    expected = {
        "token": identity.token,
        "allocation_id": identity.allocation_id,
        "compute_id": identity.compute_id,
        "owner": identity.owner,
        "project": identity.project,
        "instance_name": identity.instance_name,
        "idmap_base": identity.idmap_base,
        "idmap_size": identity.idmap_size,
    }
    for name, value in expected.items():
        actual = metadata.get(name)
        if type(actual) is not type(value) or actual != value:
            raise idmap.IDMapIntegrityError(
                reason="Incus materialization %s binding mismatches" % name)
    try:
        return StorageMaterializationBinding(
            token=identity.token,
            allocation_id=identity.allocation_id,
            compute_id=identity.compute_id,
            owner=identity.owner,
            project=identity.project,
            instance_name=identity.instance_name,
            idmap_base=identity.idmap_base,
            idmap_size=identity.idmap_size,
            storage_driver=metadata["storage_driver"],
            storage_pool=metadata["storage_pool"],
            storage_volume=metadata["storage_volume"],
            cleanup_disposition=metadata["cleanup_disposition"],
            rbd_image=metadata.get("rbd_image", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise idmap.IDMapIntegrityError(
            reason="Incus returned an invalid immutable storage binding"
        ) from exc


def _canonical_uuid(value, label):
    if not isinstance(value, str):
        raise idmap.IDMapConfigurationError(
            reason="%s must be a canonical UUID" % label)
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise idmap.IDMapConfigurationError(
            reason="%s must be a canonical UUID" % label) from exc
    if canonical != value:
        raise idmap.IDMapConfigurationError(
            reason="%s must be a canonical UUID" % label)
    return value


def _safe_text(value, label, max_length=None, allow_empty=False):
    if (not isinstance(value, str) or (not allow_empty and not value) or
            (max_length is not None and len(value) > max_length) or
            any(ord(character) < 32 for character in value)):
        raise idmap.IDMapIntegrityError(reason="%s is invalid" % label)
    return value


def _response_metadata(response, label):
    try:
        body = response.json()
        metadata = body["metadata"]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise idmap.IDMapIntegrityError(
            reason="Incus returned malformed %s metadata" % label) from exc
    if not isinstance(metadata, dict):
        raise idmap.IDMapIntegrityError(
            reason="Incus returned malformed %s metadata" % label)
    return metadata


def _attempt_proof(metadata, binding, storage_identity):
    value = metadata.get("proof")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"outcome", "digest"}:
        raise idmap.IDMapIntegrityError(
            reason="Incus materialization proof schema is invalid")
    state = metadata.get("state")
    proof_state = state
    if state == "retired":
        proof_state = (
            "aborted" if value.get("outcome") == "not-materialized"
            else "clean")
    proof = idmap.IDMapMaterializationProof(
        token=binding.token,
        allocation_id=binding.allocation_id,
        compute_id=binding.compute_id,
        owner=binding.owner,
        project=binding.project,
        instance_name=binding.instance_name,
        idmap_base=binding.idmap_base,
        idmap_size=binding.idmap_size,
        storage_driver=binding.storage_driver,
        storage_pool=binding.storage_pool,
        storage_volume=binding.storage_volume,
        rbd_image=binding.rbd_image,
        storage_identity=storage_identity,
        baseline_clean=metadata.get("baseline_clean"),
        cleanup_disposition=binding.cleanup_disposition,
        state=proof_state,
        storage_phase=metadata.get("storage_phase"),
        started=metadata.get("started"),
        finished=metadata.get("finished"),
        outcome=value.get("outcome"),
        digest=value.get("digest"),
    )
    idmap.validate_materialization_proof(proof)
    return proof


def _validate_attempt_state(attempt):
    proof = attempt.proof
    if attempt.state == "active":
        valid = (
            not attempt.finished and proof is None and
            ((not attempt.started and attempt.storage_phase == "none") or
             (attempt.started and attempt.storage_phase in
              ("pending", "materialized"))))
    elif attempt.state == "aborted":
        valid = (
            (not attempt.started and attempt.finished and
             attempt.storage_phase == "none" and proof is not None) or
            (attempt.started and not attempt.finished and
             attempt.storage_phase in ("pending", "materialized") and
             proof is None))
    elif attempt.state == "committed":
        valid = (attempt.started and attempt.finished and
                 attempt.storage_phase == "materialized" and proof is None)
    elif attempt.state == "clean":
        valid = (attempt.started and attempt.finished and
                 attempt.storage_phase == "clean" and proof is not None)
    else:  # retired tombstone, whose proof remains replayable.
        valid = attempt.finished and proof is not None and (
            (not attempt.started and attempt.storage_phase == "none") or
            (attempt.started and attempt.storage_phase == "clean"))
    if not valid:
        raise idmap.IDMapIntegrityError(
            reason="Incus materialization state tuple is inconsistent")


def _validate_proof_binding(binding, proof):
    expected = (
        binding.token, binding.allocation_id, binding.compute_id,
        binding.owner, binding.project, binding.instance_name,
        binding.idmap_base, binding.idmap_size, binding.storage_driver,
        binding.storage_pool, binding.storage_volume, binding.rbd_image,
        binding.cleanup_disposition,
    )
    actual = (
        proof.token, proof.allocation_id, proof.compute_id, proof.owner,
        proof.project, proof.instance_name, proof.idmap_base,
        proof.idmap_size, proof.storage_driver, proof.storage_pool,
        proof.storage_volume, proof.rbd_image, proof.cleanup_disposition,
    )
    if actual != expected:
        raise idmap.IDMapIntegrityError(
            reason="materialization proof belongs to another binding")


def _validate_receipt_binding(binding, receipt):
    expected = (
        binding.token, binding.allocation_id, binding.compute_id,
        binding.token, binding.owner, binding.project,
        binding.instance_name, binding.idmap_base, binding.idmap_size,
        binding.storage_driver, binding.storage_pool,
        binding.storage_volume, binding.rbd_image,
        binding.cleanup_disposition,
    )
    actual = (
        receipt.token, receipt.allocation_id, receipt.compute_id,
        receipt.materialization_id, receipt.owner, receipt.project,
        receipt.instance_name, receipt.idmap_base, receipt.idmap_size,
        receipt.storage_driver, receipt.storage_pool,
        receipt.storage_volume, receipt.rbd_image,
        receipt.cleanup_disposition,
    )
    if actual != expected:
        raise idmap.IDMapIntegrityError(
            reason="storage release receipt belongs to another binding")


def _release_receipt_params(binding):
    return {
        "project": binding.project,
        "rootfs-idmap-release-owner": binding.owner,
        "rootfs-idmap-allocation-id": binding.allocation_id,
        "rootfs-idmap-compute-id": binding.compute_id,
        "instance": binding.instance_name,
        "idmap-base": str(binding.idmap_base),
        "idmap-size": str(binding.idmap_size),
    }


def _release_receipt_identity_params(identity):
    return {
        "project": identity.project,
        "rootfs-idmap-release-owner": identity.owner,
        "rootfs-idmap-allocation-id": identity.allocation_id,
        "rootfs-idmap-compute-id": identity.compute_id,
        "instance": identity.instance_name,
        "idmap-base": str(identity.idmap_base),
        "idmap-size": str(identity.idmap_size),
    }
