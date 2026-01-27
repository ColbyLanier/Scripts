from cli_tools.subagents import terminal_launcher as tl


class DummyPath:
    def __init__(self, value: str) -> None:
        self._value = value

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._value


def test_normalize_wsl_unc_path_basic() -> None:
    path = "\\\\wsl.localhost\\Ubuntu\\home\\token\\project"
    assert tl._normalize_wsl_unc_path(path) == "/home/token/project"


def test_normalize_wsl_unc_path_unc_prefix() -> None:
    path = "\\\\?\\UNC\\wsl.localhost\\Ubuntu\\home\\token"
    assert tl._normalize_wsl_unc_path(path) == "/home/token"


def test_normalize_wsl_unc_path_wsl_dollar() -> None:
    path = "\\\\wsl$\\Ubuntu-20.04\\home\\token"
    assert tl._normalize_wsl_unc_path(path) == "/home/token"


def test_build_wsl_shell_command_uses_normalized_path() -> None:
    working_dir = DummyPath("\\\\wsl.localhost\\Ubuntu\\home\\token\\workspace")
    command = tl._build_wsl_shell_command(["echo", "hello"], working_dir, keep_open=False)
    assert "cd /home/token/workspace" in command


def test_windows_path_to_wsl_drive() -> None:
    assert tl._windows_path_to_wsl("C:\\Windows\\System32") == "/mnt/c/Windows/System32"


def test_windows_path_to_wsl_passthrough() -> None:
    assert tl._windows_path_to_wsl("/mnt/d/projects") == "/mnt/d/projects"


def test_windows_path_to_wsl_unc_returns_none() -> None:
    assert tl._windows_path_to_wsl("\\\\server\\share") is None


def test_build_wsl_command_args_includes_distro(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    args = tl._build_wsl_command_args("/home/token", "echo hi")
    assert args[:5] == ["wsl.exe", "-d", "Ubuntu-22.04", "--cd", "/home/token"]
