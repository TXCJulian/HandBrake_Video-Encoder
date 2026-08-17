from app import gpu


def test_configured_gpu_name_wins(monkeypatch):
    monkeypatch.setenv("ENCODER_GPU_NAME", "Intel Arc A770")
    assert gpu.detect_gpu_name() == "Intel Arc A770"


def test_nvidia_smi_name_is_used_when_available(monkeypatch):
    monkeypatch.delenv("ENCODER_GPU_NAME", raising=False)

    def fake_run(command, **_kwargs):
        assert command[0] == "nvidia-smi"
        return type("Result", (), {"returncode": 0, "stdout": "NVIDIA RTX 4090\n"})()

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    assert gpu.detect_gpu_name() == "NVIDIA RTX 4090"


def test_missing_diagnostics_are_non_fatal(monkeypatch):
    monkeypatch.delenv("ENCODER_GPU_NAME", raising=False)

    def fake_run(_command, **_kwargs):
        raise FileNotFoundError("diagnostic missing")

    monkeypatch.setattr(gpu.subprocess, "run", fake_run)
    assert gpu.detect_gpu_name() is None
