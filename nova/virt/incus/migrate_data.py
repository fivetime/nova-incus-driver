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

from nova.objects import base as obj_base
from nova.objects import fields
from nova.objects import migrate_data
from oslo_utils import versionutils


@obj_base.NovaObjectRegistry.register
class IncusLiveMigrateData(migrate_data.LiveMigrateData):
    """Incus destination facts carried through Nova's migration RPCs."""

    VERSION = '1.6'

    fields = {
        'destination_address': fields.StringField(),
        'destination_architecture': fields.StringField(),
        'destination_kernel_version': fields.StringField(),
        'destination_server_version': fields.StringField(),
        # JSON keeps the nested Incus device mapping opaque to Nova objects
        # while carrying the exact source profile into destination preflight.
        'source_profile': fields.StringField(nullable=True),
        # Per-attempt fencing token used for positive destination cleanup
        # acknowledgement during rollback.
        'cleanup_token': fields.StringField(),
        # Nova Migration UUID is independent from the Incus attempt token.
        # Periodic recovery needs both identities to reject a stale attempt.
        'migration_uuid': fields.StringField(),
        # Incus operation identities are carried explicitly so rollback can
        # cancel and prove terminal both sides before it restores the source.
        'source_operation_id': fields.StringField(nullable=True),
        'destination_operation_id': fields.StringField(nullable=True),
        # Fixed isolated idmap reserved by the target-side migration fence.
        'idmap_base': fields.IntegerField(),
        'idmap_size': fields.IntegerField(),
        # Set only after the source driver has checked its dedicated profile,
        # instance-local config, and expanded config under the profile lock.
        # A new destination rejects migration data from an older source that
        # cannot make this attestation.
        'full_checkpoint_verified': fields.BooleanField(),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if versionutils.convert_version_to_tuple(target_version) < (1, 1):
            primitive.pop('source_profile', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 2):
            primitive.pop('cleanup_token', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 3):
            primitive.pop('source_operation_id', None)
            primitive.pop('destination_operation_id', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 4):
            primitive.pop('idmap_base', None)
            primitive.pop('idmap_size', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 5):
            primitive.pop('migration_uuid', None)
        if versionutils.convert_version_to_tuple(target_version) < (1, 6):
            primitive.pop('full_checkpoint_verified', None)
