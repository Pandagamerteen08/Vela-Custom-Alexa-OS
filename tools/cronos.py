#!/usr/bin/env python3
"""cronos.py - one cross-platform entry point for everything that is not flashing.

The shell scripts in this repository assume a POSIX host, which is correct for
the kernel and rootfs builds (they need chroot, QEMU user emulation and a
cross toolchain) but wrong as the *only* way in. This wrapper is pure standard
library Python 3.8+, so it runs the same way on Linux, macOS and Windows:

    python tools/cronos.py doctor                  what this machine can do
    python tools/cronos.py test                    every host check
    python tools/cronos.py release                 package dist/ for a GitHub release
    python tools/cronos.py docker <target>         run a Linux-only step in Docker

The split it enforces is deliberate:

  * host-only work (boot image, DTB, cpio, kconfig, package contract, A/B state
    machine, release packaging) must work on any OS, because that is where most
    of the review and CI happens;
  * kernel and rootfs work needs Linux semantics (chroot, binfmt, a cross
    toolchain) and on Windows/macOS is expected to run inside the container
    defined by ./Dockerfile.

Nothing here writes flash, and nothing here needs root.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import textwrap
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
PY = sys.executable

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def which(name):
    """shutil.which, plus the .exe dance Windows sometimes needs."""
    hit = shutil.which(name)
    if hit:
        return hit
    if IS_WINDOWS:
        for ext in (".exe", ".bat", ".cmd"):
            hit = shutil.which(name + ext)
            if hit:
                return hit
    return None


def run(cmd, cwd=None, env=None, capture=False):
    """Run a command, echo it, return (rc, output)."""
    printable = " ".join(str(c) for c in cmd)
    print(f"  $ {printable}")
    kwargs = dict(cwd=str(cwd or ROOT), env=env)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    else:
        kwargs.update(stdout=None, stderr=None)
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        print(f"  ! {cmd[0]}: not found")
        return 127, ""
    out = proc.stdout if capture else ""
    if capture and out:
        sys.stdout.write(out)
    return proc.returncode, out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def shell():
    """A POSIX shell usable for the .sh suites, or None."""
    if not IS_WINDOWS:
        return which("sh")
    # Git for Windows ships one; MSYS/Cygwin/WSL paths vary, so probe.
    for cand in ("sh", "bash", r"C:\Program Files\Git\bin\sh.exe",
                 r"C:\Program Files\Git\usr\bin\sh.exe"):
        hit = which(cand) if not os.path.isabs(cand) else (cand if os.path.exists(cand) else None)
        if hit:
            return hit
    return None


def git_rev():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=str(ROOT), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "nogit"


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

# (command, what it unlocks, required-for)
CHECKS = [
    ("python3", "this script, all host tools", "always"),
    ("git", "cloning the 4.9 kernel tree", "kernel"),
    ("docker", "the pinned Linux container (Windows/macOS route)", "container"),
    ("podman", "same, rootless", "container"),
    ("make", "make test / make boot", "convenience"),
    ("cc", "compiling slotctl for the A/B tests", "host-test"),
    ("aarch64-linux-gnu-gcc", "cross-compiling the arm64 kernel", "kernel"),
    ("mksquashfs", "packing the rootfs", "rootfs"),
    ("qemu-arm-static", "running armv7 apk inside a chroot", "rootfs"),
    ("dtc", "cross-checking the DTB patcher (optional)", "optional"),
    ("adb", "pushing slot images", "device"),
    ("fastboot", "flashing boot.img", "device"),
]


def cmd_doctor(args):
    print(f"cronos-min doctor")
    print(f"  os        {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"  python    {platform.python_version()} at {PY}")
    print(f"  root      {ROOT}")
    print()
    found = {}
    width = max(len(c[0]) for c in CHECKS) + 2
    print(f"  {'tool':<{width}} {'found':<7} purpose")
    print("  " + "-" * (width + 60))
    for name, purpose, need in CHECKS:
        hit = which(name)
        if name == "python3":
            hit = hit or which("python")
        found[name] = hit
        mark = "yes" if hit else "-"
        if not hit and need == "always":
            mark = "MISSING"
        print(f"  {name:<{width}} {mark:<7} {purpose}")
    print()

    want_docker = found["docker"] or found["podman"]
    host_tests = True  # if you are reading this, a working interpreter is running
    native_kernel = (platform.system() == "Linux" and found["aarch64-linux-gnu-gcc"]
                     and found["git"])
    native_rootfs = (platform.system() == "Linux" and found["mksquashfs"]
                     and found["qemu-arm-static"])

    print("  what you can do here")
    print(f"    host checks            {'yes' if host_tests else 'no'} "
          f"(python tools/cronos.py test)")
    if platform.system() == "Linux":
        print(f"    kernel build           {'yes' if native_kernel else 'no'}"
              f"{'' if native_kernel else '  -> install gcc-aarch64-linux-gnu (GCC <= 10)'}")
        print(f"    rootfs build           {'yes' if native_rootfs else 'no'}"
              f"{'' if native_rootfs else '  -> squashfs-tools + qemu-user-static + binfmt'}")
    else:
        print("    kernel build           no (Linux only)")
        print("    rootfs build           no (Linux only)")
        print(f"    via container          {'yes' if want_docker else 'no'}"
              f"{'' if want_docker else '  -> install Docker Desktop, then: cronos.py docker test'}")
    print(f"    flashing               "
          f"{'yes' if (found['adb'] and found['fastboot']) else 'no (adb + fastboot)'}")

    if platform.system() == "Darwin":
        print("\n  macOS note: the kernel step needs a case-sensitive filesystem. Run the")
        print("  container, or make a case-sensitive APFS volume and build there.")
    if IS_WINDOWS:
        print("\n  Windows note: use the container. The .sh suites run under Git Bash if")
        print("  you installed it; without one they are reported as skipped, not failed.")

    rc = 0 if found["python3"] else 1
    if not host_tests:
        rc = 1
    return rc


# --------------------------------------------------------------------------
# test
# --------------------------------------------------------------------------


def cmd_test(args):
    """Host checks. Python suite always; shell suites when a POSIX shell exists."""
    results = []

    print("== python unit tests (any OS)")
    rc, _ = run([PY, "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_*.py", "-v"])
    results.append(("python unit tests", "PASS" if rc == 0 else "FAIL"))

    sh = shell()
    cc = which("cc") or which("gcc")

    print("\n== slotctl state machine (needs a C compiler and a POSIX shell)")
    if sh and cc:
        env = dict(os.environ)
        env.setdefault("CC", cc)
        rc, _ = run([sh, "tests/slotctl.sh"], env=env)
        results.append(("slotctl", "PASS" if rc == 0 else "FAIL"))
    else:
        missing = " and ".join(m for m, v in (("shell", sh), ("C compiler", cc)) if not v)
        print(f"  SKIP: no {missing}. Install Git Bash / build-essential, or use the container.")
        results.append(("slotctl", "SKIP"))

    print("\n== rootfs packer + package contract (needs mksquashfs)")
    if sh and which("mksquashfs"):
        rc, _ = run([sh, "tests/mkrootfs.sh"])
        results.append(("rootfs packer", "PASS" if rc == 0 else "FAIL"))
    else:
        why = "no mksquashfs" if not which("mksquashfs") else "no POSIX shell"
        print(f"  SKIP: {why}. `sudo tools/mkrootfs.sh` on Linux, or the container.")
        results.append(("rootfs packer", "SKIP"))

    print("\n== summary")
    for name, status in results:
        print(f"  {name:<24} {status}")
    bad = [n for n, s in results if s == "FAIL"]
    skipped = [n for n, s in results if s == "SKIP"]
    if bad:
        print(f"\nFAILURES: {', '.join(bad)}")
        return 1
    if skipped:
        print(f"\npassed with {len(skipped)} suite(s) skipped on this host")
    else:
        print("\nALL CHECKS PASSED")
    return 0


# --------------------------------------------------------------------------
# release
# --------------------------------------------------------------------------

# Artifacts worth shipping. Missing ones are reported, not fatal: a release cut
# from a machine that has no kernel toolchain still produces a usable doc+tool
# pack, and says so in RELEASE.md.
ARTIFACTS = [
    ("build/boot.img", "boot partition image: arm64 4.9.337 + initramfs + DTB"),
    ("build/boot.img.dev", "debug kernel (use this when the panel stays dark)"),
    ("build/root.squashfs", "slot image: the armv7 root filesystem"),
    ("build/manifest.json", "rootfs manifest: sha256, size, build id"),
    ("build/initramfs.cpio.gz", "initramfs on its own (for hand-built boot images)"),
]

SOURCE_GLOBS = [
    "README.md", "CHANGELOG.md", "LICENSE", "Makefile", "Dockerfile",
    "docs", "kernel", "initramfs", "rootfs", "src", "tests", "tools",
]

RELEASE_TEMPLATE = """# cronos-min {version}

