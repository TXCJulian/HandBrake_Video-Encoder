"""Best-effort host GPU identification for the operator health response."""

import os
import re
import subprocess

_PCI_NAME = re.compile(
    r"(?:VGA compatible controller|3D controller|Display controller):\s*(.+)",
    re.IGNORECASE,
)


def detect_gpu_name() -> str | None:
    """Return a friendly GPU model when the container can see one.

    Hardware probing is deliberately advisory: a missing diagnostic utility or
    a container without PCI visibility must never make ``/health`` unhealthy.
    ``ENCODER_GPU_NAME`` is an explicit escape hatch for hosts that do not
    expose ``nvidia-smi``/``lspci`` but do know the model from deployment.
    """
    configured = os.getenv("ENCODER_GPU_NAME", "").strip()
    if configured:
        return configured

    commands = (
        [
            "nvidia-smi",
            "--query-gpu=name",
            "--format=csv,noheader,nounits",
        ],
        ["lspci", "-nn"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if result.returncode != 0:
            continue
        output = (result.stdout or "").strip()
        if not output:
            continue
        if command[0] == "nvidia-smi":
            return output.splitlines()[0].strip() or None
        for line in output.splitlines():
            match = _PCI_NAME.search(line)
            if match:
                return match.group(1).strip() or None
    return None
