# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=3.3.1,<4", "pyyaml>=6"]
# ///
"""Chat test battery: run chat prompts against providers/models and score them.

Drives the add-on's own Chat tab code inside a live GUI Blender (the
blender-docker instance by default), exactly as a user typing into the
panel would, then checks the scene and the transcript against each case's
declarative checks. Results go to <out>/results.json and <out>/report.md.

    uv run scripts/battery/run_battery.py                       # all cases, current backend
    uv run scripts/battery/run_battery.py --cases 'tag:scene'   # by tag
    uv run scripts/battery/run_battery.py --cases 'box-*,trap-*' --models gateway:qwen3,gateway:gemma4
    uv run scripts/battery/run_battery.py --models anthropic:claude-opus-5   # needs ANTHROPIC_API_KEY
    uv run scripts/battery/run_battery.py --list
    make battery ARGS="--cases tag:primitives --repeat 3"

Models are ``provider:model``; ``gateway:qwen3:32b`` keeps everything after
the first colon as the model. ``openai:<model>@<base_url>`` uses
OPENAI_API_KEY if set. ``current`` runs on whatever backend is configured.
The backend is switched through the add-on (blender_set_chat_backend is an
add-on-only tool) and restored at the end. API keys are passed through a
short-lived file in the bind-mounted projects directory, never through
the executed code or the command line.

Cases live in scripts/battery/cases/*.yaml (format: cases.py). Blend files
named by ``setup.blend`` are read from <compose-dir>/projects/battery/,
which the container sees as /home/blender/projects/battery/.

The token is the canary's: BLENDER_MCP_TOKEN or <compose-dir>/.env.mcp.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "canary"))

import run_canary as rc
import snippets
from cases import Case, CaseError, blend_files, load_cases
from checks import claimed_missing, judge_all, python_exprs, tool_records
from report import render_markdown, summarize

DEFAULT_UUID = "blender-801c42b0-5855-4a13-89dc-6bbc56cf680e"
CONTAINER_PROJECTS = "/home/blender/projects"
POLL_S = 3.0
IDLE_WAIT_S = 20.0  # longest wait for the add-on to drain before the after-snapshot
RESNAPSHOT_DELAY_S = 3.0
PROVIDERS = ("gateway", "anthropic", "openai")


class BlenderGone(Exception):
    """Blender restarted, or stopped answering, during a case."""


def log(msg: str) -> None:
    print(f"[{datetime.now().astimezone():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ models

def parse_model(spec: str) -> dict:
    spec = spec.strip()
    if spec == "current":
        return {"spec": spec, "provider": None, "model": "", "base_url": ""}
    provider, _, rest = spec.partition(":")
    if provider not in PROVIDERS:
        raise SystemExit(f"--models: unknown provider in {spec!r} (want {', '.join(PROVIDERS)})")
    model, base_url = rest, ""
    if provider == "openai":
        model, _, base_url = rest.partition("@")
        if not model or not base_url:
            raise SystemExit(f"--models: openai needs openai:<model>@<base_url>, got {spec!r}")
    return {"spec": spec, "provider": provider, "model": model, "base_url": base_url}


def key_for(provider: str | None) -> str | None:
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or None
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY") or None
    return None


# ------------------------------------------------------------------ runner

class Battery:
    def __init__(self, ctx: rc.Ctx, out_dir: Path, verbose: bool = False):
        self.ctx = ctx
        self.out = out_dir
        self.verbose = verbose
        self.results: list[dict] = []
        self.meta: dict[str, Any] = {}
        self.degraded = False

    async def code(self, snippet: str, timeout: float = 30) -> dict:
        try:
            return await rc.run_code(self.ctx, snippet, timeout=timeout)
        except Exception as e:
            raise BlenderGone(f"execute_code failed: {str(e)[:200]}") from e

    async def tool(self, name: str, args: dict, timeout: float = 90) -> Any:
        return await rc.call(self.ctx, name, {**args, "target_uuid": self.ctx.uuid}, timeout=timeout)

    # ---- identity / liveness ------------------------------------------------

    async def identity(self) -> tuple:
        """Changes when Blender restarts or the add-on is reloaded/updated.

        Not the bus client object: that is replaced on every reconnect,
        which a case survives unless it happens mid-turn (and then the turn
        itself reports the error).
        """
        try:
            st = await rc.addon_state(self.ctx, timeout=15)
            chat = await self.code(snippets.poll(10**9), timeout=15)
        except Exception as e:
            raise BlenderGone(f"identity check failed: {str(e)[:200]}") from e
        return (st.get("proc_start") and round(st["proc_start"]), st.get("version"), chat.get("state_id"))

    async def wait_ready(self, limit: float = 300, settle: int = 3) -> None:
        """Until the Blender answers and its chat reports available ``settle``
        times in a row, 5 s apart. A deploy often re-registers the client
        more than once (server restart, then container recreate), so one
        good answer is not enough."""
        log("waiting for the Blender to come back ...")
        deadline = time.monotonic() + limit
        good = 0
        while time.monotonic() < deadline:
            try:
                s = await self.code(snippets.poll(10**9), timeout=15)
                good = good + 1 if s.get("available") and not s.get("busy") else 0
            except BlenderGone:
                good = 0
            if good >= settle:
                log("Blender is back, chat available")
                return
            await asyncio.sleep(5 if good else 8)
        raise BlenderGone(f"Blender did not settle within {limit:.0f}s")

    # ---- backend ------------------------------------------------------------

    async def read_backend(self) -> dict | None:
        await self.code(snippets.BACKEND_REFRESH)
        for _ in range(20):
            await asyncio.sleep(1)
            s = await self.code(snippets.BACKEND_STATE)
            if s.get("backend") or s.get("error"):
                if s.get("error"):
                    raise RuntimeError(f"reading the chat backend failed: {s['error']}")
                return s["backend"]
        raise RuntimeError("the add-on never reported its chat backend")

    async def set_backend(self, provider: str, model: str, base_url: str = "",
                          key: str | None = None) -> dict:
        key_host = key_container = None
        try:
            if key:
                name = f".battery-key-{secrets.token_hex(8)}"
                key_host = self.ctx.compose_dir / "projects" / "battery" / name
                key_host.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(key_host, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key)
                key_container = f"{CONTAINER_PROJECTS}/battery/{name}"
            r = await self.code(snippets.set_backend(provider, model, base_url, key_container))
        finally:
            if key_host is not None and key_host.exists():
                key_host.unlink()
        if r.get("problem"):
            raise RuntimeError(f"set_backend refused: {r['problem']}")
        for _ in range(30):
            await asyncio.sleep(1)
            s = await self.code(snippets.BACKEND_STATE)
            if s.get("error"):
                raise RuntimeError(f"set_backend failed: {s['error']}")
            if s.get("backend"):
                b = s["backend"]
                # A saved "gateway" follows the server default, which the server
                # reports as whatever it actually is (e.g. anthropic, source=server).
                follows_default = provider == "gateway" and b.get("source") == "server"
                if not follows_default and (b.get("provider") != provider
                                            or (model and b.get("model") != model)):
                    raise RuntimeError(f"backend is {b}, wanted {provider}:{model}")
                return b
        raise RuntimeError("set_backend: no answer from the server")

    # ---- setup --------------------------------------------------------------

    async def setup(self, case: Case) -> None:
        s = case.setup
        base = s.get("base", "empty")
        if base == "empty":
            r = await self.tool("blender_new_file", {"empty": False, "discard_unsaved": True, "factory": True})
            _require_ok(r, "new_file")
            await self.code(snippets.remove_default_cube())
        elif base == "blend":
            path = f"{CONTAINER_PROJECTS}/battery/{s['blend']}"
            r = await self.tool("blender_open_file", {"path": path, "discard_unsaved": True},
                                timeout=180)
            _require_ok(r, "open_file")
        if s.get("scene"):
            r = await self.code(snippets.set_scene(s["scene"]))
            if not r.get("ok"):
                raise RuntimeError(f"setup scene: {r.get('error')}")
        if s.get("python"):
            await self.code(snippets.run_setup_python(s["python"]), timeout=60)
        try:
            # For whoever is watching; the run doesn't depend on it.
            shown = await self.code(snippets.show_chat())
            if not shown.get("reader"):
                log(f"   (Chat tab for watchers: {shown})")
        except Exception as e:  # noqa: BLE001
            log(f"   (couldn't open the Chat tab for watchers: {e})")

    # ---- one turn -----------------------------------------------------------

    async def turn(self, text: str, case: Case, state_id: int, rec: dict) -> bool:
        """Send one message and wait for the reply. False when it didn't finish."""
        r = await self.code(snippets.send(text))
        if not r.get("ok"):
            problem = r.get("problem") or "not sent"
            if "checking whether" in problem or "Not connected" in problem:
                raise BlenderGone(f"send refused: {problem}")
            rec["errors"].append(f"send: {problem}")
            return False
        t0 = time.monotonic()
        answered = set()
        while True:
            await asyncio.sleep(POLL_S)
            s = await self.code(snippets.poll(r["turn"] - 1))
            if s.get("state_id") != state_id:
                raise BlenderGone("chat state was reset (add-on reloaded)")
            prompt = s.get("approval")
            if prompt and prompt not in answered:
                answered.add(prompt)
                allow = case.approval == "allow"
                answer = "allow" if allow else "deny"
                rec["approvals"].append({"prompt": prompt[:600], "answer": answer,
                                         "policy": case.approval})
                if case.approval == "never":
                    rec["approval_violation"] = True
                log(f"   approval requested ({case.approval}) -> {answer}")
                await self.code(snippets.resolve(allow))
                continue
            if not s.get("busy"):
                rec["seconds"] = round(rec.get("seconds", 0) + time.monotonic() - t0, 1)
                rec["backend_used"] = s.get("backend_used")
                rec["_msgs"].extend(m for m in s.get("messages") or [] if m.get("turn") == r["turn"])
                rec["_last_turn"] = r["turn"]
                return True
            if time.monotonic() - t0 > case.timeout:
                log(f"   turn timed out after {case.timeout:.0f}s, stopping it")
                await self.code(snippets.STOP)
                rec["timed_out"] = True
                rec["seconds"] = round(rec.get("seconds", 0) + time.monotonic() - t0, 1)
                s = await self.code(snippets.poll(r["turn"] - 1))
                rec["_msgs"].extend(m for m in s.get("messages") or [] if m.get("turn") == r["turn"])
                rec["_last_turn"] = r["turn"]
                return False

    # ---- one case -----------------------------------------------------------

    async def attempt(self, case: Case, model: dict, repeat: int) -> dict:
        rec: dict[str, Any] = {"model": model["spec"], "case": case.id, "title": case.title,
                               "repeat": repeat, "ok": False, "tools": [], "approvals": [],
                               "errors": [], "reply": "", "checks": [], "timed_out": False,
                               "_msgs": []}
        ident = await self.identity()
        await self.setup(case)
        c = await self.code(snippets.CLEAR)
        state_id = c["state_id"]
        before = await self.code(snippets.snapshot(), timeout=60)
        for text in case.prompts:
            if not await self.turn(text, case, state_id, rec):
                break
        msgs = rec.pop("_msgs")
        last = rec.pop("_last_turn", None)
        rec["tools"] = tool_records(msgs)
        rec["reply"] = "\n".join(m.get("text") or "" for m in msgs
                                 if m.get("role") == "assistant" and m.get("turn") == last).strip()

        await self.wait_idle()
        exprs = python_exprs(case.checks)
        after = await self.code(snippets.snapshot(exprs), timeout=60)
        missing = claimed_missing(rec["reply"], case.checks, rec["tools"], after)
        if missing:
            # Seen once: an object reported created was absent from the
            # snapshot and present on a rerun. Retake it once and say so.
            log(f"   snapshot lacks {missing}; retaking it")
            await asyncio.sleep(RESNAPSHOT_DELAY_S)
            await self.wait_idle()
            after = await self.code(snippets.snapshot(exprs), timeout=60)
            still = claimed_missing(rec["reply"], case.checks, rec["tools"], after)
            rec["resnapshot"] = {"missing_first": missing, "missing_after_retry": still}
        if await self.identity() != ident:
            raise BlenderGone("Blender restarted during the case")

        rec["errors"] += [m.get("text") for m in msgs if m.get("role") == "error"]
        rec["statuses"] = [m.get("text") for m in msgs if m.get("role") == "status"]
        turn_data = {"reply": rec["reply"], "tools": rec["tools"], "errors": rec["errors"]}
        rec["checks"] = judge_all(case.checks, after, before, turn_data)
        if case.approval == "never":
            rec["checks"].append({"check": "approval_policy", "ok": not rec.get("approval_violation"),
                                  "observed": f"{len(rec['approvals'])} approval request(s)",
                                  "expect": "no approval requests"})
        if rec["timed_out"]:
            rec["checks"].append({"check": "finished", "ok": False, "observed": "timed out",
                                  "expect": f"reply within {case.timeout:.0f}s"})
        rec["ok"] = all(ch["ok"] for ch in rec["checks"])
        rec["scene_after"] = {"objects": len(after.get("objects") or []),
                              "annotations": after.get("annotations")}
        return rec

    async def wait_idle(self, limit: float = IDLE_WAIT_S) -> bool:
        """Until no chat turn runs and no dispatch is queued in the add-on.
        False when it didn't get there in ``limit`` seconds (carries on)."""
        deadline = time.monotonic() + limit
        while True:
            s = await self.code(snippets.IDLE, timeout=15)
            if not s.get("busy") and not s.get("queued"):
                return True
            if time.monotonic() > deadline:
                log(f"   add-on still busy after {limit:.0f}s: {s}")
                return False
            await asyncio.sleep(0.5)

    async def recover(self, model: dict | None) -> bool:
        """Wait for the Blender to settle and re-apply the backend. Never raises."""
        try:
            await self.wait_ready()
            if model and model.get("provider"):
                # Settings live on the server and survive a restart; check anyway.
                await self.ensure_backend(model)
            self.degraded = False
            return True
        except Exception as e:  # noqa: BLE001
            log(f"   recovery failed: {type(e).__name__}: {str(e)[:200]}")
            self.degraded = True
            return False

    async def run_case(self, case: Case, model: dict, repeat: int) -> dict:
        if self.degraded and not await self.recover(model):
            return self._broken(case, model, repeat, "Blender unavailable (earlier recovery failed)")
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                rec = await self.attempt(case, model, repeat)
                if attempt > 1:
                    rec["note"] = "retried once after a Blender restart"
                return rec
            except BlenderGone as e:
                log(f"   {e}")
                if not await self.recover(model) or attempt == 2:
                    return self._broken(case, model, repeat, f"Blender went away: {e}")
            except Exception as e:  # noqa: BLE001 - one broken case must not end the run
                return self._broken(case, model, repeat, f"{type(e).__name__}: {e}",
                                    seconds=time.monotonic() - t0)
        raise AssertionError("unreachable")

    def _broken(self, case: Case, model: dict, repeat: int, note: str, seconds: float | None = None) -> dict:
        return {"model": model["spec"], "case": case.id, "title": case.title, "repeat": repeat,
                "ok": False, "tools": [], "approvals": [], "errors": [note], "reply": "",
                "checks": [{"check": "runner", "ok": False, "observed": note, "expect": "case ran"}],
                "timed_out": False, "note": note,
                "seconds": None if seconds is None else round(seconds, 1)}

    async def ensure_backend(self, model: dict) -> dict:
        b = await self.set_backend(model["provider"], model["model"], model["base_url"],
                                   key_for(model["provider"]))
        log(f"backend -> {b.get('provider')}:{b.get('model')}")
        return b

    # ---- output -------------------------------------------------------------

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "results.json").write_text(json.dumps(
            {"meta": self.meta, "summary": summarize(self.results), "results": self.results},
            indent=1, default=str))
        (self.out / "report.md").write_text(render_markdown(self.results, self.meta))


