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

from types import SimpleNamespace
import unittest
from unittest import mock

from nova.virt.lxd import client


def _config(**overrides):
    options = {
        "endpoint": "/var/lib/incus/unix.socket",
        "project": "default",
        "request_timeout": 300,
        "tls_cert": None,
        "tls_key": None,
        "tls_ca": None,
        "migration_preflight_tls_cert": None,
        "migration_preflight_tls_key": None,
        "migration_preflight_tls_ca": None,
        "migration_preflight_timeout": 5,
        "migration_preflight_project": "nova-preflight",
    }
    options.update(overrides)
    return SimpleNamespace(incus=SimpleNamespace(**options))


class IncusClientTest(unittest.TestCase):

    @mock.patch.object(client.pylxd, "Client")
    def test_local_client(self, mock_client):
        conf = _config()

        client.get_client(conf)

        mock_client.assert_called_once_with(
            endpoint="/var/lib/incus/unix.socket",
            cert=None,
            verify=True,
            timeout=300,
            project="default",
        )

    @mock.patch.object(client.pylxd, "Client")
    def test_https_client(self, mock_client):
        conf = _config(
            endpoint="https://incus.example.test:8443",
            project="compute",
            tls_cert="/etc/nova/incus.crt",
            tls_key="/etc/nova/incus.key",
            tls_ca="/etc/nova/incus-ca.crt",
            request_timeout=45,
        )

        client.get_client(conf)

        mock_client.assert_called_once_with(
            endpoint="https://incus.example.test:8443",
            cert=("/etc/nova/incus.crt", "/etc/nova/incus.key"),
            verify="/etc/nova/incus-ca.crt",
            timeout=45,
            project="compute",
        )

    def test_https_client_requires_certificate_pair(self):
        conf = _config(
            endpoint="https://incus.example.test:8443",
            tls_cert="/etc/nova/incus.crt",
        )

        self.assertRaisesRegex(
            ValueError,
            "must be set together",
            client.get_client,
            conf,
        )

    @mock.patch.object(client.pylxd, "Client")
    def test_migration_preflight_client(self, mock_client):
        conf = _config(
            migration_preflight_tls_cert="/etc/nova/preflight.crt",
            migration_preflight_tls_key="/etc/nova/preflight.key",
            migration_preflight_tls_ca="/etc/nova/preflight-ca.crt",
            migration_preflight_timeout=7,
        )

        client.get_migration_preflight_client(
            "https://compute-2.example.test:8443",
            verify="/etc/nova/compute-2.crt", conf=conf)

        mock_client.assert_called_once_with(
            endpoint="https://compute-2.example.test:8443",
            cert=("/etc/nova/preflight.crt", "/etc/nova/preflight.key"),
            verify="/etc/nova/compute-2.crt",
            timeout=7,
            project="nova-preflight",
        )

    def test_migration_preflight_client_requires_all_tls_options(self):
        conf = _config(
            migration_preflight_tls_cert="/etc/nova/preflight.crt",
        )

        self.assertRaisesRegex(
            ValueError,
            "migration_preflight_tls_ca, migration_preflight_tls_key",
            client.get_migration_preflight_client,
            "https://compute-2.example.test:8443",
            conf=conf,
        )
