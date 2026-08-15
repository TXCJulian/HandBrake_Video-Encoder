import sys
import textwrap

import pytest


@pytest.fixture
def fake_handbrake(tmp_path):
    """Install a scriptable stand-in for HandBrakeCLI as a fake binary.

    Returns a factory: call it with python source that runs as the fake
    binary. Tests use this instead of a real encode so the suite needs no
    media files, no codecs, and no GPU.

    Windows note: a `#!` shebang is not honoured by Windows' process
    creation (subprocess.Popen uses CreateProcess, which does not interpret
    shebangs), so a chmod+shebang script cannot be launched directly there.
    A .bat/.cmd shim was rejected: cmd.exe becomes the process Popen holds a
    handle to, and killing that handle leaves the real Python child running
    orphaned — breaking the exact cancellation behaviour under test. So the
    fake is written as a plain `.py` file and returned as a *list* command
    prefix ``[sys.executable, script_path]``, which tests splat into the
    command they build. Popen's first argument is then always the real
    python.exe, with no intermediate shell in the process tree and no PATH
    lookup involved.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def _install(body: str, name: str = "handbrake") -> list[str]:
        script = bin_dir / f"{name}.py"
        script.write_text(textwrap.dedent(body))
        return [sys.executable, str(script)]

    return _install
