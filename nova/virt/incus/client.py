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

from oslo_config import cfg
import pylxd

from nova.virt.incus import config


config.register_opts()


_CONFIGURED_PROJECT = object()


def _client_certificate(options):
    if options.tls_cert is None and options.tls_key is None:
        return None
    if not options.tls_cert or not options.tls_key:
        raise ValueError(
            "incus.tls_cert and incus.tls_key must be set together"
        )
    return options.tls_cert, options.tls_key


def get_client(conf=cfg.CONF, project=_CONFIGURED_PROJECT):
    """Create an SDK client for the host-local Incus daemon."""
    options = conf.incus
    endpoint = options.endpoint
    verify = options.tls_ca if options.tls_ca else True
    if project is _CONFIGURED_PROJECT:
        project = options.project

    if endpoint.startswith("/"):
        cert = None
        verify = True
    else:
        cert = _client_certificate(options)

    return pylxd.Client(
        endpoint=endpoint,
        cert=cert,
        verify=verify,
        timeout=options.request_timeout,
        project=project,
    )


def get_migration_preflight_client(endpoint, verify=None, conf=cfg.CONF):
    """Create a least-privilege client for remote BFV readiness checks."""
    options = conf.incus
    values = {
        "migration_preflight_tls_cert":
            options.migration_preflight_tls_cert,
        "migration_preflight_tls_key": options.migration_preflight_tls_key,
        "migration_preflight_tls_ca": options.migration_preflight_tls_ca,
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise ValueError(
            "Missing Incus migration preflight TLS options: %s" %
            ", ".join(missing)
        )

    return pylxd.Client(
        endpoint=endpoint,
        cert=(options.migration_preflight_tls_cert,
              options.migration_preflight_tls_key),
        verify=verify or options.migration_preflight_tls_ca,
        timeout=options.migration_preflight_timeout,
        project=options.migration_preflight_project,
    )


def get_migration_client(endpoint, verify=None, conf=cfg.CONF):
    """Create the project-restricted client used for instance migration."""
    options = conf.incus
    values = {
        "migration_tls_cert": options.migration_tls_cert,
        "migration_tls_key": options.migration_tls_key,
    }
    missing = sorted(name for name, value in values.items() if not value)
    if missing:
        raise ValueError(
            "Missing Incus migration TLS options: %s" % ", ".join(missing)
        )
    verify = verify or options.migration_tls_ca
    if not verify:
        raise ValueError("Missing Incus migration TLS CA")

    return pylxd.Client(
        endpoint=endpoint,
        cert=(options.migration_tls_cert, options.migration_tls_key),
        verify=verify,
        timeout=options.request_timeout,
        project=options.project,
    )
