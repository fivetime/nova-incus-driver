# Copyright 2015 Canonical Ltd
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import collections
import os
import re

from nova import conf
from nova import exception


INCUS_STORAGE_POOL_TRAIT_PREFIX = 'CUSTOM_INCUS_STORAGE_POOL_'


def root_storage_pool_trait(selector):
    suffix = re.sub(r'[^A-Z0-9_]', '_', selector.upper())
    trait = INCUS_STORAGE_POOL_TRAIT_PREFIX + suffix
    if not suffix or len(trait) > 255:
        raise exception.InvalidConfiguration(
            'Invalid Incus root storage pool selector {}'.format(selector))
    return trait


_InstanceAttributes = collections.namedtuple('InstanceAttributes', [
    'instance_dir', 'console_path', 'storage_path', 'container_path'])


def InstanceAttributes(instance):
    """An instance adapter for nova-incus specific attributes."""
    if is_snap_incus():
        prefix = '/var/snap/incus/common/incus/logs'
    else:
        prefix = '/var/log/incus'
    instance_dir = os.path.join(conf.CONF.instances_path, instance.name)
    console_path = os.path.join(prefix, instance.name, 'console.log')
    storage_path = os.path.join(instance_dir, 'storage')
    container_path = os.path.join(
        conf.CONF.incus.root_dir, 'containers', instance.name)
    return _InstanceAttributes(
        instance_dir, console_path, storage_path, container_path)


def is_snap_incus():
    """Determine whether Incus is installed as a snap or a package.

    This is easily done by checking if the bin file is /snap/bin/lxc

    :returns: True if snap installed, otherwise False
    :rtype: bool
    """
    return os.path.isfile('/snap/bin/lxc')
