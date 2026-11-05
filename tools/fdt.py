#!/usr/bin/env python3
"""Minimal flattened-device-tree reader/writer (stdlib only).

Exists so the boot image can be patched on any host without dtc: the cronos DTBs
come from the vendor tree and we need to (a) drop `amzn,mic-downmix` so the two
microphones stop being averaged, and (b) see what /chosen/bootargs says before
we trust the boot header's cmdline.

Only the parts of the spec we need are implemented: header v17 (last compatible
v16), memory reservation map, and the FDT_BEGIN_NODE / FDT_END_NODE / FDT_PROP /
FDT_NOP / FDT_END tokens. Property values are opaque bytes; ordering is
preserved so an unmodified round-trip is byte-comparable in structure (the
string block is rebuilt, so the bytes may differ even when the tree does not).
"""

import struct

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9
HEADER_SIZE = 40


class FdtError(Exception):
    pass


class Node:
    __slots__ = ("name", "props", "children", "parent")

    def __init__(self, name, parent=None):
        self.name = name
        self.props = []          # list[(name:str, value:bytes)] in original order
        self.children = []
        self.parent = parent

    # -- property access ---------------------------------------------------
    def get(self, name):
        for n, v in self.props:
            if n == name:
                return v
        return None

    def get_str(self, name):
        v = self.get(name)
        return None if v is None else v.split(b"\0", 1)[0].decode(errors="replace")

    def set(self, name, value):
        if isinstance(value, str):
            value = value.encode() + b"\0"
        for i, (n, _) in enumerate(self.props):
            if n == name:
                self.props[i] = (n, value)
                return
        self.props.append((name, value))

    def delete(self, name):
        before = len(self.props)
        self.props = [(n, v) for n, v in self.props if n != name]
        return before - len(self.props)

    def child(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def path(self):
        parts = []
        node = self
        while node is not None and node.parent is not None:
            parts.append(node.name.split("@", 1)[0] if node.name else "")
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def full_path(self):
        parts = []
        node = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def __repr__(self):
        return f"<Node {self.full_path()} props={len(self.props)} children={len(self.children)}>"


class DeviceTree:
    def __init__(self):
        self.root = Node("")
        self.mem_rsv = []        # list[(addr, size)]
        self.boot_cpuid_phys = 0
        self.version = 17
        self.last_comp_version = 16

    # -- parsing -----------------------------------------------------------
    @classmethod
    def parse(cls, blob):
        if len(blob) < HEADER_SIZE:
            raise FdtError("blob shorter than an FDT header")
        (magic, totalsize, off_struct, off_strings, off_rsv, version,
         last_comp, cpuid, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
        if magic != FDT_MAGIC:
            raise FdtError(f"bad FDT magic 0x{magic:08x} (expected 0xd00dfeed). "
                           "The vendor kernel may use a QCDT container; extract the "
                           "appended DTB with 'binwalk' or look for the second "
                           "0xd00dfeed occurrence.")
        if totalsize > len(blob):
            raise FdtError(f"FDT totalsize {totalsize} > blob {len(blob)} (truncated?)")
        if version < 16:
            raise FdtError(f"FDT version {version} is too old (need >= 16)")

        self = cls()
        self.version = version
        self.last_comp_version = last_comp
        self.boot_cpuid_phys = cpuid

        pos = off_rsv
        while True:
            if pos + 16 > totalsize:
                raise FdtError("memory reservation map runs past the end of the blob")
            addr, size = struct.unpack_from(">QQ", blob, pos)
            pos += 16
            if addr == 0 and size == 0:
                break
            self.mem_rsv.append((addr, size))

        strings = blob[off_strings:off_strings + size_strings]
        if len(strings) != size_strings:
            raise FdtError("strings block truncated")

        pos = off_struct
        end = off_struct + size_struct
        stack = [self.root]
        first = True      # the first BEGIN_NODE *is* self.root, not its child
        root_closed = False
        while pos < end:
            (tag,) = struct.unpack_from(">I", blob, pos)
            pos += 4
            if tag == FDT_BEGIN_NODE:
                name, pos = _read_cstr(blob, pos, end)
                pos = (pos + 3) & ~3      # node names are padded to 4 bytes
                if first:
                    self.root.name = name
                    first = False
                    continue
                node = Node(name, parent=stack[-1])
                stack[-1].children.append(node)
                stack.append(node)
            elif tag == FDT_END_NODE:
                if len(stack) == 1:
                    # the closing token for the root node itself
                    root_closed = True
                    continue
                stack.pop()
            elif tag == FDT_PROP:
                if pos + 8 > end:
                    raise FdtError("truncated property header")
                length, nameoff = struct.unpack_from(">II", blob, pos)
                pos += 8
                value = blob[pos:pos + length]
                pos += length
                pos = (pos + 3) & ~3
                if nameoff >= len(strings):
                    raise FdtError(f"property name offset {nameoff} outside strings block")
                pname, _ = _read_cstr(strings, nameoff, len(strings))
                stack[-1].props.append((pname, bytes(value)))
            elif tag == FDT_NOP:
                continue
            elif tag == FDT_END:
                break
            else:
                raise FdtError(f"unknown FDT token 0x{tag:x} at offset {pos - 4}")
        if len(stack) != 1:
            raise FdtError("unterminated node(s) at FDT_END")
        if not root_closed:
            raise FdtError("root node was never closed (truncated struct block)")
        return self

    # -- serialising -------------------------------------------------------
    def to_bytes(self):
        strings = bytearray()
        string_offsets = {}

        def intern(name):
            if name not in string_offsets:
                string_offsets[name] = len(strings)
                strings.extend(name.encode())
                strings.append(0)
            return string_offsets[name]

        struct_block = bytearray()

        def emit(node):
            struct_block.extend(struct.pack(">I", FDT_BEGIN_NODE))
            struct_block.extend(node.name.encode() + b"\0")
            while len(struct_block) % 4:
                struct_block.append(0)
            for name, value in node.props:
                struct_block.extend(struct.pack(">III", FDT_PROP, len(value), intern(name)))
                struct_block.extend(value)
                while len(struct_block) % 4:
                    struct_block.append(0)
            for c in node.children:
                emit(c)
            struct_block.extend(struct.pack(">I", FDT_END_NODE))

        emit(self.root)
        struct_block.extend(struct.pack(">I", FDT_END))

        rsv = bytearray()
        for addr, size in self.mem_rsv:
            rsv.extend(struct.pack(">QQ", addr, size))
        rsv.extend(struct.pack(">QQ", 0, 0))

        off_rsv = HEADER_SIZE
        off_struct = _align(off_rsv + len(rsv), 4)
        off_strings = _align(off_struct + len(struct_block), 4)
        totalsize = off_strings + len(strings)

        out = bytearray()
        out.extend(struct.pack(
            ">10I", FDT_MAGIC, totalsize, off_struct, off_strings, off_rsv,
            self.version, self.last_comp_version, self.boot_cpuid_phys,
            len(strings), len(struct_block)))
        out.extend(b"\0" * (off_rsv - len(out)))
        out.extend(rsv)
        out.extend(b"\0" * (off_struct - len(out)))
        out.extend(struct_block)
        out.extend(b"\0" * (off_strings - len(out)))
        out.extend(strings)
        if len(out) != totalsize:
            raise FdtError("internal: serialised size mismatch")
        return bytes(out)

    # -- helpers -----------------------------------------------------------
    def find(self, path):
        if path in ("/", ""):
            return self.root
        node = self.root
        for part in [p for p in path.split("/") if p]:
            nxt = node.child(part)
            if nxt is None:
                # allow alias by basename without the unit address
                matches = [c for c in node.children if c.name.split("@", 1)[0] == part]
                if len(matches) != 1:
                    return None
                nxt = matches[0]
            node = nxt
        return node

    def delete_prop_everywhere(self, name):
        hits = []
        for node in self.root.walk():
            if node.delete(name):
                hits.append(node.full_path())
        return hits

    def bootargs(self):
        chosen = self.find("/chosen")
        return None if chosen is None else chosen.get_str("bootargs")


def _read_cstr(buf, pos, end):
    zero = buf.find(b"\0", pos, end)
    if zero < 0:
        raise FdtError("unterminated string in FDT")
    return buf[pos:zero].decode(errors="replace"), zero + 1


def _align(n, a):
    return (n + a - 1) // a * a


def set_prop_typed(node, name, spec):
    """spec is 'str:VALUE', 'u32:N[,N...]', or 'bytes:HEX'."""
    kind, _, value = spec.partition(":")
    if kind == "str":
        node.set(name, value)
    elif kind == "u32":
        words = [int(v, 0) for v in value.split(",") if v != ""]
        node.set(name, struct.pack(f">{len(words)}I", *words))
    elif kind in ("bytes", "hex"):
        node.set(name, bytes.fromhex(value))
    else:
        raise FdtError(f"unknown value type {kind!r} (use str:, u32:, bytes:)")
