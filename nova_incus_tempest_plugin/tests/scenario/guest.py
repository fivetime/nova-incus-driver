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


def privilege_command(ssh):
    """Return the guest's supported non-interactive privilege tool."""
    command = ssh.exec_command(
        'command -v sudo || command -v doas || true').strip()
    if command.rsplit('/', 1)[-1] not in {'sudo', 'doas'}:
        raise RuntimeError('Guest provides neither sudo nor doas')
    return command
