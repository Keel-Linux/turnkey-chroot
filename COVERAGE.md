# Test coverage baseline

Measured on 2026-09-24 against upstream master (52bcfa6), following the
project decision 0003 (90 percent floor per repository, 95 percent for every
file our changes touch).

## Measured baseline on master: 100 percent (2026-09-26)

fix/umount-in-lxc merged into master on 2026-09-26 (pull request #1, merge
commit 0b1c044). master now measures 100 percent lines and branches over
`chroot/__init__.py` with 40 tests, the same command as below. The gate in
`.github/workflows/tests.yml` stays at 95, the bar for project-authored
code; the sections that follow record the numbers before the merge.

## Baseline before the merge: 44 percent on upstream master

The package is one module, `chroot/__init__.py`, with `tests/test_chroot.py`
(10 tests upstream). Command, from the repository root with `pytest` and
`coverage` installed:

    PYTHONPATH=. coverage run --branch --source=chroot -m pytest -q tests \
      && coverage report -m

| File | Stmts | Miss | Branch | Partial | Cover |
|------|-------|------|--------|---------|-------|
| chroot/__init__.py | 152 | 75 | 52 | 4 | 44 percent |

Missing on upstream: lines 32 and 36 (log level from `DEBUG` and
`LOG_LEVEL`), 148 to 165 and 174 to 207 (`mount()` loop and its
`MountError`), 217 to 251 (`umount()` including the devpts ordering), 281
to 283, 307 to 308 (`environ` handling), 336 to 342 (`TypeError` in
`run()`), 383, 402 to 407 (`__repr__`).

## Measured on our branch fix/umount-in-lxc

Same command, 13 tests pass:

| File | Stmts | Miss | Branch | Partial | Cover |
|------|-------|------|--------|---------|-------|
| chroot/__init__.py | 156 | 53 | 52 | 5 | 59 percent |

The three added tests cover the new unmount path (plain `umount`, then
`--lazy`, never `--force`). Still missing: 32, 36, 174 to 207 (`mount()`),
249 to 250 and 258 to 263 (`umount()` error paths), 293 to 295, 319 to 320,
348 to 354, 395, 414 to 419.

## Our branches and the 95 percent bar

| Branch | File touched | Automated test |
|--------|--------------|----------------|
| fix/umount-in-lxc | chroot/__init__.py | Yes: 3 new tests for the changed `umount()` path, file at 59 percent. Below the 95 percent bar for a touched file. |
| fix/umount-in-lxc | tests/test_chroot.py | Test file. |

## Plan to reach 90 percent per file

One file, so the repository floor and the touched-file bar coincide at 95
percent. Method: `pytest` with `unittest.mock.patch` on `subprocess.run`
and `os.path` as the existing tests do; `fail_under = 95` committed in
`pyproject.toml`; `coverage report --fail-under=95` in the gate.

Priority order (size: small under 30 lines of test, medium under 150,
large above):

1. `chroot/__init__.py`, `mount()` lines 174 to 207 (medium). Test each
   mount in `self.path` being mounted, already mounted (skipped), and a
   `CalledProcessError` raised as `MountError` with the original args; the
   `mounted` map after a partial failure.
2. `umount()` lines 249 to 263 (small). devpts unmounted before dev,
   `--lazy` fallback failing too, exception type and the `mounted` flags
   left true on failure.
3. `run()` lines 293 to 295, 319 to 320, 348 to 354, 395 (small).
   `environ` given and not given, `TypeError` wrapped in `ChrootError`,
   string versus list command, `command` empty.
4. Module import branches lines 32 and 36 (small): reload the module with
   `DEBUG` set and with `LOG_LEVEL` set to each level name.
5. `__repr__` lines 414 to 419 (small): private attributes hidden, output
   ends with a newline.

Estimated 15 to 20 more tests, medium overall. Any address in a fixture
(for example a `resolv.conf` copied into the chroot) is IPv6, such as
`nameserver 2001:db8::53`.
