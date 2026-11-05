#!/usr/bin/env python3
"""Patch a cronos DTB: drop amzn,mic-downmix, fix bootargs, inspect the tree.

The vendor DTB sets `amzn,mic-downmix`, which makes the audio driver average
capture channels 0 and 1 — two microphones used as one. Deleting the property
yields two independent microphones for the beamformer/AEC, and it is a DTB edit,
so no kernel rebuild is needed.

Examples:
    ./patch-dtb.py dtb/mt8163.dtb -o dtb/mt8163-patched.dtb --no-mic-downmix
    ./patch-dtb.py dtb/mt8163.dtb --list                       # grep-able tree
    ./patch-dtb.py dtb/mt8163.dtb --list --props sound          # only one subtree
    ./patch-dtb.py in.dtb -o out.dtb --set /chosen:bootargs:str:"console=ttyMT0,921600n1"

The input may be a bare DTB or an appended one (a kernel Image with the DTB at
the end); --find-appended scans for the FDT magic and extracts it.
"""

import argparse
import struct
import sys

import fdt
from fdt import FdtError

FDT_MAGIC = struct.pack(">I", fdt.FDT_MAGIC)


def load(blob, find_appended=False, index=None):
    """Return (dtb_bytes, offset) from a bare or appended DTB blob."""
    offsets = []
    start = 0
    while True:
        i = blob.find(FDT_MAGIC, start)
        if i < 0:
            break
        # validate: totalsize must be sane and fit
        if i + 8 <= len(blob):
            (total,) = struct.unpack_from(">I", blob, i + 4)
            if 40 <= total <= len(blob) - i:
                offsets.append(i)
        start = i + 4
    if not offsets:
        raise FdtError("no FDT magic (0xd00dfeed) found in the input")
    if index is not None:
        if index >= len(offsets):
            raise FdtError(f"--index {index} but only {len(offsets)} FDT(s) found at {offsets}")
        off = offsets[index]
    elif find_appended or (offsets[0] != 0 and len(offsets) > 1):
        off = offsets[-1]
    else:
        off = offsets[0]
    if len(offsets) > 1:
        sys.stderr.write(f"patch-dtb: {len(offsets)} FDT(s) at byte offsets {offsets}; "
                         f"using {off}. Override with --index.\n")
    (total,) = struct.unpack_from(">I", blob, off + 4)
    return blob[off:off + total], off


def cmd_list(tree, args):
    roots = [tree.root]
    if args.props:
        node = tree.find("/" + args.props.strip("/")) if args.props != "/" else tree.root
        if node is None:
            sys.stderr.write(f"patch-dtb: no node at /{args.props}\n")
            return 1
        roots = [node]
    for root in roots:
        for node in root.walk():
            depth = node.full_path().count("/") - 1
            print("  " * depth + (node.name or "/"))
            if args.quiet:
                continue
            for name, value in node.props:
                print("  " * (depth + 1) + f"{name} = {_render(value)}")
    return 0


