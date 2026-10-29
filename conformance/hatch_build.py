"""Bundle the SDK's spec into the conformance package, so an installed wheel runs without --spec."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class SpecHook(BuildHookInterface):
    def initialize(self, version, build_data):
        spec = Path(self.root).parent / "spec" / "jaah-extension.json"
        if spec.is_file():  # absent when building a wheel from the sdist, which already carries the copy
            build_data["force_include"][str(spec)] = "jaah_conformance/jaah-extension.json"
