# Copyright 2026
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

import argparse
import time
import uuid

import pylxd


def _wait_for_status(instance, expected, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        instance.sync()
        if instance.status == expected:
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Instance {instance.name} did not reach {expected}: "
        f"current status is {instance.status}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--project", default="default")
    parser.add_argument("--root-size", default="1GiB")
    args = parser.parse_args()

    name = f"nova-incus-smoke-{uuid.uuid4().hex[:8]}"
    client = pylxd.Client(
        endpoint=args.endpoint,
        project=args.project,
        timeout=30,
    )
    instance = None

    try:
        instance = client.instances.create({
            "name": name,
            "type": "container",
            "profiles": [],
            "config": {
                "limits.cpu": "1",
                "limits.memory": "256MiB",
                "limits.memory.swap": "false",
                "limits.processes": "256",
                "security.privileged": "false",
                "security.idmap.isolated": "true",
            },
            "devices": {
                "root": {
                    "type": "disk",
                    "path": "/",
                    "pool": args.pool,
                    "size": args.root_size,
                },
            },
            "source": {
                "type": "image",
                "fingerprint": args.image,
            },
        }, wait=True)
        _wait_for_status(instance, "Stopped")

        instance.start(wait=True)
        _wait_for_status(instance, "Running")
        console_log = instance.console_log()
        if not isinstance(console_log, bytes):
            raise RuntimeError(
                f"Console log API returned {type(console_log).__name__}")
        instance.sync()
        expected_config = {
            "limits.processes": "256",
            "limits.memory.swap": "false",
            "security.idmap.isolated": "true",
            "security.privileged": "false",
        }
        for key, expected in expected_config.items():
            if instance.config.get(key) != expected:
                raise RuntimeError(
                    f"Unexpected {key}: {instance.config.get(key)!r}")
        if instance.devices["root"].get("size") != args.root_size:
            raise RuntimeError(
                f"Rootfs size was not retained: {instance.devices['root']}")

        result = instance.execute(["cat", "/proc/self/uid_map"])
        if result.exit_code != 0:
            raise RuntimeError(f"UID map query failed: {result.stderr}")
        uid_map = [line.split() for line in result.stdout.splitlines()]
        if not uid_map or uid_map[0][0] != "0" or uid_map[0][1] == "0":
            raise RuntimeError(f"Container root is not remapped: {uid_map}")

        result = instance.execute([
            "sh", "-c", "printf nova-incus-smoke >/var/tmp/nova-smoke",
        ])
        if result.exit_code != 0:
            raise RuntimeError(f"Marker write failed: {result.stderr}")

        instance.restart(wait=True)
        _wait_for_status(instance, "Running")
        result = instance.execute(["cat", "/var/tmp/nova-smoke"])
        if result.exit_code != 0 or result.stdout != "nova-incus-smoke":
            raise RuntimeError(
                "Rootfs marker did not survive restart: "
                f"exit={result.exit_code} stdout={result.stdout!r}")

        instance.stop(wait=True)
        _wait_for_status(instance, "Stopped")

        state = instance.state()
        print(
            f"PASS name={name} status={instance.status} "
            f"pid={state.pid} pool={args.pool} root_size={args.root_size} "
            f"host_root_uid={uid_map[0][1]} "
            f"console_bytes={len(console_log)}")
    finally:
        if instance is not None:
            instance.sync()
            if instance.status != "Stopped":
                instance.stop(force=True, wait=True)
            instance.delete(wait=True)


if __name__ == "__main__":
    main()
