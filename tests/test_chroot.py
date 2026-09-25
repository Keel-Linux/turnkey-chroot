# Copyright (c) 2021-2026 TurnkeyLinux <admin@turnkeylinux.org>
"""Tests for the `chroot` module.

These tests deliberately avoid any real mounting or chrooting (both of which
need root and mutate system state). Instead `MagicMounts` and
`subprocess.run` are patched so we exercise the pure logic - argv
construction, command validation, mount/umount bookkeeping and the context
manager contract - in isolation.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
from typing import ClassVar
from unittest.mock import mock_open, patch

import pytest

import chroot


class DummyMagicMounts:
    """Stand-in for `MagicMounts` that records calls instead of mounting.

    `Chroot.__init__` instantiates `MagicMounts` (which would shell out to
    `mount`), so we swap in this no-op double to keep construction side-effect
    free while still letting us assert that `umount()` is reached.
    """

    instances: ClassVar[list[DummyMagicMounts]] = []

    def __init__(self, mnt_profile: dict[str, str], root: str = "/") -> None:
        self.profile = mnt_profile
        self.root = root
        self.umount_called = False
        DummyMagicMounts.instances.append(self)

    def mount(self) -> None:
        pass

    def umount(self) -> None:
        self.umount_called = True


@pytest.fixture
def patched_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[DummyMagicMounts]:
    """Patch out mounting and provide a TERM env var.

    `Chroot.__init__` reads `os.environ["TERM"]`, so we set it to avoid a
    KeyError on hosts/CI where TERM is unset. monkeypatch undoes the setattr
    automatically, so no explicit teardown is needed.
    """
    monkeypatch.setenv("TERM", "xterm")
    DummyMagicMounts.instances = []
    monkeypatch.setattr(chroot, "MagicMounts", DummyMagicMounts)
    return DummyMagicMounts.instances


def test_is_mounted_true() -> None:
    # second whitespace-separated field of each /proc/mounts line is the
    # mount point ("guest") the code matches against
    fake_mounts = (
        "proc /mnt/cr/proc proc rw 0 0\nsysfs /mnt/cr/sys sysfs rw 0 0\n"
    )
    with patch("builtins.open", mock_open(read_data=fake_mounts)):
        assert chroot.is_mounted("/mnt/cr/proc") is True


def test_is_mounted_false() -> None:
    fake_mounts = "proc /mnt/cr/proc proc rw 0 0\n"
    with patch("builtins.open", mock_open(read_data=fake_mounts)):
        assert chroot.is_mounted("/mnt/cr/sys") is False


def test_prepare_command_quotes_args(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")
    cmd = cr._prepare_command("ls", "-la", "/tmp")
    # final element is the shell-quoted command string passed to `sh -c`
    assert cmd == ["chroot", "/mnt/cr", "sh", "-c", "ls -la /tmp"]


def test_prepare_command_shell_quoting(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")
    # an arg containing a space must be quoted so it stays a single token
    cmd = cr._prepare_command("echo", "a b")
    assert cmd[-1] == "echo 'a b'"


@pytest.mark.parametrize("bad", [">", "<", "|"])
def test_prepare_command_rejects_redirects(
    patched_mounts: list[DummyMagicMounts],
    bad: str,
) -> None:
    cr = chroot.Chroot("/mnt/cr")
    # redirects/pipes can't be expressed safely through the argv-based
    # `chroot ... sh -c` invocation, so they must be refused
    with pytest.raises(chroot.ChrootError):
        cr._prepare_command("cat", bad, "file")


def test_run_builds_expected_argv(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    captured_cmd: list[str] = []
    captured_kwargs: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    cr = chroot.Chroot("/mnt/cr")
    with patch.object(chroot.subprocess, "run", fake_run):
        cr.run(["ls", "-la"])

    assert captured_cmd == ["chroot", "/mnt/cr", "sh", "-c", "ls -la"]
    # env is forced to the chroot's environ; check defaults to False
    assert captured_kwargs["env"] is cr.environ
    assert captured_kwargs["check"] is False


def test_run_check_override(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    captured_kwargs: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    cr = chroot.Chroot("/mnt/cr")
    with patch.object(chroot.subprocess, "run", fake_run):
        # caller-supplied check must survive the pop/re-pass and not raise a
        # duplicate-keyword TypeError
        cr.run(["true"], check=True)

    assert captured_kwargs["check"] is True


def test_mount_context_manager_unmounts(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    with chroot.mount("/mnt/cr") as mnt:
        assert isinstance(mnt, chroot.Chroot)
        assert mnt.path == "/mnt/cr"
        # nothing unmounted while still inside the context
        assert patched_mounts[0].umount_called is False
    # leaving the context must trigger teardown via the finally block
    assert patched_mounts[0].umount_called is True


# --- MagicMounts.umount -----------------------------------------------------


def _mounted_magicmounts(root: str = "/fake/root") -> chroot.MagicMounts:
    """Build a MagicMounts for MNT_DEFAULT without mounting anything.

    `__init__` calls `mount()`, which is patched out; the bookkeeping is then
    set as if both proc and devpts had been mounted.
    """
    with patch.object(chroot.MagicMounts, "mount"):
        mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), root=root)
    for name in mnt.mounted:
        mnt.mounted[name] = True
    return mnt


def test_umount_uses_plain_umount_not_force() -> None:
    """`umount --force` is refused for proc inside LXC containers."""
    mnt = _mounted_magicmounts()
    with patch("chroot.subprocess.run") as run:
        mnt.umount()
    argvs = [c.args[0] for c in run.call_args_list]
    assert argvs == [
        ["/usr/bin/umount", "/fake/root/proc"],
        ["/usr/bin/umount", "/fake/root/dev/pts"],
    ]
    assert not any(mnt.mounted.values())


def test_umount_falls_back_to_lazy_when_plain_fails() -> None:
    mnt = _mounted_magicmounts()

    def fake_run(argv: list[str], **_kwargs: object) -> None:
        if "--lazy" not in argv and argv[-1].endswith("/proc"):
            raise subprocess.CalledProcessError(32, argv)

    with patch("chroot.subprocess.run", side_effect=fake_run) as run:
        mnt.umount()
    argvs = [c.args[0] for c in run.call_args_list]
    assert ["/usr/bin/umount", "--lazy", "/fake/root/proc"] in argvs
    assert not any(mnt.mounted.values())


def test_umount_raises_mounterror_when_lazy_also_fails() -> None:
    mnt = _mounted_magicmounts()
    err = subprocess.CalledProcessError(32, ["umount"])
    with (
        patch("chroot.subprocess.run", side_effect=err),
        pytest.raises(chroot.MountError),
    ):
        mnt.umount()
    assert mnt.mounted["proc"] is True
    for name in mnt.mounted:
        mnt.mounted[name] = False


# --- Chroot.system / run / environ -------------------------------------------


def _capture_run(
    captured_cmd: list[str],
    captured_kwargs: dict[str, object],
    returncode: int = 0,
) -> object:
    def fake_run(
        cmd: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, returncode)

    return fake_run


def test_system_runs_the_command_through_bash(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    captured_cmd: list[str] = []
    captured_kwargs: dict[str, object] = {}

    cr = chroot.Chroot("/mnt/cr")
    with patch.object(
        chroot.subprocess,
        "run",
        _capture_run(captured_cmd, captured_kwargs, returncode=3),
    ):
        rc = cr.system("apt-get update")

    assert captured_cmd == [
        "chroot",
        "/mnt/cr",
        "/bin/bash",
        "-c",
        "apt-get update",
    ]
    assert captured_kwargs["env"] is cr.environ
    assert captured_kwargs["check"] is False
    assert rc == 3  # noqa: PLR2004


def test_system_without_a_command_opens_a_shell(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    captured_cmd: list[str] = []

    cr = chroot.Chroot("/mnt/cr")
    with patch.object(
        chroot.subprocess,
        "run",
        _capture_run(captured_cmd, {}),
    ):
        assert cr.system() == 0

    assert captured_cmd == ["chroot", "/mnt/cr", "/bin/bash"]


def test_run_splits_a_string_command(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    captured_cmd: list[str] = []

    cr = chroot.Chroot("/mnt/cr")
    with patch.object(
        chroot.subprocess,
        "run",
        _capture_run(captured_cmd, {}),
    ):
        cr.run("ls -la /tmp")

    assert captured_cmd == ["chroot", "/mnt/cr", "sh", "-c", "ls -la /tmp"]


def test_prepare_command_rejects_arguments_that_are_not_strings(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")
    # shlex.quote raises TypeError for anything but a string
    with pytest.raises(chroot.ChrootError, match="failed to prepare"):
        cr._prepare_command("sleep", 1)  # type: ignore[arg-type]


def test_environ_has_defaults_that_the_caller_can_override(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr", environ={"LC_ALL": "C.UTF-8", "X": "y"})

    assert cr.environ["HOME"] == "/root"
    assert cr.environ["TERM"] == "xterm"
    assert cr.environ["LC_ALL"] == "C.UTF-8"
    assert cr.environ["X"] == "y"
    assert cr.environ["PATH"].startswith("/usr/local/sbin:")


def test_profile_defaults_to_a_copy_of_mnt_default(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")

    assert cr.profile == chroot.MNT_DEFAULT
    assert cr.profile is not chroot.MNT_DEFAULT
    assert patched_mounts[0].profile is cr.profile


def test_given_profile_is_copied(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr", mnt_profile=chroot.MNT_FULL)

    assert cr.profile == chroot.MNT_FULL
    assert cr.profile is not chroot.MNT_FULL


def test_umount_delegates_to_magicmounts(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")

    cr.umount()

    assert patched_mounts[0].umount_called is True


def test_repr_lists_the_public_attributes(
    patched_mounts: list[DummyMagicMounts],
) -> None:
    cr = chroot.Chroot("/mnt/cr")
    cr._hidden = "not shown"

    text = repr(cr)

    assert text.startswith("\nchroot.Chroot(")
    assert "  path=/mnt/cr," in text
    assert "_hidden" not in text


# --- log level from the environment -----------------------------------------


def test_log_level_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The level is chosen at import time, so the module is reloaded."""
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("CHROOT_LOG_LEVEL", "info")
    importlib.reload(chroot)
    assert chroot.log_level == logging.INFO

    monkeypatch.setenv("CHROOT_LOG_LEVEL", "nonsense")
    importlib.reload(chroot)
    assert chroot.log_level == logging.WARNING

    monkeypatch.setenv("DEBUG", "1")
    importlib.reload(chroot)
    assert chroot.log_level == logging.DEBUG

    monkeypatch.undo()
    importlib.reload(chroot)
