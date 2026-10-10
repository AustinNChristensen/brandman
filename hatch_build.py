"""Include a fresh dashboard in distributable wheels, never in editable installs."""
from pathlib import Path
import subprocess
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class DashboardBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version == "editable":
            return
        root = Path(self.root)
        subprocess.run([sys.executable, str(root / "scripts" / "build_dashboard.py")],
                       cwd=root, check=True)
        build_data["force_include"][str(root / "brandman" / "static" / "app")] = "brandman/static/app"
