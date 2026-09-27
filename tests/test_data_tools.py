"""Mesh-from-data geometry, upload file handling, polygon ops, upload helpers."""

import base64
import hashlib
import os
import time

import pytest
import shapely
from shapely.geometry import Polygon

from addon import prism_geometry as pg
from addon import upload_store as us
from blender_mcp import data_tools as dt
from blender_mcp import geometry_ops as go

SQUARE = [[0, 0], [4, 0], [4, 4], [0, 4]]
HOLE = [[1, 1], [3, 1], [3, 3], [1, 3]]


def shapely_tessellate(polys):
    """Stand-in for mathutils.geometry.tessellate_polygon: triangle indices
    into the concatenated rings."""
    rings = [[(x, y) for x, y, _z in r] for r in polys]
    flat = [p for r in rings for p in r]
    index = {p: i for i, p in enumerate(flat)}
    tris = shapely.constrained_delaunay_triangles(Polygon(rings[0], rings[1:]))
    return [tuple(index[c] for c in t.exterior.coords[:3]) for t in tris.geoms]


def no_tessellate(_polys):
    raise AssertionError("caps without holes must not need tessellation")


def is_closed(faces):
    return all(c == 2 for c in pg.edge_face_counts(faces).values())


def signed_volume(verts, faces):
    v = 0.0
    for f in faces:
        a = verts[f[0]]
        for i in range(1, len(f) - 1):
            b, c = verts[f[i]], verts[f[i + 1]]
            v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return v / 6.0


# ---- prism geometry ---------------------------------------------------------

def test_square_prism_is_closed_with_outward_normals():
    verts, faces = pg.build_prism(pg.clean_ring(SQUARE), [], 0.0, 2.0, no_tessellate)
    assert len(verts) == 8 and len(faces) == 6
    assert is_closed(faces)
    assert signed_volume(verts, faces) == pytest.approx(32.0)


def test_clockwise_input_is_reoriented():
    verts, faces = pg.build_prism(pg.clean_ring(SQUARE[::-1]), [], 1.0, 3.0, no_tessellate)
    assert signed_volume(verts, faces) == pytest.approx(32.0)
    assert min(v[2] for v in verts) == 1.0


def test_concave_ring_single_ngon_caps():
    ell = pg.clean_ring([[0, 0], [6, 0], [6, 2], [2, 2], [2, 6], [0, 6]])
    verts, faces = pg.build_prism(ell, [], 0.0, 2.5, no_tessellate)
    assert len(faces) == 8 and is_closed(faces)
    assert signed_volume(verts, faces) == pytest.approx(50.0)


@pytest.mark.parametrize("hole", [HOLE, HOLE[::-1]])
def test_prism_with_hole_is_closed(hole):
    verts, faces = pg.build_prism(pg.clean_ring(SQUARE), [pg.clean_ring(hole)], 0.0, 3.0,
                                  shapely_tessellate)
    assert len(verts) == 16
    assert is_closed(faces)
    assert signed_volume(verts, faces) == pytest.approx(36.0)


def test_zero_height_rejected():
    with pytest.raises(ValueError, match="positive"):
        pg.build_prism(pg.clean_ring(SQUARE), [], 1.0, 1.0, no_tessellate)


def test_clean_ring_drops_closing_and_duplicate_points():
    ring = pg.clean_ring([[0, 0], [0, 0], [1, 0], [1, 1], [0, 0]])
    assert ring == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]


@pytest.mark.parametrize("bad, msg", [
    ([[0, 0], [1, 1]], "at least 3"),
    ([[0, 0], [1, 1], [2, 2]], "zero area"),
    ([[0, 0], [1, "x"], [2, 2]], "non-numeric"),
    ("nope", "list"),
])
def test_clean_ring_rejects_bad_input(bad, msg):
    with pytest.raises(ValueError, match=msg):
        pg.clean_ring(bad)


def test_parse_polygon_spec_forms():
    bare = pg.parse_polygon_spec(SQUARE, 0)
    assert bare["holes"] == [] and "height" not in bare
    full = pg.parse_polygon_spec({"outer": SQUARE, "holes": [HOLE], "height": "2.5", "name": "w"}, 3)
    assert full["height"] == 2.5 and full["name"] == "w" and len(full["holes"]) == 1
    with pytest.raises(ValueError, match="polygon 7: missing 'outer'"):
        pg.parse_polygon_spec({"holes": []}, 7)


def test_transform_ring_origin_then_scale():
    assert pg.transform_ring([(10.0, 20.0)], (10.0, 10.0), 0.5) == [(0.0, 5.0)]


