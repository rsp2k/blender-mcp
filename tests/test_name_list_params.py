"""A name list sent as a JSON string is decoded before dispatch."""

from blender_mcp.dispatch_component import unwrap_name_lists


def test_json_encoded_list_becomes_a_list():
    assert unwrap_name_lists({"objects": '["Tower 4"]'}) == {"objects": ["Tower 4"]}
    assert unwrap_name_lists({"operands": '["A", "B"]', "x": 1}) == {"operands": ["A", "B"], "x": 1}


def test_plain_values_are_untouched():
    params = {"objects": "Cube", "frame": None}
    assert unwrap_name_lists(params) is params
    assert unwrap_name_lists({"objects": "[not json"}) == {"objects": "[not json"}
    assert unwrap_name_lists({"objects": "[1, 2]"}) == {"objects": "[1, 2]"}
    assert unwrap_name_lists({"code": '["x"]'}) == {"code": '["x"]'}
