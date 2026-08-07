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
"""The upgrade matrix has to keep describing the code.

A document listing which Nova internals this driver depends on is worth
having only while it is complete. Left to drift it becomes worse than
nothing: an upgrade would be planned against a list that omits the
override that breaks.

These tests compare the document against the code, so adding or removing
a coupling point fails here until the matrix is updated in the same
change.
"""
import os
from pathlib import Path

from nova.compute import manager as base_manager
from nova import test

from nova.virt.incus import manager as incus_manager


REPO_ROOT = Path(__file__).parents[5]
MATRIX = REPO_ROOT / "doc" / "source" / "upgrade_matrix.rst"
NOVA_PATCHES = REPO_ROOT / "patches" / "nova"


def _overridden(predicate):
    base = set(dir(base_manager.ComputeManager))
    own = {
        name for name, value in vars(incus_manager.IncusComputeManager).items()
        if callable(value)
    }
    return {name for name in own & base if predicate(name)}


class UpgradeMatrixTest(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.matrix = MATRIX.read_text(encoding="utf-8")

    def test_every_overridden_private_method_is_documented(self):
        """A private Nova method has no contract.

        It can change signature, call site or the state it is called
        with, in any release, without failing to import. Each one has to
        carry a recorded assumption and a way to re-check it.
        """
        overrides = _overridden(
            lambda name: name.startswith("_") and not name.startswith("__"))

        missing = sorted(
            name for name in overrides
            if "``%s``" % name not in self.matrix)

        self.assertEqual(
            [], missing,
            "these Nova private methods are overridden but absent from "
            "doc/source/upgrade_matrix.rst: %s" % ", ".join(missing))

    def test_the_matrix_documents_nothing_that_is_no_longer_overridden(self):
        # A stale entry is as misleading as a missing one: it sends the
        # next upgrade to re-verify an assumption nothing relies on.
        overrides = _overridden(
            lambda name: name.startswith("_") and not name.startswith("__"))
        candidates = {
            name for name in dir(base_manager.ComputeManager)
            if name.startswith("_") and not name.startswith("__")
        }

        stale = sorted(
            name for name in candidates - overrides
            if "``%s``" % name in self.matrix)

        self.assertEqual(
            [], stale,
            "doc/source/upgrade_matrix.rst documents overrides that no "
            "longer exist: %s" % ", ".join(stale))

    def test_every_nova_patch_is_documented(self):
        # Patches fail loudly on a version bump, but only if someone
        # knows to look for them.
        patches = sorted(
            os.path.splitext(name)[0]
            for name in os.listdir(NOVA_PATCHES)
            if name.endswith(".patch"))

        missing = sorted(
            name for name in patches
            if "``%s``" % name not in self.matrix)

        self.assertEqual(
            [], missing,
            "these Nova patches are absent from "
            "doc/source/upgrade_matrix.rst: %s" % ", ".join(missing))
