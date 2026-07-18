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

from unittest import mock

from nova import test
from nova.virt.incus import console


class SerialConsoleBrokerTest(test.NoDBTestCase):

    @mock.patch.object(console.eventlet, 'spawn')
    @mock.patch.object(console.socket, 'socket')
    @mock.patch.object(console.serial_console, 'acquire_port',
                       return_value=10001)
    def test_listener_is_bound_to_proxyclient_address(
            self, acquire_port, socket_factory, spawn):
        listener = socket_factory.return_value
        instance = mock.Mock()

        broker = console.SerialConsoleBroker('192.0.2.10', instance)

        acquire_port.assert_called_once_with('192.0.2.10')
        listener.bind.assert_called_once_with(('192.0.2.10', 10001))
        listener.listen.assert_called_once_with(5)
        spawn.assert_called_once_with(broker._accept)

    @mock.patch.object(console.serial_console, 'release_port')
    @mock.patch.object(console.eventlet, 'spawn')
    @mock.patch.object(console.socket, 'socket')
    @mock.patch.object(console.serial_console, 'acquire_port',
                       return_value=10001)
    def test_close_releases_port(
            self, acquire_port, socket_factory, spawn, release_port):
        broker = console.SerialConsoleBroker(
            '192.0.2.10', mock.Mock())

        broker.close()

        broker.listener.close.assert_called_once_with()
        release_port.assert_called_once_with('192.0.2.10', 10001)
        broker._accept_greenlet.kill.assert_called_once_with()

    @mock.patch.object(console, '_ConsoleWebSocket')
    @mock.patch.object(console, '_ControlWebSocket')
    @mock.patch.object(console, 'WebSocketManager')
    def test_bridge_keeps_unix_socket_url_and_resource_separate(
            self, manager_factory, control_factory, websocket_factory):
        instance = mock.Mock()
        instance.name = 'instance-00000001'
        instance.client.websocket_url = 'ws+unix:///var/lib/incus/unix.socket'
        instance.client.ssl_options = None
        instance.raw_interactive_execute.return_value = {
            'ws': '/1.0/operations/op/websocket?secret=secret',
            'control': '/1.0/operations/op/websocket?secret=control',
        }
        tcp_socket = mock.Mock()
        tcp_socket.recv.return_value = b''
        websocket = websocket_factory.return_value

        broker = object.__new__(console.SerialConsoleBroker)
        broker.instance = instance
        broker.command = ['/bin/login']
        broker._bridge(tcp_socket)

        websocket_factory.assert_called_once_with(
            manager_factory.return_value,
            tcp_socket,
            'ws+unix:///var/lib/incus/unix.socket',
            ssl_options=None)
        self.assertEqual(
            '/1.0/operations/op/websocket?secret=secret',
            websocket.resource)
        websocket.connect.assert_called_once_with()
        control_factory.assert_called_once_with(
            manager_factory.return_value,
            'ws+unix:///var/lib/incus/unix.socket',
            ssl_options=None)
        self.assertEqual(
            '/1.0/operations/op/websocket?secret=control',
            control_factory.return_value.resource)
        control_factory.return_value.connect.assert_called_once_with()
