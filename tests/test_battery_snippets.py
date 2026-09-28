"""Chat battery: the Blender-side snippets compile and embed values safely."""

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "battery"))

import snippets

TRICKY = 'He said "hi"\n\\n \'quoted\' """triple""" {braces} é'


def test_every_snippet_compiles():
    for code in (snippets.snapshot({"0": "1"}), snippets.remove_default_cube(), snippets.set_scene(TRICKY),
                 snippets.run_setup_python("x = 1"), snippets.poll(3), snippets.send(TRICKY),
                 snippets.resolve(True), snippets.STOP, snippets.CLEAR, snippets.BACKEND_REFRESH,
                 snippets.BACKEND_STATE, snippets.set_backend("gateway", "qwen3"),
                 snippets.set_backend("anthropic", "m", key_path="/x/.battery-key-1")):
        compile(code, "snippet", "exec")


def test_literal_round_trips_any_text():
    assert eval(snippets._lit(TRICKY), {"json": json}) == TRICKY
    assert eval(snippets._lit({"0": TRICKY}), {"json": json}) == {"0": TRICKY}


def test_key_never_in_snippet_text():
    code = snippets.set_backend("anthropic", "claude-sonnet-5", key_path="/p/.battery-key-ab")
    assert "/p/.battery-key-ab" in code
    assert "os.remove(_kp)" in code


def test_block_scalar_expressions_evaluate_once_wrapped():
    # The same wrapping SNAPSHOT_BODY applies before eval().
    expr = yaml.safe_load("e: >-\n  (lambda xs: len(xs)\n    == 2)\n  ([1, 2])\n")["e"]
    assert "\n" in expr
    assert eval("(\n" + expr + "\n)") is True
    assert '"(\\n" + expr + "\\n)"' in snippets.SNAPSHOT_BODY
