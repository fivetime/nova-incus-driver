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

"""TCP to Incus interactive-exec bridge for Nova serialproxy."""

import socket

import eventlet
from oslo_log import log as logging
from ws4py.client import WebSocketBaseClient
from ws4py.manager import WebSocketManager

from nova.console import serial as serial_console


LOG = logging.getLogger(__name__)


class _ConsoleWebSocket(WebSocketBaseClient):
    def __init__(self, manager, tcp_socket, *args, **kwargs):
        self.manager = manager
        self.tcp_socket = tcp_socket
        kwargs.setdefault('exclude_headers', ['origin'])
        super().__init__(*args, **kwargs)

    def handshake_ok(self):
        self.manager.add(self)

    def received_message(self, message):
        if message.data:
            self.tcp_socket.sendall(message.data)

    def closed(self, code, reason=None):
        try:
            self.tcp_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _ControlWebSocket(WebSocketBaseClient):
    """Keep the Incus interactive-exec control channel established."""

    def __init__(self, manager, *args, **kwargs):
        self.manager = manager
        kwargs.setdefault('exclude_headers', ['origin'])
        super().__init__(*args, **kwargs)

    def handshake_ok(self):
        self.manager.add(self)


class SerialConsoleBroker:
    """Own one management-network listener for an Incus instance."""

    def __init__(self, host, instance, command=None):
        self.host = host
        self.instance = instance
        self.command = command or ['/bin/login']
        self.port = serial_console.acquire_port(host)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((host, self.port))
            self.listener.listen(5)
        except Exception:
            self.listener.close()
            serial_console.release_port(host, self.port)
            raise
        self._closed = False
        self._accept_greenlet = eventlet.spawn(self._accept)

    def _accept(self):
        while not self._closed:
            try:
                tcp_socket, _address = self.listener.accept()
            except OSError:
                if not self._closed:
                    LOG.exception('Incus serial console accept failed')
                return
            eventlet.spawn_n(self._bridge, tcp_socket)

    def _bridge(self, tcp_socket):
        manager = WebSocketManager()
        websocket = None
        control_websocket = None
        try:
            endpoints = self.instance.raw_interactive_execute(
                self.command,
                environment={'TERM': 'xterm'},
                user=0,
                group=0)
            websocket = _ConsoleWebSocket(
                manager,
                tcp_socket,
                self.instance.client.websocket_url,
                ssl_options=self.instance.client.ssl_options)
            # Keep the operation resource separate from the base URL.  This
            # is required for local clients where websocket_url uses the
            # ws+unix scheme and its host component is the socket path.
            websocket.resource = endpoints['ws']
            websocket.connect()
            control_websocket = _ControlWebSocket(
                manager,
                self.instance.client.websocket_url,
                ssl_options=self.instance.client.ssl_options)
            control_websocket.resource = endpoints['control']
            control_websocket.connect()
            manager.start()
            while True:
                data = tcp_socket.recv(65536)
                if not data:
                    break
                websocket.send(data, binary=True)
        except Exception:
            LOG.exception(
                'Incus interactive serial console bridge failed for %s',
                self.instance.name)
        finally:
            if websocket is not None:
                try:
                    websocket.close()
                except Exception:
                    pass
            if control_websocket is not None:
                try:
                    control_websocket.close()
                except Exception:
                    pass
            manager.close_all()
            manager.stop()
            try:
                manager.join()
            except RuntimeError:
                pass
            tcp_socket.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.listener.close()
        serial_console.release_port(self.host, self.port)
        self._accept_greenlet.kill()
