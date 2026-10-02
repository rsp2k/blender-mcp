"""Patch the 3D Viewport sidebar in a saved, uncompressed .blend.

Blender's Python API can show the sidebar but cannot set its width
(Region.width is read-only) or pick its tab (Region.active_panel_category
raises "read-only" when assigned in 5.2). Both are plain fields in the file:
ARegion.sizex/winx, and the region's panels_category_active stack, whose
first entry is the active tab. This walks the file's own SDNA to find them
and rewrites them in place, so nothing is inserted or moved.

Pure Python, no bpy: runs inside Blender or on the host.

    patch_sidebar(path, width=360, category="BlenderMCP")
    read_sidebar(path) -> [{"sizex": .., "winx": .., "categories": [..]}, ..]
"""

import struct

SPACE_VIEW3D = 1  # eSpace_Type
RGN_TYPE_UI = 4  # eRegion_Type

_INT = {"char": "b", "uchar": "B", "short": "h", "ushort": "H", "int": "i",
        "uint": "I", "float": "f", "int64_t": "q", "uint64_t": "Q"}


class BlendFile:
    def __init__(self, data: bytearray):
        self.data = data
        if data[:7] != b"BLENDER":
            raise ValueError("not an uncompressed .blend (save with compress=False)")
        if data[7:8] in (b"-", b"_"):  # legacy 12-byte header
            if data[7:8] != b"-" or data[8:9] != b"v":
                raise ValueError("only 64-bit little-endian files are supported")
            pos, bhead = 12, struct.Struct("<4siQii")  # code len old sdna nr
            order = ("code", "len", "old", "sdna", "nr")
        else:  # 5.x 17-byte header, large block headers
            pos, bhead = 17, struct.Struct("<4siQqq")  # code sdna old len nr
            order = ("code", "sdna", "old", "len", "nr")
        self.blocks = []  # (code, data offset, len, old address, sdna index)
        while pos + bhead.size <= len(data):
            h = dict(zip(order, bhead.unpack_from(data, pos)))
            code = h["code"].rstrip(b"\0")
            pos += bhead.size
            if code == b"ENDB":
                break
            self.blocks.append((code, pos, h["len"], h["old"], h["sdna"]))
            pos += h["len"]
        dna = next(b for b in self.blocks if b[0] == b"DNA1")
        self._parse_sdna(dna[1])
        self.by_old = {b[3]: b for b in self.blocks if b[0] == b"DATA"}

    def _parse_sdna(self, off):
        d = self.data

        def align(pos):  # SDNA pads to 4 bytes from the block start
            return off + ((pos - off + 3) & ~3)

        def take_names(pos, tag):
            assert d[pos:pos + 4] == tag, tag
            n = struct.unpack_from("<i", d, pos + 4)[0]
            pos += 8
            out = []
            for _ in range(n):
                end = d.index(b"\0", pos)
                out.append(d[pos:end].decode())
                pos = end + 1
            return out, align(pos)

        assert d[off:off + 4] == b"SDNA"
        names, pos = take_names(off + 4, b"NAME")
        types, pos = take_names(pos, b"TYPE")
        assert d[pos:pos + 4] == b"TLEN"
        tlen = struct.unpack_from(f"<{len(types)}h", d, pos + 4)
        pos = align(pos + 4 + 2 * len(types))
        assert d[pos:pos + 4] == b"STRC"
        count = struct.unpack_from("<i", d, pos + 4)[0]
        pos += 8
        self.structs = {}  # name -> (index, {field: (offset, type, name)})
        for index in range(count):
            t, nfields = struct.unpack_from("<hh", d, pos)
            pos += 4
            fields, at = {}, 0
            for _ in range(nfields):
                ft, fn = struct.unpack_from("<hh", d, pos)
                pos += 4
                name = names[fn]
                if name.startswith("*") or name.startswith("("):
                    size = 8
                else:
                    size = tlen[ft]
                for dim in name.split("[")[1:]:
                    size *= int(dim.rstrip("]"))
                bare = name.lstrip("*(").split("[")[0].split(")")[0]
                fields[bare] = (at, types[ft], name)
                at += size
            self.structs[types[t]] = (index, fields)

    def field(self, struct_name, name):
        return self.structs[struct_name][1][name]

    def get(self, base, struct_name, name):
        at, typ, raw = self.field(struct_name, name)
        if raw.startswith("*"):
            return struct.unpack_from("<Q", self.data, base + at)[0]
        return struct.unpack_from("<" + _INT[typ], self.data, base + at)[0]

    def put(self, base, struct_name, name, value):
        at, typ, _ = self.field(struct_name, name)
        struct.pack_into("<" + _INT[typ], self.data, base + at, value)

    def chain(self, first, struct_name):
        """Data offsets of a ListBase, following next pointers."""
        seen = set()
        while first and first not in seen:
            seen.add(first)
            block = self.by_old.get(first)
            if block is None:
                return
            yield block[1]
            first = self.get(block[1], struct_name, "next")


def _view3d_sidebars(bf: BlendFile):
    area_index = bf.structs["ScrArea"][0]
    rb_off = bf.field("ScrArea", "regionbase")[0]
    pc_off = bf.field("ARegion", "panels_category_active")[0]
    for code, off, _, _, sdna in bf.blocks:
        if code != b"DATA" or sdna != area_index:
            continue
        if bf.get(off, "ScrArea", "spacetype") != SPACE_VIEW3D:
            continue
        first = struct.unpack_from("<Q", bf.data, off + rb_off)[0]
        for reg in bf.chain(first, "ARegion"):
            if bf.get(reg, "ARegion", "regiontype") == RGN_TYPE_UI:
                stack_first = struct.unpack_from("<Q", bf.data, reg + pc_off)[0]
                yield reg, list(bf.chain(stack_first, "PanelCategoryStack"))


def _idname(bf, pcs):
    at = bf.field("PanelCategoryStack", "idname")[0]
    raw = bytes(bf.data[pcs + at:pcs + at + 64])
    return raw.split(b"\0")[0].decode()


def read_sidebar(path: str) -> list:
    with open(path, "rb") as fh:
        bf = BlendFile(bytearray(fh.read()))
    return [{"sizex": bf.get(reg, "ARegion", "sizex"),
             "winx": bf.get(reg, "ARegion", "winx"),
             "categories": [_idname(bf, p) for p in stack]}
            for reg, stack in _view3d_sidebars(bf)]


def patch_sidebar(path: str, width: int, category: str) -> int:
    """Set every 3D Viewport sidebar's width and active tab; returns the count.

    The active-tab stack must already have an entry to overwrite. Blender adds
    one the first time it draws a visible sidebar, so save the file from a GUI
    session with the sidebar open.
    """
    with open(path, "rb") as fh:
        bf = BlendFile(bytearray(fh.read()))
    name = category.encode()
    if len(name) >= 64:
        raise ValueError("category name too long")
    at = bf.field("PanelCategoryStack", "idname")[0]
    patched = 0
    for reg, stack in _view3d_sidebars(bf):
        if not stack:
            raise RuntimeError("sidebar has no tab stack yet; save from a GUI session")
        bf.put(reg, "ARegion", "sizex", width)
        bf.put(reg, "ARegion", "winx", width)
        bf.data[stack[0] + at:stack[0] + at + 64] = name.ljust(64, b"\0")
        patched += 1
    if not patched:
        raise RuntimeError("no 3D Viewport sidebar found")
    with open(path, "wb") as fh:
        fh.write(bf.data)
    return patched


if __name__ == "__main__":
    import sys
    for row in read_sidebar(sys.argv[1]):
        print(row)
