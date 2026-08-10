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
from dataclasses import dataclass
import hashlib
import os
import re
import stat
import threading
import time
from urllib import parse as urlparse
import uuid

from nova import exception
from nova.i18n import _
from oslo_serialization import jsonutils
from oslo_utils import uuidutils


_SCHEMA_VERSION = 3
_KEY_PREFIX = "/openstack-incus/idmaps/v3"
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RECEIPT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CLAIM_STATES = frozenset((
    "unmaterialized", "possible", "committed", "cleaned"))
_MATERIALIZATION_OUTCOMES = frozenset((
    "not-materialized", "reconciled-clean"))
_RELEASE_OUTCOMES = frozenset(("deleted", "normalized", "detached"))
_INVALID_AUTH_TOKEN = "etcdserver: invalid auth token"
_AUDIT_CONTROL_SCHEMA = 1
_AUDIT_CONTROL_STATES = frozenset(("pending", "healthy"))


class IDMapError(exception.NovaException):
    msg_fmt = "Incus ID map allocation failed: %(reason)s"


class IDMapAllocationError(IDMapError):
    """Compatibility name for errors raised while allocating a slot."""


class IDMapConfigurationError(IDMapError):
    pass


class IDMapBackendError(IDMapError):
    pass


class IDMapIntegrityError(IDMapError):
    pass


class IDMapConflict(IDMapError):
    pass


class IDMapExhausted(IDMapError):
    pass


class _GatewayResponseError(Exception):
    """Normalize an etcd gateway error returned as a successful HTTP body."""

    def __init__(self, detail):
        super().__init__("etcd gateway returned an error response")
        self.detail_text = detail


@dataclass(frozen=True)
class IDMapAssignment:
    """One durable, fleet-wide isolated ID-map allocation."""

    instance_uuid: str
    base: int
    size: int
    slot: int
    allocation_id: str
    fingerprint: str
    host_ids: tuple = ()

    def __post_init__(self):
        host_ids = tuple(self.host_ids)
        if host_ids != tuple(sorted(set(host_ids))):
            raise ValueError(
                _("ID-map host claims must be unique and sorted"))
        for host_id in host_ids:
            if not isinstance(host_id, str):
                raise ValueError(_("ID-map host claim is invalid"))
            try:
                normalized = str(uuid.UUID(host_id))
            except (AttributeError, TypeError, ValueError):
                raise ValueError(_("ID-map host claim is invalid"))
            if normalized != host_id:
                raise ValueError(_(
                    "ID-map host claim must be a canonical compute UUID"))
        object.__setattr__(self, "host_ids", host_ids)


@dataclass(frozen=True)
class IDMapReleaseIntent:
    """One immutable, shared request to retire an allocation generation."""

    instance_uuid: str
    instance_name: str
    base: int
    size: int
    slot: int
    allocation_id: str
    fingerprint: str


@dataclass(frozen=True)
class IDMapHostClaim:
    """One exact host/materialization claim for an allocation generation."""

    host_id: str
    materialization_id: str
    instance_uuid: str
    base: int
    size: int
    slot: int
    allocation_id: str
    fingerprint: str
    state: str
    proof: object = None


@dataclass(frozen=True)
class IDMapFenceProof:
    """Operator evidence that external STONITH powered off a claim holder.

    This is the only authority that may dispose of a claim without the
    holding host's storage release receipt, because a fenced host can never
    produce one. It is written to the registry's fence ledger so the
    disposal stays auditable and distinguishable from a normal cleanup.
    """

    instance_uuid: str
    host_id: str
    allocation_id: str
    fence_agent: str
    fenced_at: str
    operator: str
    evidence: str


@dataclass(frozen=True)
class IDMapMaterializationProof:
    """Exact Incus proof that one materialization cannot retain artifacts."""

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
    rbd_image: str
    storage_identity: str
    baseline_clean: bool
    cleanup_disposition: str
    state: str
    storage_phase: str
    started: bool
    finished: bool
    outcome: str
    digest: str


@dataclass(frozen=True)
class IDMapRootfsReleaseReceipt:
    """Canonical completed Incus root-storage release receipt."""

    token: str
    allocation_id: str
    compute_id: str
    materialization_id: str
    owner: str
    project: str
    instance_name: str
    idmap_base: int
    idmap_size: int
    storage_driver: str
    storage_pool: str
    storage_volume: str
    storage_identity: str
    rbd_image: str
    baseline_clean: bool
    cleanup_disposition: str
    outcome: str
    state: str
    digest: str
    created_at: int
    completed_at: int


@dataclass(frozen=True)
class IDMapRootfsReleaseProof:
    """Exact Incus proof that one host-local rootfs claim was released."""

    token: str
    allocation_id: str
    compute_id: str
    materialization_id: str
    owner: str
    instance_name: str
    project: str
    idmap_base: int
    idmap_size: int
    storage_driver: str
    storage_pool: str
    storage_volume: str
    storage_identity: str
    rbd_image: str
    baseline_clean: bool
    cleanup_disposition: str
    outcome: str
    state: str
    digest: str
    created_at: int
    completed_at: int


