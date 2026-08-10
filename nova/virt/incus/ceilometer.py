# Copyright 2026 OpenStack Incus contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Ceilometer inspector for Nova-managed Incus system containers."""

from types import SimpleNamespace

from ceilometer.compute.pollsters import util
from ceilometer.compute.virt import inspector
from pylxd import exceptions as incus_exceptions

from nova.virt.incus import client as incus_client
from nova.virt.incus import config as incus_config
from nova.virt.incus import driver as incus_driver


class IncusInspector(inspector.Inspector):
    """Read cumulative instance counters from the host-local Incus API."""

    def __init__(self, conf):
        # Ceilometer owns a ConfigOpts instance distinct from Nova's global
        # cfg.CONF. Register the driver options on the object that the
        # pollster passes to us before constructing the local Incus client.
        incus_config.register_opts(conf)
        super().__init__(conf)
        # The driver registers the [incus] group on import. Ceilometer uses
        # its own config file, so local-socket/project defaults apply unless
        # an operator explicitly mirrors non-default Incus connection values.
        self.client = incus_client.get_client(conf)

    @staticmethod
    def _identity(instance):
        name = util.instance_name(instance)
        if not name:
            raise inspector.InstanceNotFoundException(
                "Nova did not expose the internal instance name")
        return SimpleNamespace(name=name, uuid=instance.id)

    def _diagnostics(self, instance):
        identity = self._identity(instance)
        try:
            container = self.client.instances.get(identity.name)
            state = container.state()
        except incus_exceptions.LXDAPIException as exc:
            if incus_driver._is_incus_not_found(exc):
                raise inspector.InstanceNotFoundException(
                    "Incus instance %s was not found" % identity.name) from exc
            raise inspector.InspectorException(str(exc)) from exc

        cpu = state.cpu or {}
        memory = state.memory or {}
        return state, cpu, memory

    def inspect_instance(self, instance, duration):
        state, cpu, memory = self._diagnostics(instance)
        total = memory.get('total')
        used = memory.get('usage')
        total_mib = total / (1024 * 1024) if total is not None else None
        used_mib = used / (1024 * 1024) if used is not None else None
        available_mib = (
            max(0, total - used) / (1024 * 1024)
            if total is not None and used is not None else None)
        flavor = getattr(instance, 'flavor', None) or {}
        return inspector.InstanceStats(
            power_state=incus_driver._get_power_state(state.status_code),
            cpu_number=flavor.get('vcpus'),
            cpu_time=cpu.get('usage'),
            memory_actual=total_mib,
            memory_available=available_mib,
            memory_usage=used_mib,
            memory_resident=used_mib,
        )

    def inspect_vnics(self, instance, duration):
        state, _cpu, _memory = self._diagnostics(instance)
        for name, network in sorted((state.network or {}).items()):
            if network.get('type') == 'loopback':
                continue
            counters = network.get('counters') or {}
            yield inspector.InterfaceStats(
                name=name,
                mac=network.get('hwaddr'),
                fref=None,
                parameters=None,
                rx_bytes=counters.get('bytes_received'),
                tx_bytes=counters.get('bytes_sent'),
                rx_packets=counters.get('packets_received'),
                tx_packets=counters.get('packets_sent'),
                rx_drop=counters.get('packets_dropped_inbound'),
                tx_drop=counters.get('packets_dropped_outbound'),
                rx_errors=counters.get('errors_received'),
                tx_errors=counters.get('errors_sent'),
                rx_bytes_delta=None,
                tx_bytes_delta=None,
            )
