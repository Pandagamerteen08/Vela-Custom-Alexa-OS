#!/usr/bin/env python3
"""Apply and verify kernel-config fragments without scripts/config.

The vendor tree ships a LineageOS config with Android turned on. We do not want
to hand-edit it (it is 4000 lines and the next `make oldconfig` will re-ask),
and we cannot use `scripts/config` because it needs a prepared tree. So: a
fragment applies symbol-by-symbol, in place, and a separate assertion list makes
the build fail if a required symbol did not take.

Fragment syntax (comments start with '# ' followed by space):

    CONFIG_SQUASHFS=y
    CONFIG_SQUASHFS_XZ=y
    CONFIG_CMDLINE="console=ttyMT0,921600n1"
    CONFIG_MTK_FB=y
    -CONFIG_DRM            # force off  ->  "# CONFIG_DRM is not set"
    CONFIG_DEBUG_FS=n      # same thing

Usage:
    ./kconfig.py apply  --config .config --fragment ../kernel/cronos-prod.fragment -o .config.new
    ./kconfig.py check  --config .config --assert   ../kernel/cronos-prod.assert
    ./kconfig.py diff   --config a.config --other b.config
"""

import argparse
import os
import re
import sys

LINE_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")


class KconfigError(Exception):
    pass


def parse_config(text):
    """Return (order, values). order keeps file order so diffs stay readable."""
    order = []
    values = {}
    for line in text.splitlines():
        line = line.rstrip()
        m = LINE_RE.match(line)
        if m:
            order.append(m.group(1))
            values[m.group(1)] = m.group(2)
            continue
        m = UNSET_RE.match(line)
        if m:
            order.append(m.group(1))
            values[m.group(1)] = "n"
    return order, values


def render_line(sym, value):
    if value == "n":
        return f"# {sym} is not set"
    return f"{sym}={value}"


def parse_fragment(text):
    """Return list of (sym, value, origin_line)."""
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("# ") or line == "#":
            continue
        # strip trailing comments outside quotes
        if "#" in line:
            head, _, tail = line.partition("#")
            if head.count('"') % 2 == 0:
                line = head.strip()
                if not line:
                    continue
        if line.startswith("-"):
            sym = line[1:].strip()
            if not sym.startswith("CONFIG_"):
                raise KconfigError(f"line {lineno}: expected -CONFIG_NAME, got {raw!r}")
            out.append((sym, "n", lineno))
            continue
        m = LINE_RE.match(line)
        if not m:
            raise KconfigError(f"line {lineno}: not a config assignment: {raw!r}")
        sym, value = m.group(1), m.group(2).strip()
        if not value:
            raise KconfigError(f"line {lineno}: empty value for {sym}")
        out.append((sym, value, lineno))
    return out


def apply(config_text, fragments):
    order, values = parse_config(config_text)
    changes = []
    for frag_text, frag_name in fragments:
        for sym, value, lineno in parse_fragment(frag_text):
            before = values.get(sym)
            if before == value:
                continue
            changes.append((sym, before, value, f"{frag_name}:{lineno}"))
            values[sym] = value
            if sym not in order:
                order.append(sym)
    lines = []
    for sym in order:
        if sym in values:
            lines.append(render_line(sym, values[sym]))
    return "\n".join(lines) + "\n", changes


def check(config_text, assert_text):
    _, values = parse_config(config_text)
    problems = []
    for lineno, raw in enumerate(assert_text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            sym = line[1:].strip()
            got = values.get(sym, "n")
            if got != "n":
                problems.append(f"{sym} must be unset, is {got}")
            continue
        m = LINE_RE.match(line)
        if not m:
            raise KconfigError(f"assert line {lineno}: {raw!r}")
        sym, want = m.group(1), m.group(2).strip()
        got = values.get(sym)
        if got is None and want == "n":
            continue
        if got != want:
            problems.append(f"{sym} should be {want}, is {got if got is not None else '<missing>'}")
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply")
    a.add_argument("--config", required=True)
    a.add_argument("--fragment", action="append", required=True)
    a.add_argument("-o", "--out")
    a.add_argument("--quiet", action="store_true")

    c = sub.add_parser("check")
    c.add_argument("--config", required=True)
    c.add_argument("--assert", dest="assert_file", required=True)

    d = sub.add_parser("diff")
    d.add_argument("--config", required=True)
    d.add_argument("--other", required=True)

    args = p.parse_args(argv)
    try:
        if args.cmd == "apply":
            with open(args.config) as f:
                cfg = f.read()
            frags = []
            for path in args.fragment:
                with open(path) as f:
                    frags.append((f.read(), os.path.basename(path)))
            new_text, changes = apply(cfg, frags)
            if not args.quiet:
                for sym, before, after, origin in changes:
                    print(f"[kconfig] {sym}: {before if before is not None else '<unset>'} -> {after}   ({origin})")
                print(f"[kconfig] {len(changes)} symbol(s) changed")
            if args.out:
                with open(args.out, "w") as f:
                    f.write(new_text)
                print(f"[kconfig] wrote {args.out}")
            else:
                sys.stdout.write(new_text)
            return 0

        if args.cmd == "check":
            with open(args.config) as f:
                cfg = f.read()
            with open(args.assert_file) as f:
                asserts = f.read()
            problems = check(cfg, asserts)
            if problems:
                sys.stderr.write("[kconfig] FAILED assertions:\n")
                for prob in problems:
                    sys.stderr.write(f"  - {prob}\n")
                return 1
            print("[kconfig] all assertions satisfied")
            return 0

        with open(args.config) as f:
            _, a_vals = parse_config(f.read())
        with open(args.other) as f:
            _, b_vals = parse_config(f.read())
        for sym in sorted(set(a_vals) | set(b_vals)):
            av, bv = a_vals.get(sym, "n"), b_vals.get(sym, "n")
            if av != bv:
                print(f"{sym}: {av} -> {bv}")
        return 0
    except (OSError, KconfigError) as e:
        sys.stderr.write(f"kconfig: error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