def _render(value):
    if not value:
        return "<empty>"
    # printable NUL-terminated string?
    if value.endswith(b"\0") and all(32 <= c < 127 or c == 0 for c in value[:-1]):
        parts = [p.decode() for p in value.split(b"\0") if p]
        if len(parts) == 1:
            return f'"{parts[0]}"'
        return "[" + ", ".join(f'"{p}"' for p in parts) + "]"
    if len(value) % 4 == 0 and len(value) <= 64:
        words = struct.unpack(f">{len(value) // 4}I", value)
        if all(w < 0x10000 for w in words):
            return "<" + " ".join(f"0x{w:x}" for w in words) + ">"
    if len(value) <= 16:
        return "[" + " ".join(f"{b:02x}" for b in value) + "]"
    return f"[{len(value)} bytes]"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dtb")
    p.add_argument("-o", "--out")
    p.add_argument("--no-mic-downmix", action="store_true",
                   help="delete amzn,mic-downmix wherever it appears")
    p.add_argument("--delete-prop", action="append", default=[], metavar="NAME")
    p.add_argument("--set", action="append", default=[], metavar="PATH:NAME:SPEC",
                   help="set a property, e.g. /chosen:bootargs:str:'console=ttyMT0'")
    p.add_argument("--bootargs", help="shorthand for --set /chosen:bootargs:str:VALUE")
    p.add_argument("--show-bootargs", action="store_true")
    p.add_argument("--list", action="store_true", help="print the tree")
    p.add_argument("--props", metavar="SUBTREE", help="with --list, only this subtree")
    p.add_argument("--quiet", action="store_true", help="with --list, names only")
    p.add_argument("--find-appended", action="store_true")
    p.add_argument("--index", type=int)
    p.add_argument("--print-offset", action="store_true",
                   help="print the byte offset of the DTB in the input (for appended DTBs)")
    args = p.parse_args(argv)

    try:
        with open(args.dtb, "rb") as f:
            blob = f.read()
        dtb, offset = load(blob, args.find_appended, args.index)
        if args.print_offset:
            print(f"DTB at byte offset {offset}, {len(dtb)} bytes")
        tree = fdt.DeviceTree.parse(dtb)
    except (OSError, FdtError) as e:
        sys.stderr.write(f"patch-dtb: error: {e}\n")
        return 1

    if args.list:
        return cmd_list(tree, args)

    changed = 0
    if args.no_mic_downmix:
        hits = tree.delete_prop_everywhere("amzn,mic-downmix")
        if hits:
            for h in hits:
                print(f"[patch-dtb] removed amzn,mic-downmix from {h}")
            changed += len(hits)
        else:
            sys.stderr.write("[patch-dtb] warning: amzn,mic-downmix not found. "
                             "Either already patched or this DTB is not the cronos "
                             "audio node; check with --list and grep for 'mic'.\n")

    for name in args.delete_prop:
        hits = tree.delete_prop_everywhere(name)
        if not hits:
            sys.stderr.write(f"[patch-dtb] warning: {name} not found anywhere\n")
        for h in hits:
            print(f"[patch-dtb] removed {name} from {h}")
        changed += len(hits)

    sets = list(args.set)
    if args.bootargs:
        sets.append(f"/chosen:bootargs:str:{args.bootargs}")
    for spec in sets:
        try:
            path, name, value_spec = spec.split(":", 2)
        except ValueError:
            sys.stderr.write(f"patch-dtb: bad --set {spec!r} (want PATH:NAME:TYPE:VALUE)\n")
            return 1
        node = tree.find(path)
        if node is None:
            sys.stderr.write(f"patch-dtb: no node at {path!r}\n")
            return 1
        try:
            fdt.set_prop_typed(node, name, value_spec)
        except FdtError as e:
            sys.stderr.write(f"patch-dtb: {e}\n")
            return 1
        print(f"[patch-dtb] set {path}:{name} = {value_spec}")
        changed += 1

    if args.show_bootargs or args.bootargs:
        print(f"[patch-dtb] /chosen bootargs = {tree.bootargs()!r}")

    if not changed:
        if args.out:
            # Idempotence matters: build.sh re-patches a DTB that may already be
            # patched, and aborting there would break every rebuild.
            with open(args.out, "wb") as f:
                f.write(dtb)
            print(f"[patch-dtb] no changes; wrote an identical copy to {args.out}")
            return 0
        sys.stderr.write("patch-dtb: nothing to do (no patch flags given). "
                         "Use --list to inspect, or --no-mic-downmix.\n")
        return 2 if not args.show_bootargs and not args.print_offset else 0

    out = tree.to_bytes()
    if args.out:
        with open(args.out, "wb") as f:
            f.write(out)
        print(f"[patch-dtb] wrote {args.out}: {len(dtb)} -> {len(out)} bytes")
    elif not args.show_bootargs:
        sys.stderr.write("patch-dtb: no --out given, patch discarded\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
