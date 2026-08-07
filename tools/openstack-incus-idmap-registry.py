#!/usr/bin/env python3
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
"""Audit or explicitly restore the global Incus ID-map v3 registry."""

import argparse
import json
from pathlib import Path
import sys
import uuid

from nova.virt.incus import idmap


DOCUMENT_SCHEMA = "openstack-incus-idmap-registry/v3"


def _proof_payload(allocator, proof):
    return allocator._cleanup_proof_value(proof)


def host_claim_payload(allocator, claim):
    return {
        "allocation_id": claim.allocation_id,
        "base": claim.base,
        "fingerprint": claim.fingerprint,
        "host_id": claim.host_id,
        "instance_uuid": claim.instance_uuid,
        "materialization_id": claim.materialization_id,
        "proof": _proof_payload(allocator, claim.proof),
        "size": claim.size,
        "slot": claim.slot,
        "state": claim.state,
    }


def assignment_payload(allocator, assignment, claims):
    return {
        "allocation_id": assignment.allocation_id,
        "base": assignment.base,
        "fingerprint": assignment.fingerprint,
        "host_claims": [
            host_claim_payload(allocator, claim) for claim in claims
        ],
        "instance_uuid": assignment.instance_uuid,
        "size": assignment.size,
        "slot": assignment.slot,
    }


def release_intent_payload(intent):
    return {
        "allocation_id": intent.allocation_id,
        "base": intent.base,
        "fingerprint": intent.fingerprint,
        "instance_name": intent.instance_name,
        "instance_uuid": intent.instance_uuid,
        "size": intent.size,
        "slot": intent.slot,
    }


def registry_document(allocator, assignments, release_intents=None):
    snapshot_assignments, snapshot_intents, snapshot_claims = (
        allocator.audit_state())
    if assignments != snapshot_assignments:
        raise idmap.IDMapConflict(
            reason="assignment set changed during registry export")
    if release_intents is None:
        release_intents = snapshot_intents
    elif release_intents != snapshot_intents:
        raise idmap.IDMapConflict(
            reason="release intent set changed during registry export")
    claims_by_instance = {}
    for claim in snapshot_claims:
        claims_by_instance.setdefault(claim.instance_uuid, []).append(claim)
    return {
        "assignments": [
            assignment_payload(
                allocator, value,
                claims_by_instance.get(value.instance_uuid, []))
            for value in sorted(assignments, key=lambda value: value.slot)
        ],
        "base": allocator.base,
        "count": allocator.count,
        "fingerprint": allocator.fingerprint,
        "namespace": allocator.namespace,
        "release_intents": [
            release_intent_payload(value)
            for value in sorted(
                release_intents,
                key=lambda value: (value.slot, value.instance_uuid))
        ],
        "schema": DOCUMENT_SCHEMA,
        "size": allocator.size,
    }


def _require_exact_keys(value, expected, description):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("%s has an invalid schema" % description)


def _require_int(value, description):
    if type(value) is not int:
        raise ValueError("%s must be an integer" % description)
    return value


def _parse_claim(allocator, value, assignment_index, claim_index):
    description = "restore assignment %d host claim %d" % (
        assignment_index, claim_index)
    expected = {
        "allocation_id", "base", "fingerprint", "host_id",
        "instance_uuid", "materialization_id", "proof", "size", "slot",
        "state",
    }
    _require_exact_keys(value, expected, description)
    for field in (
            "allocation_id", "fingerprint", "host_id", "instance_uuid",
            "materialization_id", "state"):
        if not isinstance(value[field], str):
            raise ValueError("%s %s must be a string" % (description, field))
    try:
        proof = allocator._parse_cleanup_proof(value["proof"])
        claim = idmap.IDMapHostClaim(
            host_id=allocator._host_id(value["host_id"]),
            materialization_id=allocator._materialization_id(
                value["materialization_id"]),
            instance_uuid=allocator._instance_uuid(value["instance_uuid"]),
            base=_require_int(value["base"], "%s base" % description),
            size=_require_int(value["size"], "%s size" % description),
            slot=_require_int(value["slot"], "%s slot" % description),
            allocation_id=allocator._materialization_id(
                value["allocation_id"]),
            fingerprint=value["fingerprint"],
            state=value["state"],
            proof=proof,
        )
        allocator._parse_host_claim(
            allocator._host_claim_raw(
                claim, claim.host_id, claim.materialization_id,
                state=claim.state, proof=claim.proof),
            expected_host_id=claim.host_id,
            expected_uuid=claim.instance_uuid)
    except idmap.IDMapError as exc:
        raise ValueError("%s is invalid: %s" % (description, exc))
    return claim


