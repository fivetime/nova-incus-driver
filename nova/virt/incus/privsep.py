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

"""Bounded privileged helpers used by the Incus compute driver."""

import os
import re
import stat
import uuid

from oslo_concurrency import processutils

import nova.conf
from nova.i18n import _
import nova.privsep


_KILL_GRACE_SECONDS = 5
_MAX_COMMAND_TIMEOUT_SECONDS = 300
GNU_TIMEOUT_PATH = '/usr/bin/timeout'
_MOUNT_PATH = '/bin/mount'
_UMOUNT_PATH = '/bin/umount'
_ALLOWED_FILESYSTEMS = frozenset(('nfs', 'nfs4', 'ceph'))
_REQUIRED_MOUNT_OPTIONS = frozenset(('rw', 'nosuid', 'nodev'))
_NFS_MOUNT_OPTIONS = _REQUIRED_MOUNT_OPTIONS
_CEPH_MOUNT_OPTIONS = _REQUIRED_MOUNT_OPTIONS | frozenset((
    'name', 'secretfile'))
_CEPH_CLIENT_RE = re.compile(r'^[A-Za-z0-9_.-]{1,255}$')
_CEPH_SECRET_RE = re.compile(r'^\.ceph-secret-[A-Za-z0-9_.-]+$')


def _is_canonical_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _validate_share_mountpoint(mountpoint):
    """Return a canonical Manila staging path or fail closed."""
    if (not isinstance(mountpoint, str) or not mountpoint or
            '\x00' in mountpoint):
        raise ValueError(_('Manila mountpoint is invalid'))

    instances_path = os.path.abspath(nova.conf.CONF.instances_path)
    staging_root = os.path.join(instances_path, 'incus-shares')
    requested = os.path.abspath(mountpoint)
    try:
        relative = os.path.relpath(requested, staging_root)
    except ValueError as exc:
        raise ValueError(
            _('Manila mountpoint is outside instances_path')) from exc
    components = relative.split(os.sep)
    if (len(components) != 2 or
            not all(_is_canonical_uuid(value) for value in components)):
        raise ValueError(
            _('Manila mountpoint must use canonical instance and share UUIDs'))

    expected = os.path.join(staging_root, *components)
    if requested != expected:
        raise ValueError(_('Manila mountpoint is not canonical'))

    expected_real = os.path.join(
        os.path.realpath(instances_path), 'incus-shares', *components)
    if os.path.realpath(requested) != expected_real:
        raise ValueError(
            _('Manila mountpoint contains a symbolic-link escape'))

    current = staging_root
    for component in components:
        for candidate in (current, os.path.join(current, component)):
            try:
                mode = os.lstat(candidate).st_mode
            except FileNotFoundError as exc:
                raise ValueError(
                    _('Manila mountpoint hierarchy does not exist')) from exc
            if stat.S_ISLNK(mode):
                raise ValueError(
                    _('Manila mountpoint hierarchy contains a symbolic link'))
            if not stat.S_ISDIR(mode):
                raise ValueError(
                    _('Manila mountpoint hierarchy contains a non-directory'))
        current = os.path.join(current, component)

    return requested


def _validate_secretfile(value, mountpoint):
    secret_path = os.path.abspath(value)
    expected_parent = os.path.dirname(mountpoint)
    expected_real = os.path.join(
        os.path.realpath(expected_parent), os.path.basename(secret_path))
    if (not os.path.isabs(value) or
            os.path.dirname(secret_path) != expected_parent or
            not _CEPH_SECRET_RE.fullmatch(os.path.basename(secret_path)) or
            os.path.realpath(secret_path) != expected_real):
        raise ValueError(_('CephFS secretfile path is invalid'))
    try:
        secret_stat = os.lstat(secret_path)
    except FileNotFoundError as exc:
        raise ValueError(_('CephFS secretfile does not exist')) from exc
    if (not stat.S_ISREG(secret_stat.st_mode) or
            secret_stat.st_mode & 0o077):
        raise ValueError(
            _('CephFS secretfile must be a private regular file'))


def _validate_mount_options(fstype, options, mountpoint):
    if not isinstance(options, (list, tuple)):
        raise ValueError(_('Manila mount options must be a sequence'))

    parsed = {}
    normalized = []
    for option in options:
        if (not isinstance(option, str) or not option or
                '\x00' in option or ',' in option or
                option.startswith('-')):
            raise ValueError(_('Manila mount option is invalid'))
        name, separator, value = option.partition('=')
        if name in parsed:
            raise ValueError(
                _('Duplicate Manila mount option %s') % name)
        parsed[name] = value if separator else None
        normalized.append(option)

    allowed = (
        _CEPH_MOUNT_OPTIONS if fstype == 'ceph' else _NFS_MOUNT_OPTIONS)
    if not _REQUIRED_MOUNT_OPTIONS.issubset(parsed):
        raise ValueError(
            _('Manila mounts require rw,nosuid,nodev'))
    if not set(parsed).issubset(allowed):
        raise ValueError(_('Unsupported Manila mount option'))
    for name in _REQUIRED_MOUNT_OPTIONS:
        if parsed[name] is not None:
            raise ValueError(
                _('Manila flag option %s cannot have a value') % name)

    if fstype == 'ceph':
        if not _CEPH_CLIENT_RE.fullmatch(parsed.get('name') or ''):
            raise ValueError(_('CephFS client name is invalid'))
        secretfile = parsed.get('secretfile')
        if not secretfile:
            raise ValueError(_('CephFS secretfile is required'))
        _validate_secretfile(secretfile, mountpoint)

    return ','.join(normalized)


