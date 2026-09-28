"""annotation_overlay: projecting and painting strokes onto a capture."""

import numpy as np

from addon import annotation_overlay as ov

# Looking down -z from the origin with a 90 degree frustum: x, y map to
# NDC as x/-z, y/-z, and w = -z (row-major, like region_3d.perspective_matrix).
PERSP = [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, -1, -0.2],
    [0, 0, -1, 0],
]
SIZE = (200, 100)


def test_points_in_front_project_to_pixels():
    (line,) = ov.project_polyline([(0, 0, -1), (0.5, 0.5, -1)], PERSP, SIZE)
    assert line[0] == (100.0, 50.0)
    assert line[1] == (150.0, 75.0)


def test_stroke_through_the_eye_is_cut_not_wrapped():
    pts = [(-1, 0, -1), (0, 0, 1), (1, 0, -1)]  # middle point is behind the eye
    lines = ov.project_polyline(pts, PERSP, SIZE)
    assert len(lines) == 2
    # Each piece stays on its own side of the screen instead of crossing it.
    assert all(x <= 100 for x, _ in lines[0])
    assert all(x >= 100 for x, _ in lines[1])


def test_fully_behind_is_dropped():
    assert ov.project_polyline([(0, 0, 1), (1, 0, 2)], PERSP, SIZE) == []


def test_view_polyline_uses_region_percentages():
    assert ov.view_polyline([(0, 0, 0), (50, 100, 0)], SIZE) == [[(0.0, 0.0), (100.0, 100.0)]]


def test_paint_draws_the_colour_with_the_given_thickness():
    arr = np.zeros((100, 200, 4), dtype=np.float32)
    hit = ov.paint_polylines(arr, [[(20, 50), (180, 50)]], (0, 1, 0), thickness=6)
    assert hit > 0
    column = arr[:, 100, 1]
    assert 5 <= int((column > 0.99).sum()) <= 7  # ~6 px tall
    assert arr[50, 100, 3] == 1.0
    assert arr[10, 100, 1] == 0.0


def test_paint_ignores_segments_far_off_image_without_blowing_up():
    arr = np.zeros((100, 200, 4), dtype=np.float32)
    assert ov.paint_polylines(arr, [[(1e9, 1e9), (2e9, 2e9)]], (1, 0, 0), 4) == 0
    # A segment from far away into the image only paints the visible part.
    assert ov.paint_polylines(arr, [[(-1e9, 50), (100, 50)]], (1, 0, 0), 4) > 0
    assert arr[50, 50, 0] == 1.0


def test_opacity_blends():
    arr = np.ones((20, 20, 4), dtype=np.float32)
    ov.paint_polylines(arr, [[(0, 10), (19, 10)]], (0, 0, 0), 2, opacity=0.25)
    assert np.isclose(arr[10, 10, 0], 0.75)
