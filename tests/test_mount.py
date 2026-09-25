# Copyright (c) 2026 TurnkeyLinux <admin@turnkeylinux.org>
"""Tests for `chroot.mount` and `MagicMounts` with `subprocess.run` patched.

`MagicMounts` shells out to `mount` and `umount`; both need root and change
the host. Here `subprocess.run` records the argv it was given instead, and
`is_mounted` answers from a set, so the tests check the commands each mount
profile produces, the bookkeeping in `mounted`, the `MountError` paths and
the teardown contract of the context manager and of `__del__`.
"""

from __future__ import annotations

import gc
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

import chroot

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = "/mnt/cr"


class BoomError(Exception):
    """Raised inside a context to check that teardown still happens."""


@pytest.fixture
def run() -> Iterator[MagicMock]:
    """Patch `subprocess.run` for the whole test, teardown included.

    `MagicMounts.__del__` unmounts whatever is still marked as mounted, so the
    patch must outlive every instance a test creates: garbage is collected
    before the patch is removed.
    """
    with patch.object(chroot.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        yield mock_run
        gc.collect()


@pytest.fixture
def already_mounted(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Replace the /proc/mounts lookup with a set of mounted paths."""
    mounted: set[str] = set()
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(chroot, "is_mounted", lambda path: path in mounted)
    return mounted


def argvs(run: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in run.call_args_list]


# --- MagicMounts.mount ------------------------------------------------------


def test_default_profile_mounts_proc_and_devpts_by_type(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)

    assert argvs(run) == [
        ["mount", "--type", "proc", "proc", f"{ROOT}/proc"],
        ["mount", "--type", "devpts", "pts", f"{ROOT}/dev/pts"],
    ]
    assert mnt.switch == "--type"
    assert mnt.mounted == {"proc": True, "devpts": True}
    mnt.umount()


def test_full_profile_bind_mounts_from_the_host(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts(dict(chroot.MNT_FULL), ROOT)

    assert argvs(run) == [
        ["mount", "--bind", "/proc", f"{ROOT}/proc"],
        ["mount", "--bind", "/dev", f"{ROOT}/dev"],
        ["mount", "--bind", "/sys", f"{ROOT}/sys"],
        ["mount", "--bind", "/run", f"{ROOT}/run"],
    ]
    assert all(mnt.mounted.values())
    mnt.umount()


def test_type_profile_knows_sysfs_and_tmpfs(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    profile = {"switch": "--type", "sysfs": "sys", "tmpfs": "tmp"}
    mnt = chroot.MagicMounts(profile, ROOT)

    assert argvs(run) == [
        ["mount", "--type", "sysfs", "sys", f"{ROOT}/sys"],
        ["mount", "--type", "tmpfs", "tmpfs", f"{ROOT}/tmp"],
    ]
    mnt.umount()


def test_dev_is_always_bind_mounted_even_in_a_type_profile(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts({"switch": "--type", "dev": "dev"}, ROOT)

    assert argvs(run) == [["mount", "--bind", "/dev", f"{ROOT}/dev"]]
    mnt.umount()


def test_type_profile_with_an_unknown_name_passes_no_arguments(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    # no branch of the --type table matches, so only the switch is passed;
    # the behaviour is recorded here, not endorsed
    mnt = chroot.MagicMounts({"switch": "--type", "cgroup": "cg"}, ROOT)

    assert argvs(run) == [["mount", "--type"]]
    mnt.umount()


def test_root_is_made_absolute(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts({"switch": "--type", "proc": "proc"}, "cr")

    assert mnt.path["proc"].startswith("/")
    assert mnt.path["proc"].endswith("/cr/proc")
    mnt.umount()


def test_already_mounted_paths_are_skipped(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    already_mounted.add(f"{ROOT}/proc")

    mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)

    assert argvs(run) == [
        ["mount", "--type", "devpts", "pts", f"{ROOT}/dev/pts"],
    ]
    # a path mounted by someone else is not ours to unmount
    assert mnt.mounted == {"proc": False, "devpts": True}
    mnt.umount()


def test_unknown_switch_raises_mounterror_before_running_anything(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    with pytest.raises(chroot.MountError, match="Unknown switch"):
        chroot.MagicMounts({"switch": "--magic", "proc": "proc"}, ROOT)
    run.assert_not_called()


def test_failed_mount_raises_mounterror_and_keeps_earlier_mounts(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    def fake_run(argv: list[str], **_kwargs: object) -> None:
        if argv[-1].endswith("/dev/pts"):
            raise subprocess.CalledProcessError(32, argv)

    run.side_effect = fake_run
    with pytest.raises(chroot.MountError):
        chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)

    # proc was mounted before devpts failed; the instance that raised is
    # unreachable now, so __del__ unmounts it
    run.side_effect = None
    gc.collect()
    assert ["/usr/bin/umount", f"{ROOT}/proc"] in argvs(run)


# --- MagicMounts.umount and __del__ -----------------------------------------


def test_umount_unmounts_devpts_before_dev(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    profile = {"switch": "--bind", "dev": "dev", "devpts": "dev/pts"}
    mnt = chroot.MagicMounts(profile, ROOT)
    run.reset_mock()

    mnt.umount()

    assert argvs(run) == [
        ["/usr/bin/umount", f"{ROOT}/dev/pts"],
        ["/usr/bin/umount", f"{ROOT}/dev"],
    ]
    assert mnt.mounted == {"dev": False, "devpts": False}


def test_umount_skips_paths_that_were_never_mounted(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    already_mounted.update({f"{ROOT}/proc", f"{ROOT}/dev/pts"})
    mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)

    mnt.umount()

    run.assert_not_called()


def test_del_unmounts_what_is_still_mounted(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)
    run.reset_mock()

    del mnt
    gc.collect()

    assert argvs(run) == [
        ["/usr/bin/umount", f"{ROOT}/proc"],
        ["/usr/bin/umount", f"{ROOT}/dev/pts"],
    ]


def test_repr_lists_the_public_attributes(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    mnt = chroot.MagicMounts(dict(chroot.MNT_DEFAULT), ROOT)
    mnt._hidden = "not shown"

    text = repr(mnt)

    assert text.startswith("\nchroot.MagicMounts(")
    assert "  switch=--type," in text
    assert "_hidden" not in text
    mnt.umount()


# --- mount context manager --------------------------------------------------


def test_mount_context_manager_mounts_then_unmounts(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    with chroot.mount(ROOT) as mnt:
        assert isinstance(mnt, chroot.Chroot)
        assert all(mnt.magicmounts.mounted.values())
        run.reset_mock()

    assert not any(mnt.magicmounts.mounted.values())
    assert argvs(run) == [
        ["/usr/bin/umount", f"{ROOT}/proc"],
        ["/usr/bin/umount", f"{ROOT}/dev/pts"],
    ]


def test_mount_context_manager_unmounts_when_the_body_raises(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    def use_and_fail() -> None:
        with chroot.mount(ROOT):
            run.reset_mock()
            raise BoomError

    with pytest.raises(BoomError):
        use_and_fail()

    assert ["/usr/bin/umount", f"{ROOT}/proc"] in argvs(run)


def test_mount_with_the_full_profile_binds_and_leaves_the_table_intact(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    with chroot.mount(ROOT, mnt_profile=chroot.MNT_FULL) as mnt:
        assert mnt.magicmounts.switch == "--bind"
        assert ["mount", "--bind", "/run", f"{ROOT}/run"] in argvs(run)

    # the profile is copied, so the module table keeps its switch
    assert chroot.MNT_FULL["switch"] == "--bind"


def test_mount_error_propagates_from_the_context_manager(
    run: MagicMock,
    already_mounted: set[str],
) -> None:
    run.side_effect = subprocess.CalledProcessError(32, ["mount"])

    with pytest.raises(chroot.MountError), chroot.mount(ROOT):
        pytest.fail("the body must not run when mounting fails")