def validate_gnu_timeout():
    """Require the GNU implementation used by the termination contract."""
    stdout, _stderr = processutils.execute(
        GNU_TIMEOUT_PATH, '--version', timeout=5)
    first_line = stdout.splitlines()[0] if stdout else ''
    if not first_line.startswith('timeout (GNU coreutils)'):
        raise RuntimeError(
            _('%s is not GNU coreutils timeout') % GNU_TIMEOUT_PATH)


def _timeout_prefix(timeout):
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise ValueError(_('mount command timeout must be an integer'))
    if timeout < 1:
        raise ValueError(_('mount command timeout must be positive'))
    if timeout > _MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(
            _('mount command timeout cannot exceed %s seconds') %
            _MAX_COMMAND_TIMEOUT_SECONDS)
    return [
        GNU_TIMEOUT_PATH,
        '--signal=TERM',
        '--kill-after={}s'.format(_KILL_GRACE_SECONDS),
        '{}s'.format(timeout),
    ]


@nova.privsep.sys_admin_pctxt.entrypoint
def mount(fstype, device, mountpoint, options, timeout):
    """Mount without allowing an unbounded helper or kernel wait."""
    if fstype not in _ALLOWED_FILESYSTEMS:
        raise ValueError(_('Unsupported Manila filesystem type'))
    if (not isinstance(device, str) or not device or '\x00' in device or
            '\n' in device or '\r' in device):
        raise ValueError(_('Manila export location is invalid'))
    mountpoint = _validate_share_mountpoint(mountpoint)
    options = _validate_mount_options(fstype, options, mountpoint)
    command = _timeout_prefix(timeout) + [
        _MOUNT_PATH, '-t', fstype, '-o', options]
    command.extend(['--', device, mountpoint])
    # oslo.concurrency's timeout raises without terminating the child in the
    # 2026.1 baseline. GNU timeout owns termination; the outer timeout only
    # prevents a broken wrapper from holding the privsep RPC indefinitely.
    return processutils.execute(
        *command, timeout=int(timeout) + _KILL_GRACE_SECONDS + 5)


@nova.privsep.sys_admin_pctxt.entrypoint
def umount(mountpoint, timeout):
    """Unmount normally; never use force or lazy-unmount semantics."""
    mountpoint = _validate_share_mountpoint(mountpoint)
    command = _timeout_prefix(timeout) + [
        _UMOUNT_PATH, '--', mountpoint]
    return processutils.execute(
        *command, timeout=int(timeout) + _KILL_GRACE_SECONDS + 5)


def _validate_instances_subpath(path, description):
    """Return a canonical path under instances_path or fail closed."""
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError(_('%s is invalid') % description)
    instances_path = os.path.realpath(nova.conf.CONF.instances_path)
    requested = os.path.realpath(path)
    if requested != instances_path and not requested.startswith(
            instances_path + os.sep):
        raise ValueError(
            _('%s is outside instances_path') % description)
    return requested


@nova.privsep.sys_admin_pctxt.entrypoint
def configdrive_mount_iso(iso_path, mountpoint, uid, gid, timeout):
    """Loop-mount a config-drive ISO read-only under instances_path.

    The mountpoint is constrained exactly like the source. Mounting over a
    system directory is a host takeover even read-only: the mount hides the
    real directory while the caller controls the image contents. The
    filesystem type is pinned so a crafted image cannot reach an arbitrary
    kernel filesystem driver through type autodetection.
    """
    iso_path = _validate_instances_subpath(iso_path, 'config drive ISO')
    if not os.path.isfile(iso_path):
        raise ValueError(_('config drive ISO is not a regular file'))
    mountpoint = _validate_instances_subpath(
        mountpoint, 'config drive mountpoint')
    if not os.path.isdir(mountpoint):
        raise ValueError(_('config drive mountpoint is not a directory'))
    options = 'loop,ro,nosuid,nodev,noexec,uid=%d,gid=%d' % (
        int(uid), int(gid))
    command = _timeout_prefix(timeout) + [
        _MOUNT_PATH, '-t', 'iso9660', '-o', options, '--',
        iso_path, mountpoint]
    return processutils.execute(
        *command, timeout=int(timeout) + _KILL_GRACE_SECONDS + 5)


@nova.privsep.sys_admin_pctxt.entrypoint
def configdrive_umount(mountpoint, timeout):
    """Unmount a config-drive ISO mounted by configdrive_mount_iso.

    Unmounting is as privileged as mounting: releasing an arbitrary
    mountpoint would make later writes land on whatever directory sits
    underneath it, so the target is constrained to instances_path too.
    """
    mountpoint = _validate_instances_subpath(
        mountpoint, 'config drive mountpoint')
    command = _timeout_prefix(timeout) + [
        _UMOUNT_PATH, '--', mountpoint]
    return processutils.execute(
        *command, timeout=int(timeout) + _KILL_GRACE_SECONDS + 5)


@nova.privsep.sys_admin_pctxt.entrypoint
def chown_tree_to_host_id(path, host_id):
    """Recursively chown one instances_path subtree to a shifted host ID.

    Replaces the unconstrained rootwrap 'chown -R' CommandFilter: the target
    is validated to stay inside instances_path and the ownership value must
    be a plain integer, so no privileged caller can retarget system paths.
    """
    root = _validate_instances_subpath(path, 'ownership target')
    owner = int(host_id)
    os.chown(root, owner, owner)
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            os.lchown(os.path.join(parent, name), owner, owner)
