"""In-Blender chat: the add-on half of blender_chat.

    state.py        messages, busy flag, pending approval; no bpy
    client.py       send / stop / backend tools over the bus client's loop
    elicitation.py  "BlenderMCP approval:" elicitations -> Allow / Deny
    log.py          the "BlenderMCP Chat" text block

The conversation itself runs on the server; see
docs-site/src/content/docs/how-to/chat-in-blender.mdx for the contract.
"""