# ---- upload store -----------------------------------------------------------

UID = "upload-0001"


def test_chunked_upload_out_of_order_with_retry(tmp_path):
    data = os.urandom(10_000)
    us.begin(tmp_path, UID)
    chunks = [(o, data[o:o + 3000]) for o in range(0, len(data), 3000)]
    for off, c in reversed(chunks):
        us.write_chunk(tmp_path, UID, off, c)
    us.write_chunk(tmp_path, UID, *chunks[1])  # a retried chunk is harmless
    sha = hashlib.sha256(data).hexdigest()
    out = us.finish(tmp_path, UID, "walls.json", size=len(data), sha256="sha256:" + sha)
    assert out["size"] == len(data) and out["sha256"] == sha
    assert (tmp_path / "walls.json").read_bytes() == data
    assert not list(tmp_path.glob(".*.part"))


def test_finish_rejects_size_and_hash_mismatch(tmp_path):
    us.begin(tmp_path, UID)
    us.write_chunk(tmp_path, UID, 0, b"hello")
    with pytest.raises(ValueError, match="size mismatch"):
        us.finish(tmp_path, UID, "x.txt", size=6)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        us.finish(tmp_path, UID, "x.txt", sha256="0" * 64)
    assert us.abort(tmp_path, UID) is True


def test_resume_reports_bytes_written(tmp_path):
    us.begin(tmp_path, UID)
    us.write_chunk(tmp_path, UID, 0, b"abcdef")
    assert us.begin(tmp_path, UID, resume=True) == {"upload_id": UID, "resumed": True, "size": 6}
    assert us.begin(tmp_path, UID)["size"] == 0  # a fresh begin truncates


def test_overwrite_false_keeps_existing(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"old")
    us.begin(tmp_path, UID)
    us.write_chunk(tmp_path, UID, 0, b"new")
    with pytest.raises(ValueError, match="already exists"):
        us.finish(tmp_path, UID, "a.bin", overwrite=False)
    assert (tmp_path / "a.bin").read_bytes() == b"old"


@pytest.mark.parametrize("name, expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\win.ini", "win.ini"),
    ("/abs/path/plan v2.json", "plan_v2.json"),
    (".hidden", "hidden"),
])
def test_names_are_sanitised_into_the_dir(tmp_path, name, expected):
    us.begin(tmp_path, UID)
    out = us.finish(tmp_path, UID, name)
    assert out["name"] == expected
    assert (tmp_path / expected).exists()


@pytest.mark.parametrize("bad", ["..", "...", "/", "", None])
def test_unusable_names_rejected(bad):
    with pytest.raises(ValueError):
        us.sanitize_name(bad)


@pytest.mark.parametrize("bad", ["short", "../../../x", "a" * 65, "has space1", None])
def test_bad_upload_ids_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="upload_id"):
        us.begin(tmp_path, bad)


def test_chunk_needs_begin_and_valid_offset(tmp_path):
    with pytest.raises(ValueError, match="call begin first"):
        us.write_chunk(tmp_path, UID, 0, b"x")
    us.begin(tmp_path, UID)
    with pytest.raises(ValueError, match="offset"):
        us.write_chunk(tmp_path, UID, -1, b"x")


def test_list_and_delete(tmp_path):
    (tmp_path / "a.json").write_bytes(b"12")
    (tmp_path / "b.json").write_bytes(b"345")
    us.begin(tmp_path, UID)
    listing = us.list_files(tmp_path)
    assert [f["name"] for f in listing["files"]] == ["a.json", "b.json"]
    assert listing["total_bytes"] == 5
    assert us.delete(tmp_path, "../a.json") == {"deleted": ["a.json"]}
    with pytest.raises(ValueError, match="no uploaded file"):
        us.delete(tmp_path, "a.json")
    assert us.delete(tmp_path)["deleted"] == [f".{UID}.part", "b.json"]
    assert us.list_files(tmp_path / "missing")["files"] == []


def test_stale_parts_are_cleaned(tmp_path):
    us.begin(tmp_path, UID)
    old = time.time() - 2 * us.STALE_PART_S
    os.utime(us.part_path(tmp_path, UID), (old, old))
    assert us.clean_stale_parts(tmp_path) == 1