A minimal Alpine Linux for the Amazon Echo Show 5 (2nd gen, 2021, `cronos`,
MediaTek MT8163).

This Release is produced by `python tools/cronos.py release` from commit
`{rev}` at {stamp}. Read `docs/install.md` before flashing anything: the boot
image path is reversible, the preloader path is not.

## What is in this pack

{contents}

## Install in three steps

```sh
# 1. back up the stock images (from TWRP)
adb shell 'dd if=/dev/block/mmcblk0p9 of=/sdcard/boot-stock.img'
adb pull /sdcard/boot-stock.img .

# 2. prepare the A/B store once (p12), then flash our kernel
adb shell 'mke2fs -t ext4 -L cronos-store -m 0 /dev/block/mmcblk0p12'
fastboot flash boot boot.img

# 3. push the slot image and arm it
adb push root.squashfs /tmp/root.squashfs.new
adb shell 'mkdir -p /mnt && mount -t ext4 /dev/block/mmcblk0p12 /mnt && mkdir -p /mnt/slots/a'
adb shell 'cp /tmp/root.squashfs.new /mnt/slots/a/root.squashfs.new && sync && mv /mnt/slots/a/root.squashfs.new /mnt/slots/a/root.squashfs'
adb shell 'slotctl -f /mnt/slot.ptr init && reboot'
```