def validate_restore_document(allocator, document):
    expected_keys = {
        "assignments", "base", "count", "fingerprint", "namespace",
        "release_intents", "schema", "size",
    }
    _require_exact_keys(document, expected_keys, "restore document")
    for field in ("base", "count", "size"):
        _require_int(document[field], "restore document %s" % field)
    expected_config = {
        "base": allocator.base,
        "count": allocator.count,
        "fingerprint": allocator.fingerprint,
        "namespace": allocator.namespace,
        "schema": DOCUMENT_SCHEMA,
        "size": allocator.size,
    }
    if {key: document[key] for key in expected_config} != expected_config:
        raise ValueError(
            "restore document does not match allocator configuration")
    if not isinstance(document["assignments"], list):
        raise ValueError("restore assignments must be a list")

    assignment_keys = {
        "allocation_id", "base", "fingerprint", "host_claims",
        "instance_uuid", "size", "slot",
    }
    assignments = []
    claims_by_instance = {}
    seen_instances = set()
    seen_slots = set()
    for index, value in enumerate(document["assignments"]):
        _require_exact_keys(
            value, assignment_keys, "restore assignment %d" % index)
        if not isinstance(value["host_claims"], list):
            raise ValueError(
                "restore assignment %d host_claims must be a list" % index)
        claims = [
            _parse_claim(allocator, claim, index, claim_index)
            for claim_index, claim in enumerate(value["host_claims"])
        ]
        assignment = allocator.validate_restore(
            value["instance_uuid"],
            _require_int(value["base"], "restore assignment base"),
            _require_int(value["size"], "restore assignment size"),
            value["allocation_id"], value["fingerprint"], claims)
        if (_require_int(value["slot"], "restore assignment slot") !=
                assignment.slot):
            raise ValueError(
                "restore assignment %d has an invalid slot" % index)
        if assignment.instance_uuid in seen_instances:
            raise ValueError("restore document contains a duplicate instance")
        if assignment.slot in seen_slots:
            raise ValueError("restore document contains a duplicate slot")
        seen_instances.add(assignment.instance_uuid)
        seen_slots.add(assignment.slot)
        assignments.append(assignment)
        claims_by_instance[assignment.instance_uuid] = tuple(claims)

    if not isinstance(document["release_intents"], list):
        raise ValueError("restore release_intents must be a list")
    assignments_by_instance = {
        value.instance_uuid: value for value in assignments
    }
    intent_keys = {
        "allocation_id", "base", "fingerprint", "instance_name",
        "instance_uuid", "size", "slot",
    }
    release_intents = []
    seen_intents = set()
    for index, value in enumerate(document["release_intents"]):
        _require_exact_keys(
            value, intent_keys, "restore release intent %d" % index)
        assignment = assignments_by_instance.get(value["instance_uuid"])
        if assignment is None:
            raise ValueError(
                "restore release intent %d has no assignment" % index)
        generation = (
            value["instance_uuid"], value["base"], value["size"],
            value["slot"], value["allocation_id"], value["fingerprint"],
        )
        expected_generation = (
            assignment.instance_uuid, assignment.base, assignment.size,
            assignment.slot, assignment.allocation_id,
            assignment.fingerprint,
        )
        if generation != expected_generation:
            raise ValueError(
                "restore release intent %d does not match its assignment" %
                index)
        instance_name = allocator._instance_name(value["instance_name"])
        if assignment.instance_uuid in seen_intents:
            raise ValueError(
                "restore document contains a duplicate release intent")
        seen_intents.add(assignment.instance_uuid)
        release_intents.append(idmap.IDMapReleaseIntent(
            instance_uuid=assignment.instance_uuid,
            instance_name=instance_name,
            base=assignment.base,
            size=assignment.size,
            slot=assignment.slot,
            allocation_id=assignment.allocation_id,
            fingerprint=assignment.fingerprint,
        ))

    if assignments != sorted(assignments, key=lambda value: value.slot):
        raise ValueError("restore assignments are not in canonical order")
    if release_intents != sorted(
            release_intents,
            key=lambda value: (value.slot, value.instance_uuid)):
        raise ValueError(
            "restore release_intents are not in canonical order")
    return assignments, claims_by_instance, release_intents


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("restore document contains duplicate JSON keys")
        value[key] = item
    return value


def load_restore_document(path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object)


def _same_assignment_generation(left, right):
    return (
        left.instance_uuid == right.instance_uuid and
        left.base == right.base and
        left.size == right.size and
        left.slot == right.slot and
        left.allocation_id == right.allocation_id and
        left.fingerprint == right.fingerprint
    )