def test_resolve_source(tmp_path):
    (tmp_path / "p.json").write_text("[]")
    assert us.resolve_source(tmp_path, "p.json") == (tmp_path / "p.json").resolve()
    assert us.resolve_source(tmp_path, "../../p.json") == (tmp_path / "p.json").resolve()
    assert us.resolve_source(tmp_path, str(tmp_path / "p.json")) == tmp_path / "p.json"
    with pytest.raises(ValueError, match="not found"):
        us.resolve_source(tmp_path, "nope.json")


# ---- polygon ops (server-side shapely) ---------------------------------------

def test_union_of_touching_squares():
    out = go.run("union", [[[0, 0], [2, 0], [2, 2], [0, 2]], [[2, 0], [4, 0], [4, 2], [2, 2]]])
    assert out["polygon_count"] == 1 and out["area"] == pytest.approx(8.0)
    ring = out["polygons"][0]["outer"]
    assert pg.signed_area(ring) > 0 and ring[0] != ring[-1]


def test_closing_bridges_a_small_gap():
    walls = [[[0, 0], [2, 0], [2, 1], [0, 1]], [[2.1, 0], [4, 0], [4, 1], [2.1, 1]]]
    assert go.run("union", walls)["polygon_count"] == 2
    closed = go.run("closing", walls, distance=0.1)
    assert closed["polygon_count"] == 1
    assert closed["area"] == pytest.approx(4.0, abs=1e-6)


def test_difference_makes_a_hole_that_extrudes_cleanly():
    out = go.run("difference", [SQUARE], other=[HOLE])
    assert out["area"] == pytest.approx(12.0) and out["hole_count"] == 1
    p = out["polygons"][0]
    assert pg.signed_area(p["holes"][0]) < 0
    verts, faces = pg.build_prism(pg.clean_ring(p["outer"]), [pg.clean_ring(p["holes"][0])],
                                  0.0, 1.0, shapely_tessellate)
    assert is_closed(faces) and signed_volume(verts, faces) == pytest.approx(12.0)


def test_invalid_input_is_repaired_and_flagged():
    bowtie = [[0, 0], [2, 2], [2, 0], [0, 2]]
    out = go.run("validate", [bowtie])
    assert out["input_valid"] is False and "note" in out
    assert out["area"] == pytest.approx(2.0)


def test_area_omits_polygons():
    out = go.run("area", [SQUARE])
    assert out["area"] == 16.0 and "polygons" not in out


@pytest.mark.parametrize("kwargs, msg", [
    ({"operation": "explode"}, "operation must be"),
    ({"operation": "closing"}, "positive distance"),
    ({"operation": "simplify"}, "positive tolerance"),
    ({"operation": "difference"}, "needs 'other'"),
    ({"operation": "buffer", "distance": 1, "join_style": "zigzag"}, "join_style"),
])
def test_op_argument_errors(kwargs, msg):
    op = kwargs.pop("operation")
    with pytest.raises(ValueError, match=msg):
        go.run(op, [SQUARE], **kwargs)


# ---- server-side upload helpers ----------------------------------------------

def test_plan_chunks_covers_every_byte():
    assert dt.plan_chunks(0) == [(0, 0)]
    assert dt.plan_chunks(10, 4) == [(0, 4), (4, 4), (8, 2)]
    plan = dt.plan_chunks(dt.CHUNK_BYTES * 3 + 1)
    assert len(plan) == 4 and sum(n for _o, n in plan) == dt.CHUNK_BYTES * 3 + 1


def test_payload_bytes_exactly_one_source():
    assert dt.payload_bytes(base64.b64encode(b"\x00\x01").decode(), None, None) == b"\x00\x01"
    assert dt.payload_bytes(None, "héllo", None) == "héllo".encode()
    assert dt.payload_bytes(None, None, {"a": [1, 2]}) == b'{"a":[1,2]}'
    with pytest.raises(ValueError, match="exactly one"):
        dt.payload_bytes(None, "a", {"b": 1})
    with pytest.raises(ValueError, match="exactly one"):
        dt.payload_bytes(None, None, None)
    with pytest.raises(ValueError, match="not valid base64"):
        dt.payload_bytes("!!!", None, None)


def test_upload_max_bytes_env(monkeypatch):
    monkeypatch.delenv("BLENDER_MCP_UPLOAD_MAX_BYTES", raising=False)
    assert dt.upload_max_bytes() == 50 * 1024 * 1024
    monkeypatch.setenv("BLENDER_MCP_UPLOAD_MAX_BYTES", "1234")
    assert dt.upload_max_bytes() == 1234
    monkeypatch.setenv("BLENDER_MCP_UPLOAD_MAX_BYTES", "lots")
    assert dt.upload_max_bytes() == 50 * 1024 * 1024