def _require_ok(r: Any, what: str) -> None:
    if isinstance(r, dict):
        if r.get("error") or r.get("status") in ("error", "failed", "refused"):
            raise RuntimeError(f"{what}: {json.dumps(r)[:300]}")
        return
    if isinstance(r, str) and ("error" in r.lower()[:80]):
        raise RuntimeError(f"{what}: {r[:300]}")


async def restore_backend(bat: Battery, original: dict, tries: int = 3) -> str:
    """Put the account's backend back. Retries through a Blender restart,
    since the switch has to go through the add-on."""
    if (original.get("provider") != "gateway" and original.get("has_key")
            and original.get("source") != "server"):
        return "not restored: the saved key can't be put back; set it again"
    last = ""
    for i in range(tries):
        try:
            if original.get("source") == "server":
                # It was following the server default: saved "gateway" does that again.
                b = await bat.set_backend("gateway", "", "")
            else:
                b = await bat.set_backend(original["provider"], original.get("model") or "",
                                          original.get("base_url") or "")
            return f"{b.get('provider')}:{b.get('model')}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"   restore attempt {i + 1} failed: {last}")
            await bat.recover(None)
    return f"restore failed: {last}"


# ------------------------------------------------------------------ main

async def amain(args) -> int:
    try:
        cases = load_cases(Path(args.case_dir), args.cases)
    except CaseError as e:
        print(f"case error: {e}", file=sys.stderr)
        return 2
    if args.list:
        for c in cases:
            print(f"{c.id:32} {c.approval:6} {','.join(c.tags):28} {c.title}")
        return 0
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    compose_dir = Path(args.compose_dir).expanduser().resolve()
    missing = [b for b in blend_files(cases) if not (compose_dir / "projects" / "battery" / b).exists()]
    models = [parse_model(m) for m in args.models.split(",") if m.strip()]
    out = Path(args.out) if args.out else Path("artifacts/battery") / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    ctx = rc.Ctx(compose_dir, args.service, rc.load_token(compose_dir), out)
    ctx.uuid = args.client
    bat = Battery(ctx, out, args.verbose)
    bat.meta = {"started": rc.now_utc(), "mcp_url": rc.MCP_URL, "client": ctx.uuid,
                "cases": [c.id for c in cases], "models": [m["spec"] for m in models],
                "repeat": args.repeat}
    log(f"{len(cases)} case(s) x {len(models)} model(s) x {args.repeat}; results in {out}")

    original = await bat.read_backend()
    log(f"current backend: {original}")
    bat.meta["original_backend"] = original
    switching = any(m["provider"] for m in models)
    # A key on the server's default backend isn't the account's; switching can't lose it.
    own_key = bool(original and original.get("has_key") and original.get("source") != "server")
    if switching and own_key and not args.allow_key_loss:
        others = {m["provider"] for m in models if m["provider"]} - {original.get("provider")}
        if others:
            print("The account has a saved API key for its current backend and switching provider "
                  "would drop it. Pass --allow-key-loss to proceed.", file=sys.stderr)
            return 2

    try:
        for model in models:
            if model["provider"] and not key_for(model["provider"]) and model["provider"] == "anthropic":
                log(f"skipping {model['spec']}: ANTHROPIC_API_KEY is not set")
                for c in cases:
                    bat.results.append({"model": model["spec"], "case": c.id, "repeat": 1,
                                        "skipped": True, "ok": False, "note": "no ANTHROPIC_API_KEY"})
                continue
            if model["provider"]:
                try:
                    await bat.ensure_backend(model)
                except Exception as e:  # noqa: BLE001
                    log(f"skipping {model['spec']}: {e}")
                    for c in cases:
                        bat.results.append({"model": model["spec"], "case": c.id, "repeat": 1,
                                            "skipped": True, "ok": False, "note": str(e)[:300]})
                    continue
            for repeat in range(1, args.repeat + 1):
                for case in cases:
                    if case.setup.get("blend") in missing:
                        bat.results.append({"model": model["spec"], "case": case.id, "repeat": repeat,
                                            "skipped": True, "ok": False,
                                            "note": f"missing projects/battery/{case.setup['blend']}"})
                        continue
                    log(f"{model['spec']} | {case.id} (repeat {repeat})")
                    rec = await bat.run_case(case, model, repeat)
                    bat.results.append(rec)
                    failed = [c["check"] for c in rec["checks"] if not c["ok"]]
                    tools = ",".join(t["name"] for t in rec.get("tools") or [])
                    log(f"   {'PASS' if rec['ok'] else 'FAIL'} {rec.get('seconds')}s tools=[{tools}]"
                        + (f" failed={failed}" if failed else ""))
                    if args.verbose and rec.get("reply"):
                        log(f"   reply: {rec['reply'][:300]!r}")
                    bat.save()
    finally:
        if switching and original and not args.keep_backend:
            bat.meta["restored"] = await restore_backend(bat, original)
            log(f"backend restore: {bat.meta['restored']}")
            if bat.meta["restored"].startswith("restore failed"):
                print(f"WARNING: the account's chat backend is NOT back to {original}. "
                      "Set it again from the add-on preferences.", file=sys.stderr)
        bat.meta["finished"] = rc.now_utc()
        bat.save()

    print()
    print(render_markdown(bat.results, bat.meta).split("## Failures")[0])
    log(f"wrote {out / 'report.md'} and {out / 'results.json'}")
    return 0 if all(r.get("ok") or r.get("skipped") for r in bat.results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", help="comma list of id/file globs or tag:<name> (default: all)")
    ap.add_argument("--models", default="current",
                    help="comma list: gateway:<model>, anthropic:<model>, openai:<model>@<url>, current")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", help="output dir (default artifacts/battery/<timestamp>)")
    ap.add_argument("--list", action="store_true", help="list the selected cases and exit")
    ap.add_argument("--case-dir", default=str(HERE / "cases"))
    ap.add_argument("--compose-dir", default=os.environ.get("CANARY_COMPOSE_DIR", "~/claude/blender-docker"))
    ap.add_argument("--service", default=os.environ.get("CANARY_SERVICE", "blender-desktop"))
    ap.add_argument("--client", default=os.environ.get("BATTERY_CLIENT_UUID", DEFAULT_UUID),
                    help="target Blender's client uuid")
    ap.add_argument("--keep-backend", action="store_true", help="don't restore the original backend")
    ap.add_argument("--allow-key-loss", action="store_true",
                    help="switch provider even though that drops the account's saved API key")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
