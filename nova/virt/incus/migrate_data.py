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

    VERSION = '1.1'

    fields = {
        'destination_address': fields.StringField(),
        'destination_architecture': fields.StringField(),
        'destination_kernel_version': fields.StringField(),
        'destination_server_version': fields.StringField(),
        # JSON keeps the nested Incus device mapping opaque to Nova objects
        # while carrying the exact source profile into destination preflight.
        'source_profile': fields.StringField(nullable=True),
    }

    def obj_make_compatible(self, primitive, target_version):
        super().obj_make_compatible(primitive, target_version)
        if versionutils.convert_version_to_tuple(target_version) < (1, 1):
            primitive.pop('source_profile', None)