def restore_registry(allocator, document):
    expected, claims_by_instance, expected_intents = (
        validate_restore_document(allocator, document))
    allocator.bootstrap()
    existing, existing_intents, unused_existing_claims = (
        allocator.audit_state())
    expected_by_instance = {
        value.instance_uuid: value for value in expected
    }
    expected_intents_by_instance = {
        value.instance_uuid: value for value in expected_intents
    }
    for value in existing:
        if expected_by_instance.get(value.instance_uuid) != value:
            raise idmap.IDMapIntegrityError(
                reason=("registry contains an allocation absent from or "
                        "different to the frozen restore document"))
    for value in existing_intents:
        if expected_intents_by_instance.get(value.instance_uuid) != value:
            raise idmap.IDMapIntegrityError(
                reason=("registry contains a release intent absent from or "
                        "different to the frozen restore document"))

    for value in expected:
        restored = allocator.restore(
            value.instance_uuid, value.base, value.size,
            value.allocation_id, value.fingerprint,
            claims_by_instance[value.instance_uuid],
            release_intent=expected_intents_by_instance.get(
                value.instance_uuid))
        if restored != value:
            raise idmap.IDMapIntegrityError(
                reason="restored allocation does not exactly match")

    restored, restored_intents, unused_restored_claims = (
        allocator.audit_state())
    if restored != expected or restored_intents != expected_intents:
        raise idmap.IDMapIntegrityError(
            reason="registry does not exactly match the restore document")
    return restored, restored_intents


def retire_host_claim(allocator, document, instance_uuid, host_id):
    """Retire one exact already-cleaned claim from a frozen v3 document."""
    expected, claims_by_instance, unused_intents = (
        validate_restore_document(allocator, document))
    expected_assignment = next(
        (value for value in expected if value.instance_uuid == instance_uuid),
        None)
    if expected_assignment is None:
        raise idmap.IDMapIntegrityError(
            reason="frozen document has no requested assignment")
    expected_claim = next(
        (value for value in claims_by_instance[instance_uuid]
         if value.host_id == host_id), None)
    if (expected_claim is None or expected_claim.state != "cleaned" or
            expected_claim.proof is None):
        raise idmap.IDMapIntegrityError(
            reason="frozen document has no exact cleaned host claim")

    current = allocator.get(instance_uuid)
    if current is None or not _same_assignment_generation(
            current, expected_assignment):
        raise idmap.IDMapConflict(
            reason="current allocation differs from the frozen document")
    if allocator.get_host_claim(instance_uuid, host_id) != expected_claim:
        raise idmap.IDMapConflict(
            reason="current host claim differs from the frozen document")
    retired = allocator.retire_claim(
        instance_uuid, host_id, expected_claim.materialization_id,
        assignment=expected_assignment)
    if (retired is not None and
            (not _same_assignment_generation(retired, expected_assignment) or
             host_id in retired.host_ids)):
        raise idmap.IDMapIntegrityError(
            reason="retired host claim cannot be verified")
    assignments, intents, unused_claims = allocator.audit_state()
    return assignments, intents


def fence_retire_host_claim(allocator, instance_uuid, host_id, fence_agent,
                            fenced_at, operator, evidence):
    """Dispose of a fenced host's claim so evacuation can proceed.

    Unlike --retire-host-claim this does not consult a frozen document,
    because the point is that the holding host is powered off and can
    never produce the cleanup proof such a document would carry. The
    operator's fence evidence is the authority and is recorded in the
    registry's fence ledger.
    """
    current = allocator.get(instance_uuid)
    if current is None:
        raise idmap.IDMapIntegrityError(
            reason="no allocation for the requested instance")
    proof = idmap.IDMapFenceProof(
        instance_uuid=instance_uuid,
        host_id=host_id,
        allocation_id=current.allocation_id,
        fence_agent=fence_agent,
        fenced_at=fenced_at,
        operator=operator,
        evidence=evidence)
    allocator.fence_retire_claim(
        instance_uuid, host_id, proof, assignment=current)
    assignments, intents, unused_claims = allocator.audit_state()
    return assignments, intents


def _canonical_uuid_argument(value):
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a UUID")
    if normalized != value:
        raise argparse.ArgumentTypeError("must be a canonical UUID")
    return normalized


