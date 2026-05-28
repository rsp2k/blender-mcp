"""Single source of truth for the addon version.

`addon/__init__.py` imports `tuple_version` for `bl_info["version"]`.
A simple grep test in CI (or a `make check-version` target) can confirm
this string matches the rest of the project.
"""

__version__ = "1.5.1"
tuple_version = tuple(int(p) for p in __version__.split("."))