class IDMapAllocator:
    """Allocate fixed Incus ID maps through a shared etcd v3 gateway.

    Every allocation is represented by two records written in one etcd
    transaction: an instance-to-slot record and a slot-to-instance record.
    Readers require both records to contain the same canonical value. This
    makes partial or manually corrupted state fail closed.
    """

    def __init__(self, endpoint, namespace, base, size, count, timeout=5,
                 ca_cert=None, cert_cert=None, cert_key=None,
                 username=None, password_file=None,
                 allow_insecure=False, client=None, audit_lease_ttl=180):
        self.endpoint = endpoint
        if namespace is None or not str(namespace).strip():
            raise IDMapConfigurationError(
                reason="allocation namespace must be set")
        self.namespace = str(namespace)
        self.base = self._positive_int(base, "base")
        self.size = self._positive_int(size, "size")
        self.count = self._positive_int(count, "count")
        self.timeout = self._positive_int(timeout, "timeout")
        self.audit_lease_ttl = self._positive_int(
            audit_lease_ttl, "audit lease TTL")
        self.ca_cert = ca_cert
        self.cert_cert = cert_cert
        self.cert_key = cert_key
        self.username = username
        self.password_file = password_file
        self.allow_insecure = bool(allow_insecure)
        self._authenticated = False
        self._authentication_generation = 0
        self._authentication_lock = threading.Lock()

        if not _NAMESPACE_RE.fullmatch(self.namespace):
            raise IDMapConfigurationError(
                reason="invalid allocation namespace %r" % self.namespace)
        if bool(cert_cert) != bool(cert_key):
            raise IDMapConfigurationError(
                reason="client certificate and key must be set together")
        if bool(username) != bool(password_file):
            raise IDMapConfigurationError(
                reason="etcd username and password file must be set together")
        if password_file and not os.path.isabs(password_file):
            raise IDMapConfigurationError(
                reason="etcd password file path must be absolute")

        parsed = self._parse_endpoint(endpoint)
        if not self.allow_insecure:
            if parsed.scheme != "https":
                raise IDMapConfigurationError(
                    reason="production etcd endpoint must use HTTPS")
            if not ca_cert or not cert_cert or not cert_key:
                raise IDMapConfigurationError(
                    reason=("production etcd endpoint requires a CA, "
                            "client certificate and client key"))
            if not username or not password_file:
                raise IDMapConfigurationError(
                    reason=("production etcd endpoint requires an etcd "
                            "username and password file for namespace RBAC"))

        if self.base + self.count * self.size > 2 ** 32:
            raise IDMapConfigurationError(
                reason="configured ID-map range exceeds uint32")

        self._prefix = "%s/%s" % (_KEY_PREFIX, self.namespace)
        # This sibling prefix is intentionally outside the v3 allocator
        # namespace. Older binaries reject unknown records below _prefix
        # during a full audit, but safely ignore these rolling-upgrade
        # coordination records.
        self._control_prefix = "%s-control/%s" % (
            _KEY_PREFIX, self.namespace)
        material = {
            "base": self.base,
            "count": self.count,
            "namespace": self.namespace,
            "schema": _SCHEMA_VERSION,
            "size": self.size,
        }
        material_raw = self._json_bytes(material)
        self.fingerprint = hashlib.sha256(material_raw).hexdigest()
        self._configuration = dict(material, fingerprint=self.fingerprint)
        self._configuration_raw = self._json_bytes(self._configuration)
        self._client = client or self._new_client(parsed)
        self._initialized = False
        self._integrity_latch = None
        self._coordinator_token = str(uuid.uuid4())
        self._audit_lease = None
        self._fleet_health_raw = None
        self._fleet_health_lease_id = None

    @staticmethod
    def _positive_int(value, name, allow_zero=False):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise IDMapConfigurationError(
                reason="%s must be an integer" % name)
        minimum = 0 if allow_zero else 1
        if parsed < minimum:
            raise IDMapConfigurationError(
                reason="%s must be at least %d" % (name, minimum))
        return parsed

    @staticmethod
    def _parse_endpoint(endpoint):
        try:
            parsed = urlparse.urlparse(str(endpoint))
            parsed.port
        except (TypeError, ValueError) as exc:
            raise IDMapConfigurationError(
                reason="invalid etcd endpoint: %s" % exc)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or
                parsed.fragment or parsed.path not in ("", "/")):
            raise IDMapConfigurationError(
                reason="etcd endpoint must be an HTTP(S) origin")
        return parsed

    def _new_client(self, parsed):
        try:
            from etcd3gw import client as etcd_client
        except ImportError as exc:
            raise IDMapBackendError(
                reason="etcd3gw is required: %s" % exc)

        try:
            return etcd_client(
                host=parsed.hostname,
                port=parsed.port or 2379,
                protocol=parsed.scheme,
                timeout=self.timeout,
                ca_cert=self.ca_cert,
                cert_cert=self.cert_cert,
                cert_key=self.cert_key,
            )
        except Exception as exc:
            raise IDMapBackendError(
                reason="cannot create etcd client: %s" % exc)

    def _read_password(self):
        # This credential authorizes every mutation of the registry that
        # keeps two hosts from ever sharing an ID-map range, so a file any
        # local account can read is a real exposure and not merely untidy.
        # Group access is deliberately allowed: nova-compute does not run
        # as root and reads this through its group, so requiring 0600 here
        # would reject the supported deployment rather than protect it.
        # World access has no such justification. Checked on the open file
        # descriptor, not the path, so the answer describes what was
        # actually read.
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            path = os.path.abspath(self.password_file)
            descriptor = os.open(path, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("path is not a regular file")
                if stat.S_IMODE(info.st_mode) & 0o007:
                    raise ValueError(
                        "file is accessible by other")
                if info.st_size > 4096:
                    raise ValueError("file exceeds 4096 bytes")
                with os.fdopen(descriptor, "rb") as password_stream:
                    descriptor = None
                    password = password_stream.read()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise IDMapBackendError(
                reason="cannot read etcd password file: %s" % exc)

        password = password.rstrip(b"\r\n")
        if not password or b"\x00" in password:
            raise IDMapConfigurationError(
                reason="etcd password file is empty or invalid")
        try:
            return password.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IDMapConfigurationError(
                reason="etcd password file is not valid UTF-8: %s" % exc)

    def _authenticate_locked(self):
        """Obtain a gateway token while holding the authentication lock."""
        try:
            session = self._client.session
            headers = session.headers
            self._authenticated = False
            result = self._client.post(
                self._client.get_url("/auth/authenticate"),
                # A per-request None removes the shared Authorization header
                # from this request without exposing an unauthenticated
                # window to concurrent registry operations.
                headers={"Authorization": None},
                json={
                    "name": self.username,
                    "password": self._read_password(),
                },
            )
            token = result.get("token") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                raise ValueError(
                    "etcd authentication response contains no token")
            headers["Authorization"] = token
        except IDMapError:
            raise
        except Exception as exc:
            raise IDMapBackendError(
                reason="etcd authentication failed: %s" % exc)

        self._authenticated = True
        self._authentication_generation += 1
        return self._authentication_generation

    def _ensure_authenticated(self):
        if not self.username:
            return None
        if self._authenticated:
            return self._authentication_generation

        with self._authentication_lock:
            if not self._authenticated:
                self._authenticate_locked()
            return self._authentication_generation

    @staticmethod
    def _gateway_error_text(exc):
        detail = getattr(exc, "detail_text", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        if isinstance(detail, dict):
            values = (detail.get("message"), detail.get("error"))
            return " ".join(value for value in values
                            if isinstance(value, str))
        if isinstance(detail, str):
            try:
                body = jsonutils.loads(detail)
            except (TypeError, ValueError):
                return detail
            if isinstance(body, dict):
                values = (body.get("message"), body.get("error"))
                message = " ".join(value for value in values
                                   if isinstance(value, str))
                if message:
                    return message
            return detail
        return str(exc)

    @classmethod
    def _is_invalid_auth_token(cls, exc):
        detail = getattr(exc, "detail_text", None)
        if isinstance(detail, dict):
            code = detail.get("code")
            try:
                if int(code) == 16:
                    return True
            except (TypeError, ValueError):
                pass
        return _INVALID_AUTH_TOKEN in cls._gateway_error_text(exc).lower()

    def _refresh_authentication(self, failed_generation):
        with self._authentication_lock:
            # Another request may already have replaced the failed token.
            if (self._authenticated and
                    self._authentication_generation != failed_generation):
                return self._authentication_generation
            return self._authenticate_locked()

    def _call_with_auth_retry(self, callback):
        generation = self._ensure_authenticated()

        def call():
            result = callback()
            if (isinstance(result, dict) and
                    self._is_invalid_auth_token(
                        _GatewayResponseError(result))):
                # etcd3gw raises gateway errors for range requests, but its
                # transaction method can return the same JSON error body as
                # an ordinary result. Normalize both forms before applying
                # the single generation-guarded authentication retry.
                raise _GatewayResponseError(result)
            return result

        try:
            return call()
        except Exception as exc:
            if generation is None or not self._is_invalid_auth_token(exc):
                raise
            self._refresh_authentication(generation)
            return call()

    @staticmethod
    def _json_bytes(value):
        return jsonutils.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @staticmethod
    def _b64(value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _as_bytes(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        raise IDMapIntegrityError(
            reason="etcd returned a non-string value")

    @staticmethod
    def _instance_uuid(value):
        if not isinstance(value, str):
            raise IDMapConfigurationError(
                reason="instance UUID %r is not valid" % value)
        try:
            normalized = str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise IDMapConfigurationError(
                reason="instance UUID %r is not valid" % value)
        if normalized != value:
            raise IDMapConfigurationError(
                reason="instance UUID %r is not canonical" % value)
        return normalized

    @staticmethod
    def _materialization_id(value):
        if not isinstance(value, str):
            raise IDMapConfigurationError(
                reason="materialization ID %r is not valid" % value)
        try:
            normalized = str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise IDMapConfigurationError(
                reason="materialization ID %r is not a UUID" % value)
        if normalized != value:
            raise IDMapConfigurationError(
                reason="materialization ID %r is not canonical" % value)
        return normalized

    @staticmethod
    def _host_id(value):
        if not isinstance(value, str):
            raise IDMapConfigurationError(
                reason="host ID %r is not valid" % value)
        try:
            normalized = str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise IDMapConfigurationError(
                reason="host ID %r is not a compute UUID" % value)
        if normalized != value:
            raise IDMapConfigurationError(
                reason="host ID %r is not a canonical compute UUID" % value)
        return normalized

    @staticmethod
    def _instance_name(value):
        if (not isinstance(value, str) or not value or len(value) > 255 or
                any(ord(character) < 32 for character in value)):
            raise IDMapConfigurationError(
                reason="instance name %r is not valid" % value)
        return value

    @property
    def configuration_key(self):
        return "%s/config" % self._prefix

    @property
    def audit_coordinator_key(self):
        return "%s/coordinator" % self._control_prefix

    @property
    def audit_failure_key(self):
        return "%s/failure" % self._control_prefix

    def _audit_coordinator_raw(self, token, state, generation):
        return self._json_bytes({
            "fingerprint": self.fingerprint,
            "generation": generation,
            "kind": "audit-coordinator",
            "namespace": self.namespace,
            "schema": _AUDIT_CONTROL_SCHEMA,
            "state": state,
            "token": token,
        })

    def _parse_audit_coordinator(self, raw):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
            token = str(uuid.UUID(value["token"]))
            generation = str(uuid.UUID(value["generation"]))
        except (AttributeError, KeyError, TypeError, UnicodeDecodeError,
                ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid audit coordinator record: %s" % exc)
        expected_keys = {
            "fingerprint", "generation", "kind", "namespace", "schema",
            "state", "token",
        }
        if (not isinstance(value, dict) or set(value) != expected_keys or
                value.get("schema") != _AUDIT_CONTROL_SCHEMA or
                value.get("kind") != "audit-coordinator" or
                value.get("namespace") != self.namespace or
                value.get("fingerprint") != self.fingerprint or
                value.get("state") not in _AUDIT_CONTROL_STATES or
                value.get("token") != token or
                value.get("generation") != generation or
                raw != self._audit_coordinator_raw(
                    token, value.get("state"), generation)):
            raise IDMapIntegrityError(
                reason="audit coordinator record is not canonical")
        return value

    def _audit_failure_raw(self, reason):
        reason = str(reason)[:2048]
        return self._json_bytes({
            "fingerprint": self.fingerprint,
            "kind": "audit-failure",
            "namespace": self.namespace,
            "reason": reason,
            "schema": _AUDIT_CONTROL_SCHEMA,
            "token": self._coordinator_token,
        })

    def _parse_audit_failure(self, raw):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
            token = str(uuid.UUID(value["token"]))
        except (AttributeError, KeyError, TypeError, UnicodeDecodeError,
                ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid fleet audit failure record: %s" % exc)
        expected_keys = {
            "fingerprint", "kind", "namespace", "reason", "schema",
            "token",
        }
        if (not isinstance(value, dict) or set(value) != expected_keys or
                value.get("schema") != _AUDIT_CONTROL_SCHEMA or
                value.get("kind") != "audit-failure" or
                value.get("namespace") != self.namespace or
                value.get("fingerprint") != self.fingerprint or
                not isinstance(value.get("reason"), str) or
                not value.get("reason") or value.get("token") != token):
            raise IDMapIntegrityError(
                reason="fleet audit failure record is not canonical")
        canonical = self._json_bytes(dict(value, token=token))
        if raw != canonical:
            raise IDMapIntegrityError(
                reason="fleet audit failure record is not canonical")
        return value

    def instance_key(self, instance_uuid):
        return "%s/instances/%s" % (
            self._prefix, self._instance_uuid(instance_uuid))

    def slot_key(self, slot):
        slot = self._positive_int(slot, "slot", allow_zero=True)
        if slot >= self.count:
            raise IDMapConfigurationError(
                reason="slot %d is outside the configured range" % slot)
        return "%s/slots/%d" % (self._prefix, slot)

    def release_key(self, instance_uuid):
        return "%s/releases/%s" % (
            self._prefix, self._instance_uuid(instance_uuid))

    def host_claim_key(self, host_id, instance_uuid):
        return "%s/hosts/%s/%s" % (
            self._prefix, self._host_id(host_id),
            self._instance_uuid(instance_uuid))

    def fence_key(self, host_id, instance_uuid):
        """Key of the audit ledger entry for one fence-based retirement."""
        return "%s/fences/%s/%s" % (
            self._prefix, self._host_id(host_id),
            self._instance_uuid(instance_uuid))

    def _fence_proof_raw(self, proof, materialization_id):
        # The materialization token of the disposed claim is recorded
        # alongside the operator's evidence, not asked of the operator: it
        # is what lets a later audit tell "the claim we deleted reappeared"
        # (outside mutation) from "the instance was rebuilt on this host
        # after it was repaired" (ordinary, and it must not latch the
        # registry).
        return self._json_bytes({
            "schema": _SCHEMA_VERSION,
            "kind": "fence-retirement",
            "instance_uuid": self._instance_uuid(proof.instance_uuid),
            "host_id": self._host_id(proof.host_id),
            "allocation_id": self._materialization_id(proof.allocation_id),
            "materialization_id": self._materialization_id(
                materialization_id),
            "fence_agent": proof.fence_agent,
            "fenced_at": proof.fenced_at,
            "operator": proof.operator,
            "evidence": proof.evidence,
        })

    def get_fence_proof(self, instance_uuid, host_id):
        """Return one recorded fence retirement, or None."""
        self._ensure_initialized()
        key = self.fence_key(host_id, instance_uuid)
        raw = self._read_exact((key,))[key]
        if raw is None:
            return None
        value = jsonutils.loads(raw.decode("utf-8"))
        return IDMapFenceProof(
            instance_uuid=value["instance_uuid"],
            host_id=value["host_id"],
            allocation_id=value["allocation_id"],
            fence_agent=value["fence_agent"],
            fenced_at=value["fenced_at"],
            operator=value["operator"],
            evidence=value["evidence"])

    def _get_raw(self, key):
        try:
            values = self._call_with_auth_retry(
                lambda: self._client.get(key))
        except Exception as exc:
            raise IDMapBackendError(
                reason="etcd read of %s failed: %s" % (
                    key, self._gateway_error_text(exc)))
        if not values:
            return None
        if len(values) != 1:
            raise IDMapIntegrityError(
                reason="etcd returned multiple values for %s" % key)
        return self._as_bytes(values[0])

    def _get_prefix_raw(self, key_prefix):
        try:
            entries = list(self._call_with_auth_retry(
                lambda: self._client.get_prefix(key_prefix)))
        except Exception as exc:
            raise IDMapBackendError(
                reason="etcd range read of %s failed: %s" % (
                    key_prefix, self._gateway_error_text(exc)))

        result = []
        for entry in entries:
            if (not isinstance(entry, (tuple, list)) or len(entry) != 2 or
                    not isinstance(entry[1], dict) or
                    "key" not in entry[1]):
                raise IDMapIntegrityError(
                    reason="etcd range response contains invalid metadata")
            raw_key = entry[1]["key"]
            if isinstance(raw_key, bytes):
                try:
                    key = raw_key.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise IDMapIntegrityError(
                        reason="etcd returned a non-UTF-8 key: %s" % exc)
            elif isinstance(raw_key, str):
                key = raw_key
            else:
                raise IDMapIntegrityError(
                    reason="etcd returned a non-string key")
            result.append((key, self._as_bytes(entry[0])))
        return result

    def _grant_audit_lease(self):
        try:
            return self._call_with_auth_retry(
                lambda: self._client.lease(self.audit_lease_ttl))
        except Exception as exc:
            raise IDMapBackendError(
                reason="etcd audit lease grant failed: %s" %
                self._gateway_error_text(exc))

    def _refresh_audit_lease(self, lease):
        try:
            ttl = self._call_with_auth_retry(lease.refresh)
        except Exception as exc:
            raise IDMapBackendError(
                reason="etcd audit lease refresh failed: %s" %
                self._gateway_error_text(exc))
        if not isinstance(ttl, int) or ttl <= 0:
            raise IDMapBackendError(
                reason="etcd audit lease expired before refresh")
        return ttl

    def _revoke_audit_lease(self, lease):
        if lease is None:
            return
        try:
            self._call_with_auth_retry(lease.revoke)
        except Exception:
            # A losing candidate's unattached lease expires by itself. A
            # revoke failure must not replace the authoritative CAS result.
            pass

    def _put_with_lease(self, key, raw, lease):
        lease_id = getattr(lease, "id", None)
        if not isinstance(lease_id, int) or lease_id <= 0:
            raise IDMapBackendError(reason="etcd returned an invalid lease")
        return {
            "request_put": {
                "key": self._b64(key),
                "value": self._b64(raw),
                "lease": lease_id,
            },
        }

    def _transaction_response(self, transaction):
        for attempt in range(2):
            try:
                result = self._call_with_auth_retry(
                    lambda: self._client.transaction(transaction))
            except Exception as exc:
                raise IDMapBackendError(
                    reason="etcd transaction failed: %s" %
                    self._gateway_error_text(exc))
            if (isinstance(result, dict) and
                    isinstance(result.get("header"), dict)):
                # etcd3gw intentionally models the gateway's proto3 JSON:
                # ``succeeded`` is optional because false is the protobuf
                # default, and ``responses`` is optional when empty.  Make
                # those defaults explicit before the strict callers parse
                # the selected transaction branch.
                succeeded = result.get("succeeded", False)
                responses = result.get("responses", [])
                if isinstance(succeeded, bool) and isinstance(responses, list):
                    normalized = dict(result)
                    normalized["succeeded"] = succeeded
                    normalized["responses"] = responses
                    return normalized
            if attempt == 0 and self.username:
                # Under a large concurrent burst etcd's gRPC gateway can
                # return an empty/noncanonical JSON body for an expired auth
                # generation instead of its usual code=16 body.  Every
                # allocator transaction is CAS-guarded and callers already
                # resolve a lost response by exact reread, so one
                # generation-serialized replay is safe and bounded.
                generation = self._authentication_generation
                self._refresh_authentication(generation)
                continue
            raise IDMapBackendError(
                reason="etcd returned an invalid transaction response: %r" %
                result)

    def _transaction(self, transaction):
        return bool(self._transaction_response(transaction)["succeeded"])

    def _compare_config(self):
        return {
            "key": self._b64(self.configuration_key),
            "result": "EQUAL",
            "target": "VALUE",
            "value": self._b64(self._configuration_raw),
        }

    def _compare_value(self, key, raw):
        return {
            "key": self._b64(key),
            "result": "EQUAL",
            "target": "VALUE",
            "value": self._b64(raw),
        }

    def _compare_lease(self, key, lease_id):
        lease_id = self._positive_int(lease_id, "lease ID")
        return {
            "key": self._b64(key),
            "result": "EQUAL",
            "target": "LEASE",
            "lease": lease_id,
        }

    def _compare_absent(self, key):
        return {
            "key": self._b64(key),
            "result": "EQUAL",
            "target": "CREATE",
            "create_revision": 0,
        }

    def _put(self, key, raw):
        return {
            "request_put": {
                "key": self._b64(key),
                "value": self._b64(raw),
            },
        }

    def _delete(self, key):
        return {"request_delete_range": {"key": self._b64(key)}}

    def _range(self, key):
        return {"request_range": {"key": self._b64(key)}}

    @staticmethod
    def _prefix_range_end(key_prefix):
        """Return the exclusive end of an etcd prefix range."""
        raw = bytearray(key_prefix.encode("utf-8"))
        while raw:
            if raw[-1] < 0xFF:
                raw[-1] += 1
                return bytes(raw)
            raw.pop()
        raise IDMapConfigurationError(
            reason="cannot range over an empty allocator key prefix")

    def _count_range(self, key_prefix):
        """Return a range request that transfers only the key count."""
        return {
            "request_range": {
                "key": self._b64(key_prefix),
                "range_end": self._b64(self._prefix_range_end(key_prefix)),
                "count_only": True,
            },
        }

    @staticmethod
    def _decode_b64(value, description):
        if not isinstance(value, str):
            raise IDMapIntegrityError(
                reason="etcd returned a non-string %s" % description)
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, TypeError) as exc:
            raise IDMapIntegrityError(
                reason="etcd returned invalid base64 %s: %s" % (
                    description, exc))

    def _parse_transaction_range_entry(self, response, expected_key):
        if not isinstance(response, dict):
            raise IDMapBackendError(
                reason="etcd transaction returned an invalid range response")
        value = response.get("response_range")
        if not isinstance(value, dict):
            raise IDMapBackendError(
                reason="etcd transaction omitted a range response")
        kvs = value.get("kvs", [])
        if not isinstance(kvs, list) or len(kvs) > 1:
            raise IDMapIntegrityError(
                reason="etcd exact range returned multiple values")
        if not kvs:
            return None, 0
        kv = kvs[0]
        if not isinstance(kv, dict) or "key" not in kv or "value" not in kv:
            raise IDMapBackendError(
                reason="etcd transaction returned an invalid key/value")
        key = self._decode_b64(kv["key"], "key")
        if key != expected_key.encode("utf-8"):
            raise IDMapIntegrityError(
                reason="etcd exact range returned another key")
        lease = kv.get("lease", 0)
        if type(lease) not in (int, str):
            raise IDMapIntegrityError(
                reason="etcd exact range returned an invalid lease ID")
        try:
            lease_id = int(lease)
        except ValueError:
            raise IDMapIntegrityError(
                reason="etcd exact range returned an invalid lease ID")
        if lease_id < 0 or (isinstance(lease, str) and
                            lease != str(lease_id)):
            raise IDMapIntegrityError(
                reason="etcd exact range returned a non-canonical lease ID")
        return self._decode_b64(kv["value"], "value"), lease_id

    def _parse_transaction_range(self, response, expected_key):
        raw, unused_lease_id = self._parse_transaction_range_entry(
            response, expected_key)
        return raw

    def _read_exact(self, keys):
        """Read a bounded key set at one etcd transaction revision."""
        keys = tuple(keys)
        if len(set(keys)) != len(keys):
            raise IDMapConfigurationError(
                reason="exact allocator read contains duplicate keys")
        for attempt in range(2):
            transaction = {
                "compare": self._guard_compares(),
                "success": [self._range(key) for key in keys],
                "failure": [self._range(self.configuration_key)],
            }
            result = self._transaction_response(transaction)
            responses = result.get("responses")
            if not isinstance(responses, list):
                raise IDMapBackendError(
                    reason="etcd transaction omitted range responses")
            if result["succeeded"]:
                if len(responses) != len(keys):
                    raise IDMapBackendError(
                        reason=(
                            "etcd exact read returned the wrong response "
                            "count"))
                return {
                    key: self._parse_transaction_range(response, key)
                    for key, response in zip(keys, responses)
                }
            if len(responses) != 1:
                raise IDMapBackendError(
                    reason="etcd configuration check returned invalid state")
            raw = self._parse_transaction_range(
                responses[0], self.configuration_key)
            if raw is None:
                raise IDMapIntegrityError(
                    reason="allocator configuration record is missing")
            self._validate_configuration(raw)
            if attempt == 0 and self._refresh_fleet_read_guard():
                continue
            raise IDMapBackendError(
                reason="allocator configuration compare failed unexpectedly")

    def _refresh_fleet_read_guard(self):
        """Refresh a read guard after a completed fleet audit generation."""
        (coordinator_raw, coordinator_lease_id), (
            failure_raw, failure_lease_id) = self._read_audit_control()
        if failure_raw is not None:
            self._latch_fleet_failure(failure_raw, failure_lease_id)
        if coordinator_raw is None or coordinator_lease_id <= 0:
            raise IDMapBackendError(
                reason="fleet ID-map auditor has no live lease")
        coordinator = self._parse_audit_coordinator(coordinator_raw)
        if coordinator["state"] != "healthy":
            raise IDMapBackendError(
                reason="fleet ID-map audit is in progress")
        if coordinator_raw == self._fleet_health_raw:
            return False
        self._fleet_health_raw = coordinator_raw
        self._fleet_health_lease_id = coordinator_lease_id
        return True

    def _read_audit_control(self):
        """Read lease health and sticky failure at one etcd revision."""
        keys = (self.audit_coordinator_key, self.audit_failure_key)
        transaction = {
            "compare": [self._compare_config()],
            "success": [self._range(key) for key in keys],
            "failure": [self._range(self.configuration_key)],
        }
        result = self._transaction_response(transaction)
        responses = result.get("responses")
        if not isinstance(responses, list):
            raise IDMapBackendError(
                reason="etcd audit control read omitted responses")
        if not result["succeeded"]:
            if len(responses) != 1:
                raise IDMapBackendError(
                    reason="etcd audit control config check is invalid")
            raw = self._parse_transaction_range(
                responses[0], self.configuration_key)
            if raw is None:
                raise IDMapIntegrityError(
                    reason="allocator configuration record is missing")
            self._validate_configuration(raw)
            raise IDMapBackendError(
                reason="allocator configuration compare failed unexpectedly")
        if len(responses) != len(keys):
            raise IDMapBackendError(
                reason="etcd audit control read returned wrong response count")
        return tuple(
            self._parse_transaction_range_entry(response, key)
            for key, response in zip(keys, responses))

    def _latch_fleet_failure(self, raw, lease_id=0):
        failure = self._parse_audit_failure(raw)
        if lease_id != 0:
            reason = "fleet audit failure record must not have a lease"
            if self._integrity_latch is None:
                self._integrity_latch = reason
            raise IDMapIntegrityError(reason=reason)
        if self._integrity_latch is None:
            self._integrity_latch = failure["reason"]
        raise IDMapIntegrityError(
            reason="fleet audit failure is sticky: %s" % failure["reason"])

    def _publish_integrity_failure(self, reason):
        """Persist the first integrity failure outside the legacy prefix."""
        reason = str(reason)
        if self._integrity_latch is None:
            self._integrity_latch = reason
        desired = self._audit_failure_raw(reason)
        transaction = {
            "compare": [
                self._compare_config(),
                self._compare_absent(self.audit_failure_key),
            ],
            "success": [self._put(self.audit_failure_key, desired)],
            "failure": [self._range(self.audit_failure_key)],
        }
        try:
            result = self._transaction_response(transaction)
        except IDMapBackendError:
            # The local process remains latched and a coordinator already in
            # pending state cannot publish healthy. Its lease expiry is the
            # fleet backstop when the failure record cannot be written.
            raise
        if result["succeeded"]:
            return
        responses = result.get("responses")
        if not isinstance(responses, list) or len(responses) != 1:
            raise IDMapBackendError(
                reason="etcd failure publication returned invalid state")
        existing, lease_id = self._parse_transaction_range_entry(
            responses[0], self.audit_failure_key)
        if existing is None:
            raise IDMapBackendError(
                reason="fleet audit failure publication lost its CAS")
        self._parse_audit_failure(existing)
        if lease_id != 0:
            raise IDMapIntegrityError(
                reason="fleet audit failure record must not have a lease")

    def _replace_audit_coordinator(
            self, expected_raw, expected_lease_id, desired_raw, lease):
        if expected_raw is None:
            coordinator_compares = [
                self._compare_absent(self.audit_coordinator_key)]
        else:
            if expected_lease_id is None or expected_lease_id < 0:
                raise IDMapConfigurationError(
                    reason="expected coordinator lease ID is invalid")
            coordinator_compares = [
                self._compare_value(self.audit_coordinator_key, expected_raw),
            ]
            if expected_lease_id == 0:
                coordinator_compares.append({
                    "key": self._b64(self.audit_coordinator_key),
                    "result": "EQUAL",
                    "target": "LEASE",
                    "lease": 0,
                })
            else:
                coordinator_compares.append(self._compare_lease(
                    self.audit_coordinator_key, expected_lease_id))
        transaction = {
            "compare": [
                self._compare_config(),
                self._compare_absent(self.audit_failure_key),
            ] + coordinator_compares,
            "success": [self._put_with_lease(
                self.audit_coordinator_key, desired_raw, lease)],
            "failure": [],
        }
        return self._transaction(transaction)

    def _begin_coordinated_audit(self):
        """Acquire or heartbeat the one lease-backed fleet auditor."""
        (coordinator_raw, coordinator_lease_id), (
            failure_raw, failure_lease_id) = self._read_audit_control()
        if failure_raw is not None:
            self._latch_fleet_failure(failure_raw, failure_lease_id)

        newly_acquired = False
        if coordinator_raw is None or coordinator_lease_id == 0:
            if coordinator_raw is not None:
                # A canonical but unleased control record is not authority.
                # Replace it with pending under a fresh lease and force the
                # same full audit as an ordinary lease-expiry takeover.
                self._parse_audit_coordinator(coordinator_raw)
            lease = self._grant_audit_lease()
            generation = str(uuid.uuid4())
            pending_raw = self._audit_coordinator_raw(
                self._coordinator_token, "pending", generation)
            try:
                acquired = self._replace_audit_coordinator(
                    coordinator_raw, coordinator_lease_id,
                    pending_raw, lease)
            except Exception:
                self._revoke_audit_lease(lease)
                raise
            if not acquired:
                self._revoke_audit_lease(lease)
                return False, None, False
            self._audit_lease = lease
            self._fleet_health_raw = None
            self._fleet_health_lease_id = None
            return True, pending_raw, True

        coordinator = self._parse_audit_coordinator(coordinator_raw)
        if coordinator["token"] != self._coordinator_token:
            return False, None, False
        if self._audit_lease is None:
            # A process can only own the record together with the in-memory
            # lease object that created it. A restarted process has a new
            # token and waits for automatic expiry instead of stealing it.
            raise IDMapBackendError(
                reason="local audit coordinator has no owning lease")
        lease_id = getattr(self._audit_lease, "id", None)
        if (not isinstance(lease_id, int) or lease_id <= 0 or
                coordinator_lease_id != lease_id):
            self._fleet_health_raw = None
            self._fleet_health_lease_id = None
            raise IDMapBackendError(
                reason="local audit coordinator lost its owning lease")

        if coordinator["state"] != "healthy":
            raise IDMapBackendError(
                reason="local audit coordinator is already pending")

        # A routine audit must not stop every lifecycle operation for the
        # duration of an O(G) registry scan.  Mutations remain guarded by the
        # previous healthy generation and their normal exact-record CAS
        # checks.  Audit success rotates that generation atomically; audit
        # failure below replaces it with pending (or publishes a sticky
        # integrity failure) before returning control.
        self._fleet_health_raw = coordinator_raw
        self._fleet_health_lease_id = coordinator_lease_id
        self._refresh_audit_lease(self._audit_lease)
        return True, coordinator_raw, newly_acquired

    def run_coordinated_audit(self, full=False):
        """Heartbeat one fleet auditor and return a full snapshot if run."""
        self._raise_if_integrity_latched()
        try:
            owner, expected_raw, newly_acquired = (
                self._begin_coordinated_audit())
        except IDMapIntegrityError as exc:
            try:
                self._publish_integrity_failure(exc)
            except IDMapBackendError:
                pass
            raise
        if not owner:
            return False, None

        snapshot = None
        try:
            if full or newly_acquired:
                snapshot = self._audit_with_latch()
            else:
                try:
                    self._probe_snapshot()
                except IDMapIntegrityError:
                    # Counts are an escalation signal, not sufficient proof
                    # for a sticky fleet failure by themselves.
                    snapshot = self._audit_with_latch()
        except Exception:
            # A newly acquired coordinator is already pending.  A routine
            # audit retains the previous healthy generation while scanning;
            # replace it on any ambiguous result so no later sensitive caller
            # can keep using that generation.  A published sticky failure
            # makes this CAS fail harmlessly because it compares absence.
            expected = self._parse_audit_coordinator(expected_raw)
            if expected["state"] == "healthy":
                pending_raw = self._audit_coordinator_raw(
                    self._coordinator_token, "pending",
                    str(uuid.uuid4()))
                lease_id = getattr(self._audit_lease, "id", None)
                try:
                    self._replace_audit_coordinator(
                        expected_raw, lease_id, pending_raw,
                        self._audit_lease)
                except IDMapBackendError:
                    pass
            self._fleet_health_raw = None
            self._fleet_health_lease_id = None
            raise

        healthy_raw = self._audit_coordinator_raw(
            self._coordinator_token, "healthy",
            str(uuid.uuid4()))
        lease_id = getattr(self._audit_lease, "id", None)
        if not self._replace_audit_coordinator(
                expected_raw, lease_id, healthy_raw, self._audit_lease):
            raise IDMapBackendError(
                reason="audit coordinator lost ownership before commit")
        self._fleet_health_raw = healthy_raw
        self._fleet_health_lease_id = lease_id
        return True, snapshot

    def _ensure_fleet_healthy(self):
        (coordinator_raw, coordinator_lease_id), (
            failure_raw, failure_lease_id) = self._read_audit_control()
        if failure_raw is not None:
            self._latch_fleet_failure(failure_raw, failure_lease_id)
        if coordinator_raw is not None and coordinator_lease_id > 0:
            coordinator = self._parse_audit_coordinator(coordinator_raw)
            if coordinator["state"] == "healthy":
                self._fleet_health_raw = coordinator_raw
                self._fleet_health_lease_id = coordinator_lease_id
                return
            raise IDMapBackendError(
                reason="fleet ID-map audit is in progress")

        # Lease expiry is both failure detection and election. The first
        # sensitive caller after an absent heartbeat may acquire the key,
        # but must complete a full audit before its own operation proceeds.
        owner, unused_snapshot = self.run_coordinated_audit(full=True)
        if owner:
            return
        (coordinator_raw, coordinator_lease_id), (
            failure_raw, failure_lease_id) = self._read_audit_control()
        if failure_raw is not None:
            self._latch_fleet_failure(failure_raw, failure_lease_id)
        if coordinator_raw is None or coordinator_lease_id <= 0:
            raise IDMapBackendError(
                reason="fleet ID-map auditor has no live lease")
        coordinator = self._parse_audit_coordinator(coordinator_raw)
        if coordinator["state"] != "healthy":
            raise IDMapBackendError(
                reason="fleet ID-map audit is in progress")
        self._fleet_health_raw = coordinator_raw
        self._fleet_health_lease_id = coordinator_lease_id

    def _guard_compares(self):
        if (self._fleet_health_raw is None or
                not isinstance(self._fleet_health_lease_id, int) or
                self._fleet_health_lease_id <= 0):
            raise IDMapBackendError(
                reason="no verified fleet audit generation")
        return [
            self._compare_config(),
            self._compare_absent(self.audit_failure_key),
            self._compare_value(
                self.audit_coordinator_key, self._fleet_health_raw),
            self._compare_lease(
                self.audit_coordinator_key,
                self._fleet_health_lease_id),
        ]

    def _create_configuration(self):
        key = self.configuration_key
        transaction = {
            "compare": [{
                "key": self._b64(key),
                "result": "EQUAL",
                "target": "CREATE",
                "create_revision": 0,
            }],
            "success": [{
                "request_put": {
                    "key": self._b64(key),
                    "value": self._b64(self._configuration_raw),
                },
            }],
            "failure": [],
        }
        try:
            created = self._transaction(transaction)
        except IDMapBackendError:
            # A transport error may happen after etcd committed the request.
            # Re-read before failing so a successful ambiguous write is safe.
            current = self._get_raw(key)
            if current == self._configuration_raw:
                return
            raise
        if not created:
            current = self._get_raw(key)
            if current is None:
                raise IDMapBackendError(
                    reason="configuration create lost without stored state")

    def _validate_configuration(self, raw):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid allocator configuration: %s" % exc)
        if not isinstance(value, dict):
            raise IDMapIntegrityError(
                reason="allocator configuration is not an object")
        expected_keys = {
            "base", "count", "fingerprint", "namespace", "schema", "size",
        }
        if (set(value) != expected_keys or
                type(value["base"]) is not int or
                type(value["count"]) is not int or
                type(value["schema"]) is not int or
                type(value["size"]) is not int or
                not isinstance(value["namespace"], str) or
                not isinstance(value["fingerprint"], str)):
            raise IDMapIntegrityError(
                reason="allocator configuration has an invalid schema")
        fingerprint = value.get("fingerprint")
        unsigned = dict(value)
        unsigned.pop("fingerprint", None)
        actual = hashlib.sha256(self._json_bytes(unsigned)).hexdigest()
        if fingerprint != actual:
            raise IDMapIntegrityError(
                reason="allocator configuration fingerprint is invalid")
        if value != self._configuration:
            raise IDMapConfigurationError(
                reason=("allocator configuration drift for namespace %s" %
                        self.namespace))

    def initialize(self):
        """Validate the immutable namespace configuration.

        Runtime callers must never recreate a missing registry implicitly.
        A missing configuration can mean etcd was wiped while containers
        using its allocations are still alive. Only the explicit bootstrap
        or frozen disaster-recovery workflow may create it.
        """
        raw = self._get_raw(self.configuration_key)
        if raw is None:
            raise IDMapIntegrityError(
                reason=("allocator configuration is missing; explicitly "
                        "bootstrap an empty registry or restore a frozen "
                        "audit"))
        self._validate_configuration(raw)
        self._initialized = True
        return self

    def bootstrap(self):
        """Explicitly create a proven-empty allocator registry.

        The operation is idempotent for an existing healthy registry. If the
        immutable configuration is absent, every other namespace key must be
        absent before the configuration is created with compare-and-swap.
        """
        self._raise_if_integrity_latched()
        namespace_prefix = "%s/" % self._prefix
        entries = self._get_prefix_raw(namespace_prefix)
        if entries:
            if not any(
                    key == self.configuration_key for key, unused_raw in
                    entries):
                raise IDMapIntegrityError(
                    reason=("allocator namespace contains records without "
                            "its immutable configuration"))
            self._audit_with_latch()
            self._initialized = True
            return self

        self._create_configuration()
        self.initialize()
        created_entries = self._get_prefix_raw(namespace_prefix)
        if created_entries != [(
                self.configuration_key, self._configuration_raw)]:
            raise IDMapIntegrityError(
                reason=("allocator namespace changed during empty-registry "
                        "bootstrap"))
        self._audit_with_latch()
        return self

    def _raise_if_integrity_latched(self):
        if self._integrity_latch is not None:
            raise IDMapIntegrityError(
                reason=("allocator integrity latch is set; repair the "
                        "registry and restart this compute: %s" %
                        self._integrity_latch))

    # How long a successful configuration validation may be reused before
    # the next allocator operation re-reads it. Every ownership-changing
    # transaction still carries its own _compare_config() compare-and-swap,
    # which is the actual authority against an outage or operator edit; this
    # bound only limits how stale the read-side belt-and-suspenders check
    # may be, and removes an etcd round trip from every hot-path operation
    # (previously ~a quarter of all allocator etcd traffic).
    _INITIALIZE_REVALIDATE_SECONDS = 5.0

    def _ensure_initialized(self):
        self._raise_if_integrity_latched()
        now = time.monotonic()
        checked = getattr(self, '_initialized_checked_at', None)
        if (not self._initialized or checked is None or
                now - checked >= self._INITIALIZE_REVALIDATE_SECONDS):
            try:
                self.initialize()
            except IDMapIntegrityError as exc:
                if self._integrity_latch is None:
                    self._integrity_latch = str(exc)
                try:
                    self._publish_integrity_failure(exc)
                except IDMapBackendError:
                    pass
                raise
            self._initialized_checked_at = time.monotonic()
        # Lease-backed health is intentionally not cached. Every sensitive
        # operation reads the exact coordinator/failure generation that its
        # subsequent etcd transactions compare atomically.
        self._ensure_fleet_healthy()

    def _assignment_raw(self, instance_uuid, slot, allocation_id,
                        host_ids=()):
        host_ids = tuple(sorted(set(host_ids)))
        for host_id in host_ids:
            self._host_id(host_id)
        value = {
            "allocation_id": allocation_id,
            "base": self.base + slot * self.size,
            "fingerprint": self.fingerprint,
            "host_ids": list(host_ids),
            "instance_uuid": instance_uuid,
            "schema": _SCHEMA_VERSION,
            "size": self.size,
            "slot": slot,
        }
        return self._json_bytes(value)

    def _parse_assignment(self, raw, expected_uuid=None,
                          expected_slot=None):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid allocation record: %s" % exc)
        required = {
            "allocation_id", "base", "fingerprint", "host_ids",
            "instance_uuid", "schema", "size", "slot",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise IDMapIntegrityError(
                reason="allocation record has an invalid schema")
        host_ids = value.get("host_ids")
        if (not isinstance(host_ids, list) or
                any(not isinstance(host_id, str) for host_id in host_ids) or
                host_ids != sorted(set(host_ids))):
            raise IDMapIntegrityError(
                reason="allocation record has invalid host claims")
        try:
            assignment = IDMapAssignment(
                instance_uuid=self._instance_uuid(value["instance_uuid"]),
                base=int(value["base"]),
                size=int(value["size"]),
                slot=int(value["slot"]),
                allocation_id=str(uuid.UUID(value["allocation_id"])),
                fingerprint=str(value["fingerprint"]),
                host_ids=tuple(host_ids),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="allocation record contains invalid values: %s" % exc)
        if (assignment.fingerprint != self.fingerprint or
                value["schema"] != _SCHEMA_VERSION or
                assignment.size != self.size or
                assignment.slot < 0 or assignment.slot >= self.count or
                assignment.base != self.base + assignment.slot * self.size):
            raise IDMapIntegrityError(
                reason="allocation record conflicts with namespace config")
        if expected_uuid and assignment.instance_uuid != expected_uuid:
            raise IDMapIntegrityError(
                reason="allocation record belongs to another instance")
        if expected_slot is not None and assignment.slot != expected_slot:
            raise IDMapIntegrityError(
                reason="allocation record belongs to another slot")
        if raw != self._assignment_raw(
                assignment.instance_uuid, assignment.slot,
                assignment.allocation_id, assignment.host_ids):
            raise IDMapIntegrityError(
                reason="allocation record is not canonical")
        return assignment

    @staticmethod
    def _same_generation(left, right):
        return (
            left.instance_uuid == right.instance_uuid and
            left.base == right.base and
            left.size == right.size and
            left.slot == right.slot and
            left.allocation_id == right.allocation_id and
            left.fingerprint == right.fingerprint
        )

    def _release_intent_raw(self, assignment, instance_name):
        value = {
            "allocation_id": assignment.allocation_id,
            "base": assignment.base,
            "fingerprint": assignment.fingerprint,
            "instance_name": self._instance_name(instance_name),
            "instance_uuid": assignment.instance_uuid,
            "schema": _SCHEMA_VERSION,
            "size": assignment.size,
            "slot": assignment.slot,
        }
        return self._json_bytes(value)

    def _parse_release_intent(self, raw, expected_uuid=None):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid release intent: %s" % exc)
        required = {
            "allocation_id", "base", "fingerprint", "instance_name",
            "instance_uuid", "schema", "size", "slot",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise IDMapIntegrityError(
                reason="release intent has an invalid schema")
        try:
            intent = IDMapReleaseIntent(
                instance_uuid=self._instance_uuid(value["instance_uuid"]),
                instance_name=self._instance_name(value["instance_name"]),
                base=int(value["base"]),
                size=int(value["size"]),
                slot=int(value["slot"]),
                allocation_id=str(uuid.UUID(value["allocation_id"])),
                fingerprint=str(value["fingerprint"]),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="release intent contains invalid values: %s" % exc)
        if (value["schema"] != _SCHEMA_VERSION or
                intent.fingerprint != self.fingerprint or
                intent.size != self.size or intent.slot < 0 or
                intent.slot >= self.count or
                intent.base != self.base + intent.slot * self.size):
            raise IDMapIntegrityError(
                reason="release intent conflicts with namespace config")
        if expected_uuid and intent.instance_uuid != expected_uuid:
            raise IDMapIntegrityError(
                reason="release intent belongs to another instance")
        assignment = IDMapAssignment(
            instance_uuid=intent.instance_uuid,
            base=intent.base,
            size=intent.size,
            slot=intent.slot,
            allocation_id=intent.allocation_id,
            fingerprint=intent.fingerprint,
        )
        if raw != self._release_intent_raw(
                assignment, intent.instance_name):
            raise IDMapIntegrityError(
                reason="release intent is not canonical")
        return intent

    def _host_claim_raw(self, assignment, host_id, materialization_id,
                        state="unmaterialized", proof=None):
        if state not in _CLAIM_STATES:
            raise IDMapConfigurationError(reason="invalid host claim state")
        if (state == "cleaned") != (proof is not None):
            raise IDMapConfigurationError(
                reason="cleaned host claims require exactly one proof")
        value = {
            "allocation_id": assignment.allocation_id,
            "base": assignment.base,
            "fingerprint": assignment.fingerprint,
            "host_id": self._host_id(host_id),
            "instance_uuid": self._instance_uuid(
                assignment.instance_uuid),
            "materialization_id": self._materialization_id(
                materialization_id),
            "proof": self._cleanup_proof_value(proof),
            "schema": _SCHEMA_VERSION,
            "size": assignment.size,
            "slot": assignment.slot,
            "state": state,
        }
        return self._json_bytes(value)

    def _parse_host_claim(self, raw, expected_host_id=None,
                          expected_uuid=None):
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise IDMapIntegrityError(
                reason="invalid host claim index: %s" % exc)
        required = {
            "allocation_id", "base", "fingerprint", "host_id",
            "instance_uuid", "materialization_id", "proof", "schema",
            "size", "slot", "state",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise IDMapIntegrityError(
                reason="host claim index has an invalid schema")
        try:
            claim = IDMapHostClaim(
                host_id=self._host_id(value["host_id"]),
                materialization_id=self._materialization_id(
                    value["materialization_id"]),
                instance_uuid=self._instance_uuid(value["instance_uuid"]),
                base=int(value["base"]),
                size=int(value["size"]),
                slot=int(value["slot"]),
                allocation_id=str(uuid.UUID(value["allocation_id"])),
                fingerprint=str(value["fingerprint"]),
                state=str(value["state"]),
                proof=self._parse_cleanup_proof(value["proof"]),
            )
        except (AttributeError, TypeError, ValueError,
                IDMapConfigurationError) as exc:
            raise IDMapIntegrityError(
                reason="host claim index contains invalid values: %s" % exc)
        if (value["schema"] != _SCHEMA_VERSION or
                claim.fingerprint != self.fingerprint or
                claim.size != self.size or claim.slot < 0 or
                claim.slot >= self.count or
                claim.base != self.base + claim.slot * self.size):
            raise IDMapIntegrityError(
                reason="host claim index conflicts with namespace config")
        if expected_host_id and claim.host_id != expected_host_id:
            raise IDMapIntegrityError(
                reason="host claim index belongs to another host")
        if expected_uuid and claim.instance_uuid != expected_uuid:
            raise IDMapIntegrityError(
                reason="host claim index belongs to another instance")
        if claim.state not in _CLAIM_STATES:
            raise IDMapIntegrityError(reason="host claim state is invalid")
        if (claim.state == "cleaned") != (claim.proof is not None):
            raise IDMapIntegrityError(
                reason="host claim cleanup proof is inconsistent")
        self._validate_claim_proof_binding(claim)
        if raw != self._host_claim_raw(
                claim, claim.host_id, claim.materialization_id,
                state=claim.state, proof=claim.proof):
            raise IDMapIntegrityError(
                reason="host claim index is not canonical")
        return claim

    @staticmethod
    def _receipt_text(value, name, allow_empty=False):
        if (not isinstance(value, str) or
                (not allow_empty and not value) or
                any(ord(character) < 32 for character in value)):
            raise IDMapConfigurationError(
                reason="rootfs release receipt %s is invalid" % name)
        return value

    @staticmethod
    def _receipt_timestamp(value, name):
        if type(value) is not int or value <= 0 or value > 2 ** 63 - 1:
            raise IDMapConfigurationError(
                reason="rootfs release receipt %s is invalid" % name)
        return value

    @staticmethod
    def _sha256_json(value):
        encoded = jsonutils.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:%s" % hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _materialization_digest(cls, proof):
        return cls._sha256_json({
            "token": proof.token,
            "allocation_id": proof.allocation_id,
            "compute_id": proof.compute_id,
            "owner": proof.owner,
            "project": proof.project,
            "instance_name": proof.instance_name,
            "idmap_base": proof.idmap_base,
            "idmap_size": proof.idmap_size,
            "storage_driver": proof.storage_driver,
            "storage_pool": proof.storage_pool,
            "storage_volume": proof.storage_volume,
            "rbd_image": proof.rbd_image,
            "storage_identity": proof.storage_identity,
            "baseline_clean": proof.baseline_clean,
            "cleanup_disposition": proof.cleanup_disposition,
            "outcome": proof.outcome,
        })

    @classmethod
    def _release_digest(cls, proof):
        value = {
            "digest": "",
            "token": proof.token,
            "allocation_id": proof.allocation_id,
            "compute_id": proof.compute_id,
            "materialization_id": proof.materialization_id,
            "owner": proof.owner,
            "project": proof.project,
            "instance_name": proof.instance_name,
            "idmap_base": proof.idmap_base,
            "idmap_size": proof.idmap_size,
            "storage_driver": proof.storage_driver,
            "storage_pool": proof.storage_pool,
            "storage_volume": proof.storage_volume,
        }
        if proof.rbd_image:
            value["rbd_image"] = proof.rbd_image
        if proof.storage_identity:
            value["storage_identity"] = proof.storage_identity
        value.update({
            "baseline_clean": proof.baseline_clean,
            "cleanup_disposition": proof.cleanup_disposition,
            "outcome": proof.outcome,
            "state": proof.state,
            "created_at": proof.created_at,
            "completed_at": proof.completed_at,
        })
        return cls._sha256_json(value)

    def _proof_value(self, proof):
        value = dict(proof.__dict__)
        if isinstance(proof, IDMapMaterializationProof):
            value["kind"] = "materialization-attempt"
        elif isinstance(proof, IDMapRootfsReleaseProof):
            value["kind"] = "storage-release-receipt"
        else:
            raise IDMapConfigurationError(
                reason="host claim proof has an invalid type")
        return value

    def _cleanup_proof_value(self, proof):
        return None if proof is None else self._proof_value(proof)

    def _parse_cleanup_proof(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise IDMapIntegrityError(
                reason="host claim proof has an invalid schema")
        fields = dict(value)
        kind = fields.pop("kind", None)
        try:
            if kind == "materialization-attempt":
                proof = IDMapMaterializationProof(**fields)
                self._validate_materialization_proof(proof)
                return proof
            if kind == "storage-release-receipt":
                proof = IDMapRootfsReleaseProof(**fields)
                self._validate_release_proof(proof)
                return proof
        except TypeError as exc:
            raise IDMapIntegrityError(
                reason="host claim proof has an invalid schema: %s" % exc)
        raise IDMapIntegrityError(reason="host claim proof kind is invalid")

    @classmethod
    def _validate_materialization_proof(cls, proof):
        if not isinstance(proof, IDMapMaterializationProof):
            raise IDMapConfigurationError(
                reason="materialization proof has an invalid type")
        for value in (proof.token, proof.allocation_id, proof.compute_id,
                      proof.owner):
            cls._materialization_id(value)
        for name in ("project", "storage driver", "storage pool",
                     "storage volume"):
            attribute = name.replace(" ", "_")
            cls._receipt_text(getattr(proof, attribute), name)
        cls._instance_name(proof.instance_name)
        cls._receipt_text(proof.rbd_image, "RBD image", allow_empty=True)
        cls._receipt_text(
            proof.storage_identity, "storage identity", allow_empty=True)
        if proof.baseline_clean is not True:
            raise IDMapConfigurationError(
                reason="materialization proof lacks a clean baseline")
        if (type(proof.idmap_base) is not int or
                type(proof.idmap_size) is not int or
                proof.idmap_base < 0 or proof.idmap_size <= 0 or
                proof.idmap_base + proof.idmap_size > 2 ** 32):
            raise IDMapConfigurationError(
                reason="materialization proof ID map is invalid")
        if proof.cleanup_disposition not in (
                "delete", "detach", "handover"):
            raise IDMapConfigurationError(
                reason="materialization cleanup disposition is invalid")
        if (proof.storage_driver == "cephext" and
                proof.cleanup_disposition != "detach"):
            raise IDMapConfigurationError(
                reason="cephext materialization must use detach cleanup")
        if (proof.cleanup_disposition == "detach" and
                proof.storage_driver not in ("ceph", "cephext")):
            raise IDMapConfigurationError(
                reason="detached materialization requires Ceph storage")
        if (proof.cleanup_disposition == "handover" and
                proof.storage_driver != "ceph"):
            raise IDMapConfigurationError(
                reason="handover materialization requires Ceph storage")
        if (proof.cleanup_disposition in ("detach", "handover") and
                not proof.storage_identity):
            raise IDMapConfigurationError(
                reason="shared materialization lacks immutable identity")
        if proof.outcome not in _MATERIALIZATION_OUTCOMES:
            raise IDMapConfigurationError(
                reason="materialization proof outcome is invalid")
        if proof.outcome == "not-materialized":
            valid_terminal = (
                proof.state == "aborted" and
                proof.storage_phase == "none" and
                proof.started is False and proof.finished is True and
                (proof.cleanup_disposition in ("detach", "handover") or
                 not proof.storage_identity))
        else:
            valid_terminal = (
                proof.state == "clean" and
                proof.storage_phase == "clean" and
                proof.started is True and proof.finished is True)
        if not valid_terminal:
            raise IDMapConfigurationError(
                reason="materialization proof is not terminal")
        # A shared disposition must always name the volume it left behind,
        # which the detach/handover check above enforces unconditionally.
        # For a delete disposition an empty identity is the server stating
        # that nothing was ever materialized: there is no volume to name and
        # none was removed. Rejecting that stranded exactly the failed-build
        # claims this proof exists to release.
        if (proof.outcome == "reconciled-clean" and
                proof.cleanup_disposition in ("detach", "handover") and
                proof.storage_driver in ("ceph", "cephext") and
                not proof.storage_identity):
            raise IDMapConfigurationError(
                reason="Ceph materialization lacks immutable identity")
        if (not _RECEIPT_DIGEST_RE.fullmatch(proof.digest) or
                proof.digest != cls._materialization_digest(proof)):
            raise IDMapConfigurationError(
                reason="materialization proof digest is invalid")

    @classmethod
    def _validate_release_proof(cls, proof):
        if not isinstance(proof, IDMapRootfsReleaseProof):
            raise IDMapConfigurationError(
                reason="rootfs release proof has an invalid type")
        for value in (proof.token, proof.allocation_id, proof.compute_id,
                      proof.materialization_id, proof.owner):
            cls._materialization_id(value)
        if proof.token != proof.materialization_id:
            raise IDMapConfigurationError(
                reason="release token does not match materialization ID")
        cls._instance_name(proof.instance_name)
        for name in ("project", "storage driver", "storage pool",
                     "storage volume"):
            attribute = name.replace(" ", "_")
            cls._receipt_text(getattr(proof, attribute), name)
        cls._receipt_text(proof.rbd_image, "RBD image", allow_empty=True)
        cls._receipt_text(
            proof.storage_identity, "storage identity", allow_empty=True)
        if proof.baseline_clean is not True:
            raise IDMapConfigurationError(
                reason="rootfs release proof lacks a clean baseline")
        if proof.cleanup_disposition not in (
                "delete", "detach", "handover"):
            raise IDMapConfigurationError(
                reason="rootfs release cleanup disposition is invalid")
        if (type(proof.idmap_base) is not int or
                type(proof.idmap_size) is not int or
                proof.idmap_base < 0 or proof.idmap_size <= 0 or
                proof.idmap_base + proof.idmap_size > 2 ** 32):
            raise IDMapConfigurationError(
                reason="rootfs release proof ID map is invalid")
        if (proof.outcome not in _RELEASE_OUTCOMES or
                proof.state != "complete" or
                type(proof.created_at) is not int or
                type(proof.completed_at) is not int or
                proof.created_at <= 0 or
                proof.completed_at < proof.created_at):
            raise IDMapConfigurationError(
                reason="rootfs release proof is not complete")
        if (proof.storage_driver in ("ceph", "cephext") and
                not proof.storage_identity):
            raise IDMapConfigurationError(
                reason="Ceph rootfs release proof lacks immutable identity")
        if (proof.storage_driver == "cephext" and
                proof.cleanup_disposition != "detach"):
            raise IDMapConfigurationError(
                reason="cephext rootfs release must use detach cleanup")
        if (proof.cleanup_disposition == "detach" and
                proof.storage_driver not in ("ceph", "cephext")):
            raise IDMapConfigurationError(
                reason="detached rootfs release requires Ceph storage")
        if (proof.cleanup_disposition == "handover" and
                proof.storage_driver != "ceph"):
            raise IDMapConfigurationError(
                reason="handover rootfs release requires Ceph storage")
        if proof.storage_driver == "cephext":
            valid_outcomes = frozenset(("normalized", "detached"))
        elif proof.storage_driver == "ceph":
            valid_outcomes = {
                "delete": frozenset(("deleted", "detached")),
                "detach": frozenset(("detached",)),
                "handover": frozenset(("deleted", "detached")),
            }[proof.cleanup_disposition]
        else:
            valid_outcomes = frozenset(("deleted",))
        if proof.outcome not in valid_outcomes:
            raise IDMapConfigurationError(
                reason="rootfs release outcome does not match its immutable "
                       "storage cleanup disposition")
        if (not _RECEIPT_DIGEST_RE.fullmatch(proof.digest) or
                proof.digest != cls._release_digest(proof)):
            raise IDMapConfigurationError(
                reason="rootfs release proof digest is invalid")

    def _validate_claim_proof_binding(self, claim):
        proof = claim.proof
        if proof is None:
            return
        if (proof.token != claim.materialization_id or
                proof.allocation_id != claim.allocation_id or
                proof.compute_id != claim.host_id or
                proof.owner != claim.instance_uuid or
                proof.idmap_base != claim.base or
                proof.idmap_size != claim.size):
            raise IDMapIntegrityError(
                reason="host claim proof belongs to another generation")

    @classmethod
    def _proof_from_receipt(cls, receipt):
        if not isinstance(receipt, IDMapRootfsReleaseReceipt):
            raise IDMapConfigurationError(
                reason="rootfs release receipt has an invalid type")
        proof = IDMapRootfsReleaseProof(**receipt.__dict__)
        cls._validate_release_proof(proof)
        return proof

    def _read_assignment_state(self, instance_uuid):
        """Read one verified generation and its bounded companion records."""
        instance_key = self.instance_key(instance_uuid)
        release_key = self.release_key(instance_uuid)
        for unused_attempt in range(16):
            first = self._read_exact((instance_key, release_key))
            instance_raw = first[instance_key]
            if instance_raw is None:
                if first[release_key] is not None:
                    raise IDMapIntegrityError(
                        reason="release state exists without an allocation")
                return None, None, None, None, {}, {}

            assignment = self._parse_assignment(
                instance_raw, expected_uuid=instance_uuid)
            host_keys = tuple(
                self.host_claim_key(host_id, instance_uuid)
                for host_id in assignment.host_ids)
            slot_key = self.slot_key(assignment.slot)
            keys = (instance_key, slot_key, release_key) + host_keys
            current = self._read_exact(keys)
            if current[instance_key] != instance_raw:
                continue
            if current[slot_key] != instance_raw:
                raise IDMapIntegrityError(
                    reason="instance allocation has no exact slot record")
            claims = {}
            claim_raws = {}
            for host_id, host_key in zip(assignment.host_ids, host_keys):
                claim_raw = current[host_key]
                if claim_raw is None:
                    raise IDMapIntegrityError(
                        reason=("allocation host claim has no exact reverse "
                                "index"))
                claim = self._parse_host_claim(
                    claim_raw, expected_host_id=host_id,
                    expected_uuid=instance_uuid)
                if not self._same_generation(claim, assignment):
                    raise IDMapIntegrityError(
                        reason="host claim has another allocation generation")
                claims[host_id] = claim
                claim_raws[host_id] = claim_raw

            intent_raw = current[release_key]
            intent = None
            if intent_raw is not None:
                intent = self._parse_release_intent(
                    intent_raw, expected_uuid=instance_uuid)
                intent_assignment = IDMapAssignment(
                    instance_uuid=intent.instance_uuid,
                    base=intent.base,
                    size=intent.size,
                    slot=intent.slot,
                    allocation_id=intent.allocation_id,
                    fingerprint=intent.fingerprint,
                )
                if not self._same_generation(assignment, intent_assignment):
                    raise IDMapIntegrityError(
                        reason="release intent does not match its allocation")

            return (
                assignment, instance_raw, intent, intent_raw,
                claims, claim_raws)
        raise IDMapConflict(
            reason="allocator generation changed during exact read")

    def _get_assignment(self, instance_uuid):
        assignment, raw, unused_intent, unused_intent_raw, unused_claims, \
            unused_claim_raws = self._read_assignment_state(instance_uuid)
        return assignment, raw

    def _get_allocatable_assignment(self, instance_uuid):
        current, raw, intent, unused_intent_raw, unused_claims, \
            unused_claim_raws = (
                self._read_assignment_state(instance_uuid))
        if current is not None and intent is not None:
            raise IDMapConflict(
                reason="release barrier blocks allocation reuse")
        return current, raw

    def get(self, instance_uuid):
        """Return the verified allocation for an instance, if present."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        assignment, unused_raw = self._get_assignment(instance_uuid)
        return assignment

    def _audit_records(self):
        namespace_prefix = "%s/" % self._prefix
        entries = self._get_prefix_raw(namespace_prefix)
        records = {}
        for key, raw in entries:
            if not key.startswith(namespace_prefix):
                raise IDMapIntegrityError(
                    reason="etcd returned a key outside the namespace")
            if key in records:
                raise IDMapIntegrityError(
                    reason="etcd returned duplicate key %s" % key)
            records[key] = raw

        configuration_raw = records.pop(self.configuration_key, None)
        if configuration_raw is None:
            raise IDMapIntegrityError(
                reason="allocator configuration record is missing")
        self._validate_configuration(configuration_raw)
        return records

    def _audit_parse_instance_record(
            self, key, raw, prefix, instances, instance_raw):
        key_uuid = key[len(prefix):]
        try:
            normalized_uuid = self._instance_uuid(key_uuid)
        except IDMapConfigurationError as exc:
            raise IDMapIntegrityError(
                reason="invalid instance allocation key %s: %s" % (
                    key, exc))
        if key_uuid != normalized_uuid:
            raise IDMapIntegrityError(
                reason="instance allocation key is not canonical")
        assignment = self._parse_assignment(
            raw, expected_uuid=normalized_uuid)
        if normalized_uuid in instances:
            raise IDMapIntegrityError(
                reason="duplicate instance allocation")
        instances[normalized_uuid] = assignment
        instance_raw[normalized_uuid] = raw

    def _audit_parse_slot_record(
            self, key, raw, prefix, slots, slot_raw):
        key_slot = key[len(prefix):]
        if not re.fullmatch(r"0|[1-9][0-9]*", key_slot):
            raise IDMapIntegrityError(
                reason="slot allocation key is not canonical")
        slot = int(key_slot)
        if slot >= self.count:
            raise IDMapIntegrityError(
                reason="slot allocation key is outside the range")
        assignment = self._parse_assignment(raw, expected_slot=slot)
        if slot in slots:
            raise IDMapIntegrityError(reason="duplicate slot allocation")
        slots[slot] = assignment
        slot_raw[slot] = raw

    def _audit_parse_release_record(
            self, key, raw, prefix, intents):
        key_uuid = key[len(prefix):]
        try:
            normalized_uuid = self._instance_uuid(key_uuid)
        except IDMapConfigurationError as exc:
            raise IDMapIntegrityError(
                reason="invalid release intent key %s: %s" % (key, exc))
        if key_uuid != normalized_uuid:
            raise IDMapIntegrityError(
                reason="release intent key is not canonical")
        intent = self._parse_release_intent(
            raw, expected_uuid=normalized_uuid)
        if normalized_uuid in intents:
            raise IDMapIntegrityError(reason="duplicate release intent")
        intents[normalized_uuid] = intent

    def _audit_parse_host_record(
            self, key, raw, prefix, host_claims):
        suffix = key[len(prefix):]
        parts = suffix.split("/")
        if len(parts) != 2:
            raise IDMapIntegrityError(
                reason="host claim index key is not canonical")
        key_host_id, key_uuid = parts
        try:
            normalized_host_id = self._host_id(key_host_id)
            normalized_uuid = self._instance_uuid(key_uuid)
        except IDMapConfigurationError as exc:
            raise IDMapIntegrityError(
                reason="invalid host claim index key %s: %s" % (key, exc))
        if (key_host_id != normalized_host_id or
                key_uuid != normalized_uuid):
            raise IDMapIntegrityError(
                reason="host claim index key is not canonical")
        claim = self._parse_host_claim(
            raw, expected_host_id=normalized_host_id,
            expected_uuid=normalized_uuid)
        claim_key = (normalized_host_id, normalized_uuid)
        if claim_key in host_claims:
            raise IDMapIntegrityError(
                reason="duplicate host claim index")
        host_claims[claim_key] = claim

    def _audit_parse_fence_record(
            self, key, raw, prefix, fenced_tokens):
        suffix = key[len(prefix):]
        parts = suffix.split("/")
        if len(parts) != 2:
            raise IDMapIntegrityError(
                reason="fence ledger key is not canonical")
        key_host_id, key_uuid = parts
        try:
            normalized_host_id = self._host_id(key_host_id)
            normalized_uuid = self._instance_uuid(key_uuid)
        except IDMapConfigurationError as exc:
            raise IDMapIntegrityError(
                reason="invalid fence ledger key %s: %s" % (key, exc))
        if (key_host_id != normalized_host_id or
                key_uuid != normalized_uuid):
            raise IDMapIntegrityError(
                reason="fence ledger key is not canonical")
        try:
            value = jsonutils.loads(raw.decode("utf-8"))
            proof = IDMapFenceProof(
                instance_uuid=value["instance_uuid"],
                host_id=value["host_id"],
                allocation_id=value["allocation_id"],
                fence_agent=value["fence_agent"],
                fenced_at=value["fenced_at"],
                operator=value["operator"],
                evidence=value["evidence"])
            validate_fence_proof(proof)
            # Ledger entries written before the token was recorded cannot say
            # which claim they disposed of. They do not participate in the
            # coexistence check; rejecting them would latch the allocator.
            legacy_token = value.get("materialization_id")
            fenced_token = (
                self._materialization_id(legacy_token)
                if legacy_token else None)
        except (ValueError, KeyError, TypeError,
                IDMapConfigurationError) as exc:
            raise IDMapIntegrityError(
                reason="invalid fence ledger record %s: %s" % (key, exc))
        if (proof.host_id != normalized_host_id or
                proof.instance_uuid != normalized_uuid):
            raise IDMapIntegrityError(
                reason="fence ledger record contradicts its key")
        fenced_tokens[(normalized_host_id, normalized_uuid)] = fenced_token

    def _audit_parse_record_families(self, records):
        prefixes = {
            "instance": "%s/instances/" % self._prefix,
            "slot": "%s/slots/" % self._prefix,
            "release": "%s/releases/" % self._prefix,
            "host": "%s/hosts/" % self._prefix,
            "fence": "%s/fences/" % self._prefix,
        }
        instances = {}
        slots = {}
        intents = {}
        host_claims = {}
        fenced_tokens = {}
        instance_raw = {}
        slot_raw = {}
        for key, raw in records.items():
            if key.startswith(prefixes["instance"]):
                self._audit_parse_instance_record(
                    key, raw, prefixes["instance"], instances, instance_raw)
            elif key.startswith(prefixes["slot"]):
                self._audit_parse_slot_record(
                    key, raw, prefixes["slot"], slots, slot_raw)
            elif key.startswith(prefixes["release"]):
                self._audit_parse_release_record(
                    key, raw, prefixes["release"], intents)
            elif key.startswith(prefixes["host"]):
                self._audit_parse_host_record(
                    key, raw, prefixes["host"], host_claims)
            elif key.startswith(prefixes["fence"]):
                self._audit_parse_fence_record(
                    key, raw, prefixes["fence"], fenced_tokens)
            else:
                raise IDMapIntegrityError(
                    reason="unexpected allocator key %s" % key)
        return (
            instances, slots, intents, host_claims, fenced_tokens,
            instance_raw, slot_raw)

    @staticmethod
    def _audit_validate_fence_relationships(fenced_tokens, host_claims):
        # fence_retire_claim writes the ledger and deletes that exact claim in
        # one transaction. The same materialization token cannot coexist.
        for fence_pair, fenced_token in fenced_tokens.items():
            if fenced_token is None:
                continue
            claim = host_claims.get(fence_pair)
            if claim is not None and claim.materialization_id == fenced_token:
                raise IDMapIntegrityError(
                    reason="fence ledger entry coexists with the live host "
                           "claim it disposed of")

    @staticmethod
    def _audit_validate_slot_relationships(
            instances, slots, instance_raw, slot_raw):
        claimed_slots = {}
        for instance_uuid, assignment in instances.items():
            previous = claimed_slots.get(assignment.slot)
            if previous is not None and previous != instance_uuid:
                raise IDMapIntegrityError(
                    reason="multiple instances claim one ID-map slot")
            claimed_slots[assignment.slot] = instance_uuid
            matching = slots.get(assignment.slot)
            if (matching is None or matching != assignment or
                    slot_raw[assignment.slot] != instance_raw[instance_uuid]):
                raise IDMapIntegrityError(
                    reason="instance allocation has no exact slot record")

        claimed_instances = {}
        for slot, assignment in slots.items():
            previous = claimed_instances.get(assignment.instance_uuid)
            if previous is not None and previous != slot:
                raise IDMapIntegrityError(
                    reason="multiple slots claim one instance")
            claimed_instances[assignment.instance_uuid] = slot
            matching = instances.get(assignment.instance_uuid)
            if (matching is None or matching != assignment or
                    instance_raw[assignment.instance_uuid] != slot_raw[slot]):
                raise IDMapIntegrityError(
                    reason="slot allocation has no exact instance record")

    def _audit_validate_release_relationships(self, instances, intents):
        for instance_uuid, intent in intents.items():
            assignment = instances.get(instance_uuid)
            if assignment is None:
                raise IDMapIntegrityError(
                    reason="release intent has no allocation")
            intent_assignment = IDMapAssignment(
                instance_uuid=intent.instance_uuid,
                base=intent.base,
                size=intent.size,
                slot=intent.slot,
                allocation_id=intent.allocation_id,
                fingerprint=intent.fingerprint,
            )
            if not self._same_generation(assignment, intent_assignment):
                raise IDMapIntegrityError(
                    reason="release intent does not match its allocation")

    def _audit_validate_host_relationships(self, instances, host_claims):
        for instance_uuid, assignment in instances.items():
            for host_id in assignment.host_ids:
                claim = host_claims.get((host_id, instance_uuid))
                if (claim is None or
                        not self._same_generation(claim, assignment)):
                    raise IDMapIntegrityError(
                        reason=("allocation host claim has no exact reverse "
                                "index"))

        for (host_id, instance_uuid), claim in host_claims.items():
            assignment = instances.get(instance_uuid)
            if (assignment is None or host_id not in assignment.host_ids or
                    not self._same_generation(claim, assignment)):
                raise IDMapIntegrityError(
                    reason=("host claim reverse index has no exact "
                            "allocation"))

    def _audit_snapshot(self):
        """Read and validate one complete allocator snapshot.

        Unlike normal allocation operations, audit is strictly read-only. It
        will not create a missing configuration record. ``get_prefix`` maps to
        one etcd range request, so all instance and slot records are examined
        at one linearizable revision.
        """
        records = self._audit_records()

        (instances, slots, intents, host_claims, fenced_tokens,
         instance_raw, slot_raw) = self._audit_parse_record_families(records)

        self._audit_validate_fence_relationships(
            fenced_tokens, host_claims)

        self._audit_validate_slot_relationships(
            instances, slots, instance_raw, slot_raw)

        self._audit_validate_release_relationships(instances, intents)

        self._audit_validate_host_relationships(instances, host_claims)

        return (
            sorted(instances.values(), key=lambda value: value.slot),
            sorted(intents.values(), key=lambda value: value.slot),
            sorted(
                host_claims.values(),
                key=lambda value: (
                    value.slot, value.instance_uuid, value.host_id)),
        )

    def _audit_with_latch(self):
        """Run a full audit and permanently latch any integrity failure."""
        try:
            return self._audit_snapshot()
        except IDMapIntegrityError as exc:
            if self._integrity_latch is None:
                self._integrity_latch = str(exc)
            try:
                self._publish_integrity_failure(exc)
            except IDMapBackendError:
                pass
            raise

    def audit(self):
        """Return assignments after one complete, read-only audit."""
        assignments, unused_intents, unused_claims = self._audit_with_latch()
        return assignments

    def enumerate(self):
        """Compatibility alias for a complete, read-only registry audit."""
        return self.audit()

    def list_release_intents(self):
        """Return immutable shared release intents after a full audit."""
        unused_assignments, intents, unused_claims = self._audit_with_latch()
        return intents

    def audit_state(self):
        """Return assignments, intents and claims from one revision."""
        return self._audit_with_latch()

    def probe(self):
        """Run a guarded count-only drift probe."""
        self._ensure_initialized()
        return self._probe_snapshot(guarded=True)

    def _probe_snapshot(self, guarded=False):
        """Return key-family counts and check their exact relationships.

        A full audit reads and parses every record in the namespace, which
        is the whole steady-state cost of running one on every compute
        every minute. This reads only counts, so an idle compute can check
        the invariants that relate family sizes on every cycle and pay for
        the authoritative scan rarely.

        The relationships checked here are exact, not heuristic: every
        count comes from one transaction revision, allocation writes its
        instance and slot records in a single transaction, and release
        deletes both together with the intent. A probe therefore cannot
        report a mismatch that a concurrent mutation merely happens to be
        halfway through.

        It cannot see any record's *content*, so it does not replace the
        audit: a claim without its allocation, or a generation mismatch
        inside a record, is visible only to a full scan. Callers escalate a
        mismatch to one rather than latching from counts alone.
        """
        families = ("instances", "slots", "releases", "hosts")
        transaction = {
            "compare": (
                self._guard_compares()
                if guarded else [self._compare_config()]),
            "success": [
                self._count_range("%s/%s/" % (self._prefix, family))
                for family in families
            ],
            "failure": [self._range(self.configuration_key)],
        }
        result = self._transaction_response(transaction)
        responses = result.get("responses")
        if not isinstance(responses, list):
            raise IDMapBackendError(
                reason="etcd probe omitted range responses")
        if not result["succeeded"]:
            # The configuration record is immutable for the lifetime of a
            # namespace, so a failed compare means the registry was
            # replaced underneath this process.
            raise IDMapIntegrityError(
                reason="allocator configuration record changed")
        if len(responses) != len(families):
            raise IDMapBackendError(
                reason="etcd probe returned the wrong response count")

        counts = {}
        for family, response in zip(families, responses):
            if not isinstance(response, dict):
                raise IDMapBackendError(
                    reason="etcd probe returned an invalid range response")
            value = response.get("response_range")
            if not isinstance(value, dict):
                raise IDMapBackendError(
                    reason="etcd probe omitted a range response")
            try:
                counts[family] = int(value.get("count", 0))
            except (TypeError, ValueError):
                raise IDMapBackendError(
                    reason="etcd probe returned a non-numeric count for %s"
                    % family)
            if counts[family] < 0:
                raise IDMapBackendError(
                    reason="etcd probe returned a negative count for %s"
                    % family)

        if counts["instances"] != counts["slots"]:
            raise IDMapIntegrityError(
                reason=("allocation and slot record counts differ (%d vs %d)"
                        % (counts["instances"], counts["slots"])))
        if counts["releases"] > counts["instances"]:
            raise IDMapIntegrityError(
                reason=("release intent count exceeds allocation count "
                        "(%d > %d)"
                        % (counts["releases"], counts["instances"])))
        if counts["instances"] == 0 and counts["hosts"]:
            raise IDMapIntegrityError(
                reason=("host claims exist without any allocation (%d)"
                        % counts["hosts"]))
        return counts

    def get_release_intent(self, instance_uuid):
        """Return one exact, verified release intent without a prefix scan."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        unused_assignment, unused_raw, intent, unused_intent_raw, \
            unused_claims, unused_claim_raws = self._read_assignment_state(
                instance_uuid)
        return intent

    def get_host_claim(self, instance_uuid, host_id):
        """Return one exact host/materialization claim, if present."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        unused_assignment, unused_raw, unused_intent, unused_intent_raw, \
            claims, unused_claim_raws = self._read_assignment_state(
                instance_uuid)
        return claims.get(host_id)

    def list_release_intent_candidates(self):
        """Return release candidates without scanning every allocation.

        Periodic workers use this prefix-only lookup to avoid a fleet-wide
        full-registry scan when no deletion is pending. Any subsequent
        ownership mutation still performs authoritative exact-record reads
        and an exact-generation compare-and-swap.
        """
        self._ensure_initialized()
        prefix = "%s/releases/" % self._prefix
        intents = []
        seen = set()
        for key, raw in self._get_prefix_raw(prefix):
            key_uuid = key[len(prefix):] if key.startswith(prefix) else ""
            try:
                normalized_uuid = self._instance_uuid(key_uuid)
            except IDMapConfigurationError as exc:
                raise IDMapIntegrityError(
                    reason="invalid release intent key %s: %s" % (
                        key, exc))
            if key_uuid != normalized_uuid or normalized_uuid in seen:
                raise IDMapIntegrityError(
                    reason="release intent key is not canonical")
            intents.append(self._parse_release_intent(
                raw, expected_uuid=normalized_uuid))
            seen.add(normalized_uuid)
        return sorted(
            intents, key=lambda value: (value.slot, value.instance_uuid))

    def list_release_intents_for_instances(self, instance_uuids):
        """Read release intents for a bounded exact UUID set in one txn.

        A compute already has a reverse index of the allocations it may need
        to reconcile under ``hosts/<host_id>/``.  Reading the corresponding
        exact release keys as one transaction avoids making every compute
        scan the fleet-wide ``releases/`` prefix.  The returned values are
        candidate-discovery hints only; every replay still re-reads the exact
        assignment, claim and intent before its compare-and-swap.
        """
        self._ensure_initialized()
        instance_uuids = tuple(sorted({
            self._instance_uuid(value) for value in instance_uuids
        }))
        if not instance_uuids:
            return []
        keys = tuple(self.release_key(value) for value in instance_uuids)
        state = self._read_exact(keys)
        intents = []
        for instance_uuid, key in zip(instance_uuids, keys):
            raw = state[key]
            if raw is None:
                continue
            intents.append(self._parse_release_intent(
                raw, expected_uuid=instance_uuid))
        return sorted(
            intents, key=lambda value: (value.slot, value.instance_uuid))

    def list_host_claims(self, host_id):
        """Return one compute's indexed claim candidates efficiently.

        This is a prefix-only reverse lookup for periodic reconciliation. A
        subsequent ownership mutation still performs a complete authoritative
        audit and exact compare-and-swap before changing any record.
        """
        self._ensure_initialized()
        host_id = self._host_id(host_id)
        prefix = "%s/hosts/%s/" % (self._prefix, host_id)
        claims = []
        seen = set()
        for key, raw in self._get_prefix_raw(prefix):
            key_uuid = key[len(prefix):] if key.startswith(prefix) else ""
            try:
                normalized_uuid = self._instance_uuid(key_uuid)
            except IDMapConfigurationError as exc:
                raise IDMapIntegrityError(
                    reason="invalid indexed host claim key %s: %s" % (
                        key, exc))
            if key_uuid != normalized_uuid or normalized_uuid in seen:
                raise IDMapIntegrityError(
                    reason="indexed host claim key is not canonical")
            claims.append(self._parse_host_claim(
                raw, expected_host_id=host_id,
                expected_uuid=normalized_uuid))
            seen.add(normalized_uuid)
        return sorted(
            claims, key=lambda value: (value.slot, value.instance_uuid))

    def _preferred_slot(self, preferred_base, preferred_size):
        if (preferred_size is not None and
                self._positive_int(preferred_size, "preferred size") !=
                self.size):
            raise IDMapConflict(
                reason=("preferred ID-map size %s differs from configured %s" %
                        (preferred_size, self.size)))
        preferred_base = self._positive_int(
            preferred_base, "preferred base")
        offset = preferred_base - self.base
        if offset < 0 or offset % self.size:
            raise IDMapConflict(
                reason="preferred ID-map base is outside the allocator slots")
        slot = offset // self.size
        if slot >= self.count:
            raise IDMapConflict(
                reason="preferred ID-map base is outside the allocator range")
        return slot

    def _try_allocate(self, instance_uuid, slot, allocation_id):
        instance_key = self.instance_key(instance_uuid)
        slot_key = self.slot_key(slot)
        raw = self._assignment_raw(instance_uuid, slot, allocation_id)
        transaction = {
            "compare": self._guard_compares() + [
                self._compare_absent(instance_key),
                self._compare_absent(slot_key),
                self._compare_absent(self.release_key(instance_uuid)),
            ],
            "success": [
                {
                    "request_put": {
                        "key": self._b64(instance_key),
                        "value": self._b64(raw),
                    },
                },
                {
                    "request_put": {
                        "key": self._b64(slot_key),
                        "value": self._b64(raw),
                    },
                },
            ],
            "failure": [],
        }
        try:
            succeeded = self._transaction(transaction)
        except IDMapBackendError:
            current, unused_raw = self._get_allocatable_assignment(
                instance_uuid)
            if (current is not None and current.slot == slot and
                    current.allocation_id == allocation_id):
                return current
            raise
        if succeeded:
            current, unused_raw = self._get_allocatable_assignment(
                instance_uuid)
            if (current is None or current.slot != slot or
                    current.allocation_id != allocation_id):
                raise IDMapIntegrityError(
                    reason="committed allocation cannot be verified")
            return current
        return None

    def allocate(self, instance_uuid, preferred_base=None,
                 preferred_size=None):
        """Allocate or return a fixed ID map for ``instance_uuid``."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        current, unused_raw = self._get_allocatable_assignment(instance_uuid)
        preferred_slot = None
        if preferred_base is not None:
            preferred_slot = self._preferred_slot(
                preferred_base, preferred_size)
        elif (preferred_size is not None and
              self._positive_int(preferred_size, "preferred size") !=
              self.size):
            raise IDMapConflict(
                reason=("preferred ID-map size %s differs from configured %s" %
                        (preferred_size, self.size)))
        if current is not None:
            if preferred_slot is not None and current.slot != preferred_slot:
                raise IDMapConflict(
                    reason="instance already owns another ID-map slot")
            return current

        if preferred_slot is None:
            digest = hashlib.sha256(instance_uuid.encode("ascii")).digest()
            start = int.from_bytes(digest[:8], "big") % self.count
            slots = ((start + offset) % self.count
                     for offset in range(self.count))
        else:
            slots = (preferred_slot,)

        allocation_id = uuidutils.generate_uuid()
        for slot in slots:
            slot_key = self.slot_key(slot)
            slot_state = self._read_exact((slot_key,))
            slot_raw = slot_state[slot_key]
            if slot_raw is not None:
                owner = self._parse_assignment(
                    slot_raw, expected_slot=slot)
                if preferred_slot is not None:
                    raise IDMapConflict(
                        reason=("preferred ID-map slot is owned by %s" %
                                owner.instance_uuid))
                continue
            assignment = self._try_allocate(
                instance_uuid, slot, allocation_id)
            if assignment is not None:
                verified, unused_verified_raw = (
                    self._get_allocatable_assignment(instance_uuid))
                if (verified is None or
                        not self._same_generation(verified, assignment)):
                    raise IDMapIntegrityError(
                        reason="allocated generation changed before return")
                return verified

            # A concurrent request for the same UUID may have won at any
            # slot. Prefer that idempotent result over treating this as a
            # collision.
            current, unused_raw = self._get_allocatable_assignment(
                instance_uuid)
            if current is not None:
                if (preferred_slot is not None and
                        current.slot != preferred_slot):
                    raise IDMapConflict(
                        reason="instance concurrently acquired another slot")
                return current

            slot_state = self._read_exact((slot_key,))
            slot_raw = slot_state[slot_key]
            if slot_raw is None:
                raise IDMapBackendError(
                    reason="allocation transaction failed without a conflict")
            owner = self._parse_assignment(slot_raw, expected_slot=slot)
            if preferred_slot is not None:
                raise IDMapConflict(
                    reason=("preferred ID-map slot is owned by %s" %
                            owner.instance_uuid))

        raise IDMapExhausted(
            reason="all %d ID-map slots are allocated" % self.count)

    def adopt(self, instance_uuid, base, size=None):
        """Claim an existing fixed mapping without changing its base."""
        return self.allocate(
            instance_uuid, preferred_base=base,
            preferred_size=self.size if size is None else size)

    def validate_restore(self, instance_uuid, base, size, allocation_id,
                         fingerprint, host_claims):
        """Validate one exact assignment without reading or writing etcd."""
        if not isinstance(instance_uuid, str):
            raise IDMapConfigurationError(
                reason="restored instance UUID must be a string")
        normalized_uuid = self._instance_uuid(instance_uuid)
        if normalized_uuid != instance_uuid:
            raise IDMapConfigurationError(
                reason="restored instance UUID must be canonical")
        if type(base) is not int or type(size) is not int:
            raise IDMapConfigurationError(
                reason="restored ID-map base and size must be integers")
        slot = self._preferred_slot(base, size)
        if not isinstance(allocation_id, str):
            raise IDMapConfigurationError(
                reason="restored allocation ID must be a string")
        try:
            normalized_allocation_id = str(uuid.UUID(allocation_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise IDMapConfigurationError(
                reason="restored allocation ID is invalid: %s" % exc)
        if normalized_allocation_id != allocation_id:
            raise IDMapConfigurationError(
                reason="restored allocation ID must be canonical")
        if fingerprint != self.fingerprint:
            raise IDMapConfigurationError(
                reason="restored allocator fingerprint does not match")
        if not isinstance(host_claims, (list, tuple)):
            raise IDMapConfigurationError(
                reason="restored host claims must be a list or tuple")
        for claim in host_claims:
            if not isinstance(claim, IDMapHostClaim):
                raise IDMapConfigurationError(
                    reason="restored host claim has an invalid type")
        ordered_claims = tuple(sorted(
            host_claims, key=lambda value: value.host_id))
        if tuple(host_claims) != ordered_claims:
            raise IDMapConfigurationError(
                reason="restored host claims are not in canonical order")
        host_ids = tuple(claim.host_id for claim in ordered_claims)
        try:
            assignment = IDMapAssignment(
                instance_uuid=normalized_uuid,
                base=base,
                size=size,
                slot=slot,
                allocation_id=normalized_allocation_id,
                fingerprint=self.fingerprint,
                host_ids=host_ids,
            )
        except ValueError as exc:
            raise IDMapConfigurationError(
                reason="restored host claims are invalid: %s" % exc)
        for claim in ordered_claims:
            if not self._same_generation(assignment, claim):
                raise IDMapConfigurationError(
                    reason="restored host claim has another generation")
            parsed = self._parse_host_claim(
                self._host_claim_raw(
                    claim, claim.host_id, claim.materialization_id,
                    state=claim.state, proof=claim.proof),
                expected_host_id=claim.host_id,
                expected_uuid=assignment.instance_uuid)
            if parsed != claim:
                raise IDMapConfigurationError(
                    reason="restored host claim is not canonical")
        return assignment

    def _validate_restore_intent(self, assignment, intent):
        if intent is None:
            return None, None
        if not isinstance(intent, IDMapReleaseIntent):
            raise IDMapConfigurationError(
                reason="restored release intent has an invalid type")
        intended_assignment = IDMapAssignment(
            instance_uuid=intent.instance_uuid,
            base=intent.base,
            size=intent.size,
            slot=intent.slot,
            allocation_id=intent.allocation_id,
            fingerprint=intent.fingerprint,
        )
        if not self._same_generation(assignment, intended_assignment):
            raise IDMapConfigurationError(
                reason="restored release intent does not match allocation")
        raw = self._release_intent_raw(assignment, intent.instance_name)
        parsed = self._parse_release_intent(
            raw, expected_uuid=assignment.instance_uuid)
        if parsed != intent:
            raise IDMapConfigurationError(
                reason="restored release intent is not canonical")
        return parsed, raw

    def restore(self, instance_uuid, base, size, allocation_id, fingerprint,
                host_claims, release_intent=None):
        """Restore one exact allocation generation without overwriting.

        This primitive is intended only for an externally frozen disaster
        recovery workflow. The allocation pair, all durable host claims and
        an optional release barrier are restored atomically. It is safe to
        retry: an exact state succeeds, while any partial or different state
        is left untouched and rejected.
        """
        assignment = self.validate_restore(
            instance_uuid, base, size, allocation_id, fingerprint,
            host_claims)
        desired_intent, intent_raw = self._validate_restore_intent(
            assignment, release_intent)
        self._ensure_initialized()
        instance_key = self.instance_key(assignment.instance_uuid)
        slot_key = self.slot_key(assignment.slot)
        release_key = self.release_key(assignment.instance_uuid)
        raw = self._assignment_raw(
            assignment.instance_uuid, assignment.slot,
            assignment.allocation_id, assignment.host_ids)
        host_claim_records = [
            (
                self.host_claim_key(
                    claim.host_id, assignment.instance_uuid),
                self._host_claim_raw(
                    claim, claim.host_id, claim.materialization_id,
                    state=claim.state, proof=claim.proof),
            )
            for claim in host_claims
        ]

        for unused_attempt in range(32):
            assignments, intents, unused_claims = self._audit_with_latch()
            existing = next(
                (value for value in assignments
                 if value.instance_uuid == assignment.instance_uuid), None)
            slot_owner = next(
                (value for value in assignments
                 if value.slot == assignment.slot), None)
            intent = next(
                (value for value in intents
                 if value.instance_uuid == assignment.instance_uuid), None)
            if existing is not None:
                current, unused_raw, current_intent, unused_intent_raw, \
                    current_claims, unused_claim_raws = (
                        self._read_assignment_state(
                            assignment.instance_uuid))
                expected_claims = {
                    claim.host_id: claim for claim in host_claims
                }
                if existing != assignment or current_claims != expected_claims:
                    raise IDMapConflict(
                        reason=("restore would overwrite an existing "
                                "allocation or its host claims"))
                if intent == desired_intent and current_intent == intent:
                    return existing
                if intent is not None or desired_intent is None:
                    raise IDMapConflict(
                        reason=("restore would overwrite an existing release "
                                "intent"))

                transaction = {
                    "compare": self._guard_compares() + [
                        self._compare_value(instance_key, raw),
                        self._compare_value(slot_key, raw),
                        self._compare_absent(release_key),
                    ] + [
                        self._compare_value(key, claim_raw)
                        for key, claim_raw in host_claim_records
                    ],
                    "success": [self._put(release_key, intent_raw)],
                    "failure": [],
                }
                try:
                    succeeded = self._transaction(transaction)
                except IDMapBackendError:
                    after_assignments, after_intents, unused_after_claims = (
                        self._audit_with_latch())
                    observed = next(
                        (value for value in after_assignments
                         if value.instance_uuid == assignment.instance_uuid),
                        None)
                    observed_intent = next(
                        (value for value in after_intents
                         if value.instance_uuid == assignment.instance_uuid),
                        None)
                    unused_current, unused_raw, unused_exact_intent, \
                        unused_intent_raw, observed_claims, \
                        unused_claim_raws = self._read_assignment_state(
                            assignment.instance_uuid)
                    if (observed == assignment and
                            observed_intent == desired_intent and
                            observed_claims == expected_claims):
                        return observed
                    raise
                if succeeded:
                    continue
                continue
            if slot_owner is not None:
                raise IDMapConflict(
                    reason="restore slot is owned by another allocation")
            if intent is not None:
                raise IDMapIntegrityError(
                    reason="restore found an intent without an allocation")

            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_absent(instance_key),
                    self._compare_absent(slot_key),
                    self._compare_absent(release_key),
                ] + [
                    self._compare_absent(key)
                    for key, unused_raw in host_claim_records
                ],
                "success": [
                    self._put(instance_key, raw),
                    self._put(slot_key, raw),
                ] + ([self._put(release_key, intent_raw)]
                     if desired_intent is not None else []) + [
                    self._put(key, claim_raw)
                    for key, claim_raw in host_claim_records
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                after, after_intents, unused_after_claims = (
                    self._audit_with_latch())
                current = next(
                    (value for value in after
                     if value.instance_uuid == assignment.instance_uuid),
                    None)
                current_intent = next(
                    (value for value in after_intents
                     if value.instance_uuid == assignment.instance_uuid),
                    None)
                unused_current, unused_raw, unused_exact_intent, \
                    unused_intent_raw, current_claims, unused_claim_raws = (
                        self._read_assignment_state(
                            assignment.instance_uuid))
                expected_claims = {
                    claim.host_id: claim for claim in host_claims
                }
                if (current == assignment and
                        current_intent == desired_intent and
                        current_claims == expected_claims):
                    return current
                raise
            if succeeded:
                after, after_intents, unused_after_claims = (
                    self._audit_with_latch())
                current = next(
                    (value for value in after
                     if value.instance_uuid == assignment.instance_uuid),
                    None)
                current_intent = next(
                    (value for value in after_intents
                     if value.instance_uuid == assignment.instance_uuid),
                    None)
                unused_current, unused_raw, unused_exact_intent, \
                    unused_intent_raw, current_claims, unused_claim_raws = (
                        self._read_assignment_state(
                            assignment.instance_uuid))
                expected_claims = {
                    claim.host_id: claim for claim in host_claims
                }
                if (current != assignment or
                        current_intent != desired_intent or
                        current_claims != expected_claims):
                    raise IDMapIntegrityError(
                        reason=("restored allocation, host claims or release "
                                "intent cannot be verified"))
                return current

        raise IDMapConflict(reason="restore could not win its allocation CAS")

    def _require_generation(self, current, assignment):
        if assignment is None:
            return
        if not isinstance(assignment, IDMapAssignment):
            raise IDMapConfigurationError(
                reason="assignment must be an IDMapAssignment")
        if not self._same_generation(current, assignment):
            raise IDMapConflict(
                reason="allocator generation does not match the caller")

    def claim(self, instance_uuid, host_id, materialization_id,
              assignment=None):
        """Create one durable unmaterialized ``(A, H, T, U)`` claim."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                raise IDMapIntegrityError(
                    reason="cannot claim an absent ID-map allocation")
            self._require_generation(current, assignment)
            existing = claims.get(host_id)
            if existing is not None:
                if existing.materialization_id == materialization_id:
                    return current
                if existing.state != "cleaned" or existing.proof is None:
                    raise IDMapConflict(
                        reason="host already has another materialization")
                if intent is not None:
                    raise IDMapConflict(
                        reason="release intent blocks a new host claim")

                # A completed cleanup proof is sufficient authority to reuse
                # this host for the same allocation generation. Replace the
                # old materialization token in one CAS so a concurrent
                # reconciler cannot retire the new claim through an ABA
                # window in the assignment's host index.
                host_key = self.host_claim_key(host_id, instance_uuid)
                replacement_raw = self._host_claim_raw(
                    current, host_id, materialization_id)
                transaction = {
                    "compare": self._guard_compares() + [
                        self._compare_value(
                            self.instance_key(instance_uuid), current_raw),
                        self._compare_value(
                            self.slot_key(current.slot), current_raw),
                        self._compare_value(host_key, claim_raws[host_id]),
                        self._compare_absent(self.release_key(instance_uuid)),
                    ],
                    "success": [self._put(host_key, replacement_raw)],
                    "failure": [],
                }
                try:
                    succeeded = self._transaction(transaction)
                except IDMapBackendError:
                    observed, unused_raw, unused_intent, \
                        unused_intent_raw, observed_claims, \
                        unused_claim_raws = self._read_assignment_state(
                            instance_uuid)
                    observed_claim = observed_claims.get(host_id)
                    if (observed is not None and
                            self._same_generation(observed, current) and
                            observed_claim is not None and
                            observed_claim.materialization_id ==
                            materialization_id and
                            observed_claim.state == "unmaterialized"):
                        return observed
                    raise
                if not succeeded:
                    continue
                observed, unused_raw, unused_intent, unused_intent_raw, \
                    observed_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                observed_claim = observed_claims.get(host_id)
                if (observed is None or
                        not self._same_generation(observed, current) or
                        observed_claim is None or
                        observed_claim.materialization_id !=
                        materialization_id or
                        observed_claim.state != "unmaterialized" or
                        observed_claim.proof is not None):
                    raise IDMapIntegrityError(
                        reason="replacement host claim cannot be verified")
                return observed
            if intent is not None:
                raise IDMapConflict(
                    reason="release intent blocks a new host claim")

            desired = IDMapAssignment(
                instance_uuid=current.instance_uuid,
                base=current.base,
                size=current.size,
                slot=current.slot,
                allocation_id=current.allocation_id,
                fingerprint=current.fingerprint,
                host_ids=tuple(sorted(current.host_ids + (host_id,))),
            )
            desired_raw = self._assignment_raw(
                desired.instance_uuid, desired.slot,
                desired.allocation_id, desired.host_ids)
            host_key = self.host_claim_key(host_id, instance_uuid)
            claim_raw = self._host_claim_raw(
                current, host_id, materialization_id)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_absent(host_key),
                    self._compare_absent(self.release_key(instance_uuid)),
                ],
                "success": [
                    self._put(self.instance_key(instance_uuid), desired_raw),
                    self._put(self.slot_key(current.slot), desired_raw),
                    self._put(host_key, claim_raw),
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed, unused_raw, unused_intent, unused_intent_raw, \
                    observed_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                claim = observed_claims.get(host_id)
                if (observed is not None and
                        self._same_generation(observed, current) and
                        claim is not None and
                        claim.materialization_id == materialization_id):
                    return observed
                raise
            if not succeeded:
                continue
            observed, unused_raw, unused_intent, unused_intent_raw, \
                observed_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            claim = observed_claims.get(host_id)
            if (observed is None or
                    not self._same_generation(observed, current) or
                    claim is None or
                    claim.materialization_id != materialization_id):
                raise IDMapIntegrityError(
                    reason="host claim cannot be verified")
            return observed

        raise IDMapConflict(reason="host claim could not win its CAS")

    def mark_materialization_possible(self, instance_uuid, host_id,
                                      materialization_id, assignment=None):
        """Cross the rootfs-may-exist barrier immediately before Incus POST."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                raise IDMapIntegrityError(
                    reason="cannot materialize an absent allocation")
            self._require_generation(current, assignment)
            if intent is not None:
                raise IDMapConflict(
                    reason="release intent blocks rootfs materialization")
            claim = claims.get(host_id)
            if claim is None or claim.materialization_id != materialization_id:
                raise IDMapConflict(
                    reason="rootfs materialization requires its exact claim")
            if claim.state == "possible":
                return claim
            if claim.state != "unmaterialized":
                raise IDMapConflict(
                    reason="cleaned materialization cannot be reused")
            desired = IDMapHostClaim(**dict(
                claim.__dict__, state="possible"))
            desired_raw = self._host_claim_raw(
                desired, host_id, materialization_id, state="possible")
            host_key = self.host_claim_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                    self._compare_absent(self.release_key(instance_uuid)),
                ],
                "success": [self._put(host_key, desired_raw)],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed = self.get_host_claim(instance_uuid, host_id)
                if (observed is not None and
                        observed.materialization_id == materialization_id and
                        observed.state == "possible"):
                    return observed
                raise
            if not succeeded:
                continue
            observed = self.get_host_claim(instance_uuid, host_id)
            if observed != desired:
                raise IDMapIntegrityError(
                    reason="materialization barrier cannot be verified")
            return observed
        raise IDMapConflict(
            reason="materialization barrier could not win its CAS")

    def mark_materialization_committed(self, instance_uuid, host_id,
                                       materialization_id, assignment=None):
        """Promote one exact possible claim after Incus durably commits."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, unused_intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                raise IDMapIntegrityError(
                    reason="cannot commit an absent allocation")
            self._require_generation(current, assignment)
            claim = claims.get(host_id)
            if claim is None or claim.materialization_id != materialization_id:
                raise IDMapConflict(
                    reason="rootfs commit requires its exact claim")
            if claim.state == "committed":
                return claim
            if claim.state != "possible":
                raise IDMapConflict(
                    reason="only a possible materialization can commit")
            desired = IDMapHostClaim(**dict(
                claim.__dict__, state="committed"))
            desired_raw = self._host_claim_raw(
                desired, host_id, materialization_id, state="committed")
            host_key = self.host_claim_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                ],
                "success": [self._put(host_key, desired_raw)],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed = self.get_host_claim(instance_uuid, host_id)
                if (observed is not None and
                        observed.materialization_id == materialization_id and
                        observed.state == "committed"):
                    return observed
                raise
            if not succeeded:
                continue
            observed = self.get_host_claim(instance_uuid, host_id)
            if observed != desired:
                raise IDMapIntegrityError(
                    reason="materialization commit cannot be verified")
            return observed
        raise IDMapConflict(
            reason="materialization commit could not win its CAS")

    def _record_claim_proof(self, instance_uuid, host_id,
                            materialization_id, proof, assignment=None):
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, unused_intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                raise IDMapIntegrityError(
                    reason="cannot prove an absent allocation")
            self._require_generation(current, assignment)
            claim = claims.get(host_id)
            if claim is None or claim.materialization_id != materialization_id:
                raise IDMapConflict(reason="cleanup proof has no exact claim")
            if claim.state == "cleaned":
                if claim.proof == proof:
                    return claim
                raise IDMapConflict(
                    reason="another immutable cleanup proof already exists")
            if isinstance(proof, IDMapMaterializationProof):
                required_state = (
                    "unmaterialized"
                    if proof.outcome == "not-materialized" else "possible")
            elif isinstance(proof, IDMapRootfsReleaseProof):
                required_state = "committed"
            else:
                raise IDMapIntegrityError(
                    reason="cleanup proof has an invalid type")
            if claim.state != required_state:
                raise IDMapConflict(
                    reason=(
                        "cleanup proof cannot transition claim state %s" %
                        claim.state))
            candidate = IDMapHostClaim(**dict(
                claim.__dict__, state="cleaned", proof=proof))
            self._validate_claim_proof_binding(candidate)
            desired_raw = self._host_claim_raw(
                candidate, host_id, materialization_id,
                state="cleaned", proof=proof)
            host_key = self.host_claim_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                ],
                "success": [self._put(host_key, desired_raw)],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed = self.get_host_claim(instance_uuid, host_id)
                if observed == candidate:
                    return observed
                raise
            if not succeeded:
                continue
            observed = self.get_host_claim(instance_uuid, host_id)
            if observed != candidate:
                raise IDMapIntegrityError(
                    reason="cleanup proof cannot be verified")
            return observed
        raise IDMapConflict(reason="cleanup proof could not win its CAS")

    def record_materialization_proof(self, instance_uuid, host_id,
                                     materialization_id, proof,
                                     assignment=None):
        """Persist exact never-started or reconciled-clean Incus proof."""
        self._validate_materialization_proof(proof)
        return self._record_claim_proof(
            instance_uuid, host_id, materialization_id, proof,
            assignment=assignment)

    def record_rootfs_release_proof(self, instance_uuid, host_id,
                                    materialization_id, receipt,
                                    assignment=None):
        """Persist one exact complete Incus storage release receipt."""
        proof = self._proof_from_receipt(receipt)
        return self._record_claim_proof(
            instance_uuid, host_id, materialization_id, proof,
            assignment=assignment)

    def fence_retire_claim(self, instance_uuid, host_id, fence_proof,
                           assignment=None):
        """Retire a claim held by a host proven powered off by STONITH.

        Ordinary retirement requires the holding host to produce a storage
        release receipt, which a dead host can never do. Evacuation
        therefore deadlocked: the destination's spawn pre-check refuses
        while any claim of the generation is unreleased, and nothing could
        release the source's.

        External power fencing is the authority that replaces the receipt.
        The caller must pass an ``IDMapFenceProof`` recording which host was
        fenced, by which agent, when, and by whom; it is written into the
        assignment's fence ledger before the claim is removed, so the
        disposal is auditable and can never be mistaken for a normal
        cleanup. A claim that is already ``cleaned`` must go through
        ``retire_claim`` with its real proof instead.
        """
        validate_fence_proof(fence_proof)
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        if fence_proof.host_id != host_id:
            raise IDMapIntegrityError(
                reason="fence proof names another host")
        if fence_proof.instance_uuid != instance_uuid:
            raise IDMapIntegrityError(
                reason="fence proof names another instance")

        for unused_attempt in range(64):
            current, current_raw, unused_intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                return None
            self._require_generation(current, assignment)
            claim = claims.get(host_id)
            if claim is None:
                return current
            if claim.state == "cleaned" and claim.proof is not None:
                raise IDMapConflict(
                    reason="a cleaned claim must be retired with its own "
                           "cleanup proof")
            if fence_proof.allocation_id != current.allocation_id:
                raise IDMapIntegrityError(
                    reason="fence proof names another allocation generation")
            desired = IDMapAssignment(
                instance_uuid=current.instance_uuid,
                base=current.base,
                size=current.size,
                slot=current.slot,
                allocation_id=current.allocation_id,
                fingerprint=current.fingerprint,
                host_ids=tuple(
                    value for value in current.host_ids if value != host_id),
            )
            desired_raw = self._assignment_raw(
                desired.instance_uuid, desired.slot,
                desired.allocation_id, desired.host_ids)
            host_key = self.host_claim_key(host_id, instance_uuid)
            fence_key = self.fence_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                ],
                "success": [
                    self._put(fence_key, self._fence_proof_raw(
                        fence_proof, claim.materialization_id)),
                    self._put(self.instance_key(instance_uuid), desired_raw),
                    self._put(self.slot_key(current.slot), desired_raw),
                    self._delete(host_key),
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed, unused_raw, unused_intent, unused_intent_raw, \
                    observed_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                if observed is None or host_id not in observed_claims:
                    return observed
                raise
            if not succeeded:
                continue
            observed, unused_raw, unused_intent, unused_intent_raw, \
                observed_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            if observed is None or host_id not in observed_claims:
                return observed
            raise IDMapIntegrityError(
                reason="fence-retired host claim still exists")
        raise IDMapConflict(
            reason="fence retirement could not win CAS")

    def abandon_unregistered_claim(self, instance_uuid, host_id,
                                   materialization_id, assignment=None):
        """Remove one ``unmaterialized`` claim whose attempt never existed.

        The ``possible`` transition happens only after the materialization
        attempt is registered with Incus, and a registered attempt is only
        deleted after its cleanup proof made the claim ``cleaned``. A claim
        still ``unmaterialized`` while Incus holds no attempt record
        therefore proves the create request was never issued: there is no
        rootfs, no proof, and nothing for the ordinary settle path to
        consume. This is the single sanctioned proof-free removal; every
        other disposal still goes through ``retire_claim`` with a durable
        exact cleanup proof.
        """
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, unused_intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                return None
            self._require_generation(current, assignment)
            claim = claims.get(host_id)
            if claim is None:
                return current
            if claim.materialization_id != materialization_id:
                raise IDMapConflict(
                    reason="another materialization owns the host claim")
            if claim.state != "unmaterialized":
                raise IDMapConflict(
                    reason="only a never-registered unmaterialized claim "
                           "may be abandoned without proof")
            desired = IDMapAssignment(
                instance_uuid=current.instance_uuid,
                base=current.base,
                size=current.size,
                slot=current.slot,
                allocation_id=current.allocation_id,
                fingerprint=current.fingerprint,
                host_ids=tuple(
                    value for value in current.host_ids if value != host_id),
            )
            desired_raw = self._assignment_raw(
                desired.instance_uuid, desired.slot,
                desired.allocation_id, desired.host_ids)
            host_key = self.host_claim_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                ],
                "success": [
                    self._put(self.instance_key(instance_uuid), desired_raw),
                    self._put(self.slot_key(current.slot), desired_raw),
                    self._delete(host_key),
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed, unused_raw, unused_intent, unused_intent_raw, \
                    observed_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                if observed is None or host_id not in observed_claims:
                    return observed
                raise
            if not succeeded:
                continue
            observed, unused_raw, unused_intent, unused_intent_raw, \
                observed_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            if observed is None or host_id not in observed_claims:
                return observed
            raise IDMapIntegrityError(
                reason="abandoned host claim still exists")
        raise IDMapConflict(
            reason="host claim abandonment could not win CAS")

    def retire_claim(self, instance_uuid, host_id, materialization_id,
                     assignment=None):
        """Retire a claim only after an exact cleanup proof is durable."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)

        for unused_attempt in range(64):
            current, current_raw, unused_intent, unused_intent_raw, claims, \
                claim_raws = self._read_assignment_state(instance_uuid)
            if current is None:
                return None
            self._require_generation(current, assignment)
            claim = claims.get(host_id)
            if claim is None:
                return current
            if claim.materialization_id != materialization_id:
                raise IDMapConflict(
                    reason="another materialization owns the host claim")
            if claim.state != "cleaned" or claim.proof is None:
                raise IDMapConflict(
                    reason="host claim lacks an exact cleanup proof")
            desired = IDMapAssignment(
                instance_uuid=current.instance_uuid,
                base=current.base,
                size=current.size,
                slot=current.slot,
                allocation_id=current.allocation_id,
                fingerprint=current.fingerprint,
                host_ids=tuple(
                    value for value in current.host_ids if value != host_id),
            )
            desired_raw = self._assignment_raw(
                desired.instance_uuid, desired.slot,
                desired.allocation_id, desired.host_ids)
            host_key = self.host_claim_key(host_id, instance_uuid)
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_value(host_key, claim_raws[host_id]),
                ],
                "success": [
                    self._put(self.instance_key(instance_uuid), desired_raw),
                    self._put(self.slot_key(current.slot), desired_raw),
                    self._delete(host_key),
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                observed, unused_raw, unused_intent, unused_intent_raw, \
                    observed_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                if observed is None or host_id not in observed_claims:
                    return observed
                raise
            if not succeeded:
                continue
            observed, unused_raw, unused_intent, unused_intent_raw, \
                observed_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            if observed is None or host_id not in observed_claims:
                return observed
            raise IDMapIntegrityError(
                reason="retired host claim still exists")
        raise IDMapConflict(reason="host claim retirement could not win CAS")

    def request_release(self, instance_uuid, instance_name, assignment=None):
        """Create an immutable release barrier for one exact generation."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        instance_name = self._instance_name(instance_name)

        for unused_attempt in range(64):
            current, current_raw, existing, unused_existing_raw, \
                unused_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            if current is None:
                raise IDMapIntegrityError(
                    reason="cannot request release of an absent allocation")
            self._require_generation(current, assignment)
            desired_raw = self._release_intent_raw(current, instance_name)
            desired = self._parse_release_intent(
                desired_raw, expected_uuid=instance_uuid)
            if existing is not None:
                if existing == desired:
                    return existing
                raise IDMapConflict(
                    reason="another immutable release intent already exists")
            transaction = {
                "compare": self._guard_compares() + [
                    self._compare_value(
                        self.instance_key(instance_uuid), current_raw),
                    self._compare_value(
                        self.slot_key(current.slot), current_raw),
                    self._compare_absent(self.release_key(instance_uuid)),
                ],
                "success": [
                    self._put(self.release_key(instance_uuid), desired_raw),
                ],
                "failure": [],
            }
            try:
                succeeded = self._transaction(transaction)
            except IDMapBackendError:
                unused_current, unused_current_raw, observed, \
                    unused_observed_raw, unused_claims, unused_claim_raws = (
                        self._read_assignment_state(instance_uuid))
                if observed == desired:
                    return observed
                raise
            if not succeeded:
                continue
            unused_current, unused_current_raw, observed, \
                unused_observed_raw, unused_claims, unused_claim_raws = (
                    self._read_assignment_state(instance_uuid))
            if observed != desired:
                raise IDMapIntegrityError(
                    reason="release intent cannot be verified")
            return observed

        raise IDMapConflict(reason="release intent could not win its CAS")

    def assert_startable(self, instance_uuid, host_id, materialization_id,
                         assignment=None):
        """Prove one exact claimed generation has no deletion barrier."""
        self._ensure_initialized()
        instance_uuid = self._instance_uuid(instance_uuid)
        host_id = self._host_id(host_id)
        materialization_id = self._materialization_id(materialization_id)
        current, unused_raw, intent, unused_intent_raw, claims, \
            unused_claim_raws = self._read_assignment_state(instance_uuid)
        if current is None:
            raise IDMapIntegrityError(
                reason="cannot start without an ID-map allocation")
        self._require_generation(current, assignment)
        if intent is not None:
            raise IDMapConflict(
                reason="ID-map release intent blocks instance start")
        claim = claims.get(host_id)
        if (claim is None or
                claim.materialization_id != materialization_id):
            raise IDMapConflict(
                reason="local compute has no exact materialization claim")
        if claim.state != "committed":
            raise IDMapConflict(
                reason="instance rootfs is not in a startable state")
        return current

    def release(self, intent):
        """Delete an exact generation only behind an empty-claims barrier."""
        if not isinstance(intent, IDMapReleaseIntent):
            raise IDMapConfigurationError(
                reason="release requires an IDMapReleaseIntent")
        self._ensure_initialized()

        current, current_raw, existing_intent, intent_raw, claims, \
            unused_claim_raws = (
                self._read_assignment_state(intent.instance_uuid))
        if current is None:
            return True
        if existing_intent != intent:
            return False
        intent_assignment = IDMapAssignment(
            instance_uuid=intent.instance_uuid,
            base=intent.base,
            size=intent.size,
            slot=intent.slot,
            allocation_id=intent.allocation_id,
            fingerprint=intent.fingerprint,
        )
        if not self._same_generation(current, intent_assignment):
            return False
        if current.host_ids:
            return False
        if claims:
            raise IDMapIntegrityError(
                reason="empty host index conflicts with exact claim read")
        transaction = {
            "compare": self._guard_compares() + [
                self._compare_value(
                    self.instance_key(current.instance_uuid), current_raw),
                self._compare_value(self.slot_key(current.slot), current_raw),
                self._compare_value(
                    self.release_key(current.instance_uuid), intent_raw),
            ],
            "success": [
                self._delete(self.instance_key(current.instance_uuid)),
                self._delete(self.slot_key(current.slot)),
                self._delete(self.release_key(current.instance_uuid)),
            ],
            "failure": [],
        }
        try:
            succeeded = self._transaction(transaction)
        except IDMapBackendError:
            after, unused_raw, unused_intent, unused_intent_raw, \
                unused_claims, unused_claim_raws = self._read_assignment_state(
                    intent.instance_uuid)
            if after is None:
                return True
            raise
        if not succeeded:
            after, unused_raw, unused_intent, unused_intent_raw, \
                unused_claims, unused_claim_raws = self._read_assignment_state(
                    intent.instance_uuid)
            if after is None:
                return True
            return False

        after, unused_raw, unused_intent, unused_intent_raw, unused_claims, \
            unused_claim_raws = self._read_assignment_state(
                intent.instance_uuid)
        if after is not None:
            raise IDMapIntegrityError(
                reason="released allocation or intent still exists")
        return True


def materialization_proof_digest(proof):
    """Return the Incus v1 canonical materialization proof digest."""
    return IDMapAllocator._materialization_digest(proof)


def validate_materialization_proof(proof):
    """Fail closed unless *proof* is a canonical terminal Incus proof."""
    IDMapAllocator._validate_materialization_proof(proof)
    return proof


def rootfs_release_proof_digest(proof):
    """Return the Incus storage_release_receipt_v2 canonical digest."""
    return IDMapAllocator._release_digest(proof)


def validate_rootfs_release_receipt(receipt):
    """Return the exact validated proof represented by a v2 receipt."""
    return IDMapAllocator._proof_from_receipt(receipt)


def validate_fence_proof(proof):
    """Fail closed unless *proof* is a complete operator fence record."""
    if not isinstance(proof, IDMapFenceProof):
        raise IDMapIntegrityError(
            reason="fence retirement requires an IDMapFenceProof")
    for field in ("instance_uuid", "host_id", "allocation_id",
                  "fence_agent", "fenced_at", "operator", "evidence"):
        value = getattr(proof, field)
        if not isinstance(value, str) or not value.strip():
            raise IDMapIntegrityError(
                reason="fence proof field %s is missing" % field)
    for field in ("instance_uuid", "host_id", "allocation_id"):
        if not uuidutils.is_uuid_like(getattr(proof, field)):
            raise IDMapIntegrityError(
                reason="fence proof field %s must be a UUID" % field)
    return proof
