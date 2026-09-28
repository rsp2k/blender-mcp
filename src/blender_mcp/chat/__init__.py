"""In-Blender chat: the add-on's Chat tab talks to a model that drives the
same Blender through the server's tools.

The add-on calls ``blender_chat`` (tools.py). The turn (turn.py) samples the
model with ``ctx.sample_step``; the server's sampling handler (routing.py)
sends each step to the user's backend (providers/). Tool calls run through the
server's own tools in-process (executor.py), aimed at the calling Blender.
"""