def _parser():
    parser = argparse.ArgumentParser(
        description=("Audit the Incus ID-map v3 registry, or restore an "
                     "exact frozen v3 snapshot without overwriting keys."))
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--base", required=True, type=int)
    parser.add_argument("--size", type=int, default=65536)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--ca-cert")
    parser.add_argument("--client-cert")
    parser.add_argument("--client-key")
    parser.add_argument("--username")
    parser.add_argument("--password-file")
    parser.add_argument(
        "--allow-insecure", action="store_true",
        help="Allow unauthenticated HTTP (test only).")
    parser.add_argument("--restore-file", type=Path)
    parser.add_argument("--confirm-frozen", action="store_true")
    parser.add_argument("--bootstrap-empty", action="store_true")
    parser.add_argument(
        "--retire-host-claim", metavar="INSTANCE_UUID",
        type=_canonical_uuid_argument,
        help="Retire an exact cleaned claim from --restore-file.")
    parser.add_argument(
        "--host-id", metavar="COMPUTE_UUID", type=_canonical_uuid_argument)
    parser.add_argument(
        "--fence-retire-host-claim", metavar="INSTANCE_UUID",
        type=_canonical_uuid_argument,
        help=("Dispose of the --host-id claim for this instance using "
              "external fence evidence, so evacuation can proceed. Requires "
              "--fence-agent, --fenced-at, --operator and --fence-evidence."))
    parser.add_argument("--fence-agent", metavar="AGENT")
    parser.add_argument("--fenced-at", metavar="ISO8601")
    parser.add_argument("--operator", metavar="WHO")
    parser.add_argument("--fence-evidence", metavar="TEXT")
    return parser


def main(argv=None, allocator_factory=idmap.IDMapAllocator,
         stdout=None, stderr=None):
    parser = _parser()
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    fence_requested = args.fence_retire_host_claim is not None
    retire_requested = (
        not fence_requested and
        (args.retire_host_claim is not None or args.host_id is not None))
    if fence_requested:
        required = {
            "--host-id": args.host_id is not None,
            "--fence-agent": bool(args.fence_agent),
            "--fenced-at": bool(args.fenced_at),
            "--operator": bool(args.operator),
            "--fence-evidence": bool(args.fence_evidence),
        }
        missing = sorted(
            option for option, supplied in required.items() if not supplied)
        if missing:
            parser.error(
                "fence-based retirement requires %s" % ", ".join(missing))
        if args.restore_file is not None or args.retire_host_claim is not None:
            parser.error(
                "--fence-retire-host-claim cannot be combined with frozen "
                "document retirement")
    if args.bootstrap_empty and args.restore_file is not None:
        parser.error(
            "--bootstrap-empty cannot be combined with --restore-file")
    if args.bootstrap_empty and retire_requested:
        parser.error(
            "--bootstrap-empty cannot be combined with host-claim retirement")
    if retire_requested:
        required = {
            "--confirm-frozen": args.confirm_frozen,
            "--host-id": args.host_id is not None,
            "--restore-file": args.restore_file is not None,
            "--retire-host-claim": args.retire_host_claim is not None,
        }
        missing = sorted(
            option for option, supplied in required.items() if not supplied)
        if missing:
            parser.error(
                "cleaned host-claim retirement requires %s" %
                ", ".join(missing))
    elif args.restore_file is not None and not args.confirm_frozen:
        parser.error("--restore-file requires --confirm-frozen")
    elif args.bootstrap_empty and not args.confirm_frozen:
        parser.error("--bootstrap-empty requires --confirm-frozen")
    elif (args.confirm_frozen and args.restore_file is None and
          not args.bootstrap_empty):
        parser.error(
            "--confirm-frozen requires --restore-file or --bootstrap-empty")

    try:
        allocator = allocator_factory(
            endpoint=args.endpoint,
            namespace=args.namespace,
            base=args.base,
            size=args.size,
            count=args.count,
            timeout=args.timeout,
            ca_cert=args.ca_cert,
            cert_cert=args.client_cert,
            cert_key=args.client_key,
            username=args.username,
            password_file=args.password_file,
            allow_insecure=args.allow_insecure,
        )
        if fence_requested:
            assignments, release_intents = fence_retire_host_claim(
                allocator, args.fence_retire_host_claim, args.host_id,
                args.fence_agent, args.fenced_at, args.operator,
                args.fence_evidence)
        elif args.restore_file is None:
            if args.bootstrap_empty:
                allocator.bootstrap()
            assignments, release_intents, unused_claims = (
                allocator.audit_state())
        else:
            document = load_restore_document(args.restore_file)
            if retire_requested:
                assignments, release_intents = retire_host_claim(
                    allocator, document, args.retire_host_claim,
                    args.host_id)
            else:
                assignments, release_intents = restore_registry(
                    allocator, document)
        output = registry_document(
            allocator, assignments, release_intents)
    except (idmap.IDMapError, OSError, ValueError) as exc:
        print(json.dumps({
            "error": str(exc),
            "schema": DOCUMENT_SCHEMA,
            "status": "error",
        }, sort_keys=True), file=stderr)
        return 2

    print(json.dumps(output, indent=2, sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