## Verify before you flash

```sh
sha256sum -c SHA256SUMS.txt        # Linux
shasum -a 256 -c SHA256SUMS.txt    # macOS
```

## What this build is not

- **Unsigned.** Production A/B manifests are Ed25519-signed; these local builds
  are not, and the initramfs will refuse to install a signed-only manifest.
- **Unverified on hardware** unless the notes below say otherwise. Every number
  in `docs/` is an estimate until `tools/measure.sh` prints it on a unit.
- **No `cronosd` yet**: no UI, audio, touch or network client. The first boot
  ends at a serial console by design.

## Known numbers

| | target | gate |
|---|---|---|
| idle RAM | 55-85 MiB | 100 MiB |
| root.squashfs | 7-10 MiB | 20 MiB |
| boot.img | 9.5-13 MiB | 15.5 MiB (p9 is 16 MB) |
| resident processes | 3 | - |
"""


def _iter_pack_files():
    """(path, arcname) for everything that goes into the pack, deterministic."""
    files = []

    # artifacts first: they are what most people came for
    for rel, _desc in ARTIFACTS:
        p = ROOT / rel
        if p.is_file():
            files.append((p, f"images/{p.name}"))

    # then source and docs, walk in sorted order
    skip_dirs = {".git", "build", "dist", "__pycache__", "kernel/src", "node_modules"}
    for entry in SOURCE_GLOBS:
        top = ROOT / entry
        if top.is_file():
            files.append((top, entry))
            continue
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in skip_dirs)
            rel_dir = Path(dirpath).relative_to(ROOT)
            if rel_dir.as_posix() in skip_dirs:
                continue
            for name in sorted(filenames):
                if name.endswith((".pyc", ".o", ".img")):
                    continue
                p = Path(dirpath) / name
                files.append((p, str(rel_dir / name).replace(os.sep, "/")))
    # de-duplicate while keeping order
    seen, out = set(), []
    for p, arc in files:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append((p, arc))
    return out


def cmd_release(args):
    version = args.version or _version_from_git()
    out = Path(args.out or (ROOT / "dist")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    files = _iter_pack_files()
    present = {p.name for p, _ in files}
    missing = [rel for rel, _ in ARTIFACTS if Path(rel).name not in present]

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rev = git_rev()

    # per-file checksums, in the format both sha256sum(1) and shasum(1) read
    sums = {arc: sha256(p) for p, arc in files}
    checks_lines = [f"{digest}  {arc}" for arc, digest in sorted(sums.items())]

    rows = []
    for rel, desc in ARTIFACTS:
        p = ROOT / rel
        exists = p.is_file()
        rows.append(f"| `{p.name}` | {'yes' if exists else 'not built'} | "
                    f"{human(p.stat().st_size) if exists else '-'} | {desc} |")
    contents = "| file | present | size | what it is |\n|---|---|---|---|\n" + "\n".join(rows)

    release_md = RELEASE_TEMPLATE.format(version=version, rev=rev, stamp=stamp,
                                         contents=contents)

    stage = out / f"cronos-min-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    written = []
    for p, arc in files:
        dest = stage / arc
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        written.append((dest, arc))
    (stage / "RELEASE.md").write_text(release_md, encoding="utf-8")
    (stage / "SHA256SUMS.txt").write_text("\n".join(checks_lines) + "\n", encoding="utf-8")

    tarball = out / f"cronos-min-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for p, arc in written:
            tf.add(p, arcname=f"cronos-min-{version}/{arc}")
        for name in ("RELEASE.md", "SHA256SUMS.txt"):
            tf.add(stage / name, arcname=f"cronos-min-{version}/{name}")

    zippath = out / f"cronos-min-{version}.zip"
    with zipfile.ZipFile(zippath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, arc in written:
            zf.write(p, arcname=f"cronos-min-{version}/{arc}")
        for name in ("RELEASE.md", "SHA256SUMS.txt"):
            zf.write(stage / name, arcname=f"cronos-min-{version}/{name}")

    digest_lines = [f"{sha256(tarball)}  {tarball.name}",
                    f"{sha256(zippath)}  {zippath.name}"]
    (out / "SHA256SUMS.txt").write_text("\n".join(digest_lines) + "\n", encoding="utf-8")

    manifest = {
        "version": version,
        "commit": rev,
        "built_at": stamp,
        "packs": {p.name: {"sha256": sha256(p), "size": p.stat().st_size}
                  for p in (tarball, zippath)},
        "artifacts": {Path(rel).name: (sha256(ROOT / rel) if (ROOT / rel).is_file() else None)
                      for rel, _ in ARTIFACTS},
        "files": len(written),
    }
    (out / "release.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                      encoding="utf-8")

    print()
    print(f"release {version} (commit {rev})")
    for p in (tarball, zippath):
        print(f"  {p.name:<34} {human(p.stat().st_size)}")
    print(f"  files in pack: {len(written)}")
    if missing:
        print(f"  not built on this host: {', '.join(Path(m).name for m in missing)}")
        print("  (the pack still installs nothing for those; build first with")
        print("   `tools/mkrootfs.sh` + `kernel/build.sh` or `cronos.py docker release`)")
    print(f"\nnext: attach dist/* to a GitHub release, or push a v{version} tag and let")
    print(".github/workflows/release.yml do it.")
    return 0


def _version_from_git():
    try:
        tag = subprocess.check_output(["git", "describe", "--tags", "--always", "--dirty"],
                                      cwd=str(ROOT), text=True,
                                      stderr=subprocess.DEVNULL).strip()
    except Exception:
        tag = ""
    return tag or "0.1.0-dev"


# --------------------------------------------------------------------------
# docker
# --------------------------------------------------------------------------

IMAGE = "cronos-min-build"
CONTAINER_TARGETS = {
    "test": ["bash", "-lc", "python3 tools/cronos.py test"],
    "initramfs": ["bash", "-lc", "tools/mkinitramfs.sh"],
    "rootfs": ["bash", "-lc", "tools/mkrootfs.sh"],
    "kernel": ["bash", "-lc", "kernel/build.sh"],
    "kernel-check": ["bash", "-lc", "kernel/build.sh --check"],
    "release": ["bash", "-lc",
                "tools/mkrootfs.sh && kernel/build.sh --check && "
                "python3 tools/cronos.py release"],
    "shell": ["bash", "-l"],
}


def _docker():
    return which("docker") or which("podman")


def cmd_docker(args):
    de = _docker()
    if not de:
        print("docker/podman not found. Install Docker Desktop (Windows/macOS) or")
        print("docker.io (Linux), or run the steps natively on a Linux host.")
        return 1
    target = args.target
    if target not in CONTAINER_TARGETS:
        print(f"unknown target '{target}'. one of: {', '.join(CONTAINER_TARGETS)}")
        return 2

    have = subprocess.run([de, "image", "inspect", IMAGE],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0
    if not have or args.rebuild:
        print(f"building {IMAGE} (first time takes a few minutes)")
        rc, _ = run([de, "build", "-t", IMAGE, "."])
        if rc != 0:
            return rc

    interactive = args.tty and sys.stdout.isatty()
    cmd = [de, "run", "--rm"] + (["-it"] if interactive else [])
    # --privileged: tools/mkrootfs.sh chroots and runs armv7 binaries under
    # QEMU. Without it, chroot still works but apk's armv7 triggers may not.
    cmd += ["--privileged", "-v", f"{ROOT}:/work", "-w", "/work", IMAGE]
    cmd += CONTAINER_TARGETS[target]
    return run(cmd)[0] or 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cronos.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="report what this machine can build")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("test", help="run every host check this machine can run")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("release", help="package dist/ for a GitHub release")
    p.add_argument("--version", help="version string (default: git describe)")
    p.add_argument("--out", help="output directory (default: dist/)")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("docker", help="run a Linux-only step in the pinned container")
    p.add_argument("target", nargs="?", default="test",
                   choices=sorted(CONTAINER_TARGETS))
    p.add_argument("--rebuild", action="store_true", help="rebuild the image first")
    p.add_argument("--tty", action="store_true", help="allocate a TTY (shell)")
    p.set_defaults(func=cmd_docker)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
