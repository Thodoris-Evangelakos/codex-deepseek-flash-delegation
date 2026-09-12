#!/usr/bin/env python3
"""Compact runnable regression check for model-picker.py.

    python3 test_model_picker.py [--native]

One self-contained check (stdlib + installed aiohttp only) drives the bridge
against an in-process fake app-server and a real loopback aiohttp endpoint.
Covered: model->provider routing, switch verification failure/rollback,
RPC id isolation and forwarding, profile persistence target, bearer-token
enforcement, catalog merge/profile creation and history/permission
preservation across a provider switch.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "model_picker", os.path.join(HERE, "model-picker.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mp = load_adapter()
# Unit and native protocol checks never start a model turn.
os.environ.setdefault("DEEPSEEK_API_KEY", "synthetic-no-model-requests")

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class FakeAppServer:
    """Subset of the native app-server protocol with the switch gate modelled."""

    def __init__(self):
        self.threads = {}
        self.seen = []  # (method, params) the bridge forwarded
        self.writes = []  # config writes
        self.to_bridge = None
        self._seq = 0

    # bridge -> fake server
    async def send(self, message):
        method = message.get("method")
        if method is None:
            return  # client response to a server request
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        self.seen.append((method, params))
        if "id" not in message:
            return
        response = self.dispatch(method, params)
        if method == "thread/start" and "result" in response:
            await self.emit("thread/started", {
                "thread": {"id": response["result"]["thread"]["id"]}})
        await self.to_bridge({"jsonrpc": "2.0", "id": message["id"], **response})

    async def emit(self, method, params):
        await self.to_bridge({"jsonrpc": "2.0", "method": method, "params": params})

    # server-side semantics
    def dispatch(self, method, params):
        if method == "initialize":
            return {"result": {"serverInfo": {"name": "fake"}}}
        if method == "thread/start":
            self._seq += 1
            tid = f"srv-{self._seq}"
            self.threads[tid] = {
                "id": tid,
                "model": params.get("model"),
                "provider": params.get("modelProvider"),
                "rolled_out": False,
                "busy": False,
                "subscribers": 1,
                "extra_subscribers": 0,
                "messages": 0,
            }
            return {"result": self.thread_response(tid)}
        if method == "thread/read":
            self.threads[params["threadId"]]["rolled_out"] = True
            return {"error": {"code": -32601, "message": "list_turns is not supported yet"}}
        if method == "thread/resume":
            tid = params.get("threadId")
            state = self.threads.get(tid)
            if state is None:
                return {"error": {"code": -32600, "message": f"no such thread {tid}"}}
            wants = (params.get("modelProvider") or state["provider"])
            if wants != state["provider"]:
                if state["subscribers"] > 0 or state["extra_subscribers"] > 0 or state["busy"]:
                    state["subscribers"] += 1  # overrides silently ignored
                    return {"result": self.thread_response(tid)}
                if not state["rolled_out"]:
                    return {"error": {"code": -32600, "message": "thread has no rollout"}}
                state["model"] = params.get("model")
                state["provider"] = wants
            state["subscribers"] += 1
            return {"result": self.thread_response(tid)}
        if method == "thread/unsubscribe":
            state = self.threads[params.get("threadId")]
            state["subscribers"] = max(0, state["subscribers"] - 1)
            return {"result": {"status": "ok"}}
        if method == "thread/settings/update":
            return {"result": {}}
        if method == "turn/start":
            state = self.threads[params.get("threadId")]
            state["rolled_out"] = True
            state["messages"] += 1
            return {"result": {"turn": {"id": "turn-1"}}}
        if method == "config/batchWrite":
            self.writes.append((params.get("filePath"), params.get("edits")))
            return {
                "result": {
                    "status": "ok",
                    "filePath": params.get("filePath") or "/home/u/.codex/config.toml",
                    "version": f"v{len(self.writes)}",
                }
            }
        return {"result": {}}

    def thread_response(self, tid):
        state = self.threads[tid]
        return {
            "model": state["model"],
            "modelProvider": state["provider"],
            "cwd": "/tmp/work",
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "sandbox": {"type": "readOnly"},
            "thread": {"id": tid, "turns": [], "historyMode": "paginated", "path": f"/rollouts/{tid}.jsonl"},
        }


class FakeTui:
    def __init__(self, bridge):
        self.bridge = bridge
        self.received = []
        self._id = 0

    async def send(self, message):
        self.received.append(message)

    async def request(self, method, params):
        self._id += 1
        request_id = self._id
        await self.bridge.handle_client_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        for _ in range(200):
            for message in list(self.received):
                if message.get("id") == request_id:
                    return message
            await asyncio.sleep(0.005)
        raise AssertionError(f"no response for {method}")

    def response_ids(self):
        return [m.get("id") for m in self.received if "method" not in m]

    def notifications(self, method=None):
        return [
            m for m in self.received
            if "method" in m and "id" not in m and (method is None or m["method"] == method)
        ]


async def bridge_scenario():
    server = FakeAppServer()
    profile = os.path.join(tempfile.mkdtemp(prefix="picker-test-"), "codex-ds.config.toml")
    bridge = mp.Bridge(None, server.send, profile)
    tui = FakeTui(bridge)
    bridge.set_sender(tui.send)
    server.to_bridge = bridge.handle_server_message

    await tui.request("initialize", {"clientInfo": {"name": "test", "version": "0"}})

    # routing: GPT -> openai, deepseek-flash -> deepseek
    start = await tui.request(
        "thread/start",
        {"model": "gpt-5.6-terra", "cwd": "/tmp/work", "permissions": "workspace",
         "approvalPolicy": "never", "ephemeral": False},
    )
    empty_tid = start["result"]["thread"]["id"]
    start_params = dict(server.seen[-1][1])
    check("routing: gpt model uses openai",
          start_params.get("modelProvider") == "openai", str(start_params.get("modelProvider")))

    # Native history load permits switching before the first message.
    before = len(server.seen)
    switch_empty = await tui.request("thread/settings/update", {
        "threadId": empty_tid, "model": mp.DEEPSEEK_MODEL_SLUG, "effort": "high"})
    forwarded = [m for m, _ in server.seen[before:]]
    check("empty thread: same thread switches before the first message",
          "result" in switch_empty and len(server.threads) == 1
          and server.threads[empty_tid]["provider"] == "deepseek"
          and server.threads[empty_tid]["messages"] == 0, str(forwarded))
    check("empty thread: materialize, unsubscribe, resume in order",
          forwarded[:3] == ["thread/read", "thread/unsubscribe", "thread/resume"], str(forwarded))

    # routing for a deepseek thread started directly
    second = await tui.request(
        "thread/start",
        {"model": mp.DEEPSEEK_MODEL_SLUG, "cwd": "/tmp/work", "permissions": "workspace"},
    )
    deepseek_tid = second["result"]["thread"]["id"]
    deepseek_params = dict(server.seen[-1][1])
    check("routing: deepseek-flash uses deepseek provider",
          deepseek_params.get("modelProvider") == mp.DEEPSEEK_PROVIDER,
          str(deepseek_params.get("modelProvider")))
    check("routing: deepseek thread disables web search",
          deepseek_params.get("config", {}).get("web_search") == "disabled")
    check("routing: deepseek thread pins high reasoning",
          deepseek_params.get("config", {}).get("model_reasoning_effort") == "high")

    # provider switch on a thread with content: unsubscribe + verified resume
    await tui.request("turn/start", {"threadId": deepseek_tid, "model": mp.DEEPSEEK_MODEL_SLUG,
                                     "input": [{"type": "text", "text": "hi"}]})
    before = len(server.seen)
    switched = await tui.request(
        "thread/settings/update", {"threadId": deepseek_tid, "model": "gpt-5.6-terra",
                                   "effort": "high", "cwd": "/tmp/work"}
    )
    calls = list(server.seen[before:])
    methods = [m for m, _ in calls]
    resume_params = next((p for m, p in calls if m == "thread/resume"), {})
    check("switch: unsubscribe then resume",
          methods[:2] == ["thread/read", "thread/unsubscribe"] and "thread/resume" in methods, str(methods))
    check("switch: resume asks for the new provider",
          resume_params.get("modelProvider") == "openai"
          and resume_params.get("model") == "gpt-5.6-terra")
    check("switch: permissions/cwd preserved on the resume",
          resume_params.get("cwd") == "/tmp/work"
          and resume_params.get("sandbox") == "read-only", str(resume_params))
    check("switch: applied switch is verified and reported as success",
          "result" in switched, json.dumps(switched))
    check("switch: remaining settings forwarded after the resume",
          methods.count("thread/settings/update") == 1, str(methods))
    check("switch: bridge state follows the verified response",
          bridge._threads[deepseek_tid].provider == "openai"
          and bridge._threads[deepseek_tid].model == "gpt-5.6-terra")

    # switching back to deepseek re-applies its start/resume config
    before = len(server.seen)
    back = await tui.request(
        "thread/settings/update", {"threadId": deepseek_tid, "model": mp.DEEPSEEK_MODEL_SLUG}
    )
    back_resume = next(
        (p for m, p in server.seen[before:] if m == "thread/resume"), {}
    )
    check("switch back: deepseek config re-applied on the resume",
          back_resume.get("config", {}).get("web_search") == "disabled"
          and back_resume.get("config", {}).get("model_reasoning_effort") == "high"
          and "result" in back, str(back_resume.get("config")))
    check("switch back: bridge state back on deepseek",
          bridge._threads[deepseek_tid].provider == "deepseek")

    # busy thread (another subscriber / running turn): clear error + rejoin
    third = await tui.request(
        "thread/start", {"model": "gpt-5.6-terra", "cwd": "/tmp/work"}
    )
    busy_tid = third["result"]["thread"]["id"]
    await tui.request("turn/start", {"threadId": busy_tid, "model": "gpt-5.6-terra",
                                     "input": [{"type": "text", "text": "hi"}]})
    server.threads[busy_tid]["busy"] = True
    server.threads[busy_tid]["extra_subscribers"] = 1
    before = len(server.seen)
    rejected = await tui.request(
        "thread/settings/update", {"threadId": busy_tid, "model": mp.DEEPSEEK_MODEL_SLUG}
    )
    calls = server.seen[before:]
    resumed = [p for m, p in calls if m == "thread/resume"]
    check("busy switch: client gets a clear error", "error" in rejected,
          json.dumps(rejected))
    check("busy switch: old configuration rejoined",
          len(resumed) >= 2 and "modelProvider" not in resumed[-1], str(resumed))
    check("busy switch: no silent model change in bridge state",
          bridge._threads[busy_tid].provider == "openai",
          bridge._threads[busy_tid].provider)

    # RPC id isolation / forwarding
    internal_ids = {m for m in tui.response_ids() if isinstance(m, str)}
    check("ids: internal request ids never reach the TUI", not internal_ids, str(internal_ids))
    check("ids: single response per client request",
          tui.response_ids() == sorted(set(tui.response_ids())), str(tui.response_ids()))
    check("ids: client request id preserved end to end",
          1 in tui.response_ids() and 2 in tui.response_ids())
    check("ids: internal responses consumed by the bridge",
          not bridge._internal and not bridge._forwarded)

    # persistence target: model/effort -> profile, everything else -> user config
    before = len(server.writes)
    write_response = await tui.request(
        "config/batchWrite",
        {"edits": [
            {"keyPath": "model", "mergeStrategy": "replace", "value": mp.DEEPSEEK_MODEL_SLUG},
            {"keyPath": "model_reasoning_effort", "mergeStrategy": "replace", "value": "high"},
            {"keyPath": "status_line", "mergeStrategy": "replace", "value": ["model"]},
        ]},
    )
    writes = server.writes[before:]
    other_write = next((w for w in writes if w[0] is None), None)
    with open(profile) as handle:
        profile_text = handle.read()
    check("persistence: model/effort edits land in the codex-ds profile",
          writes and all(w[0] is None for w in writes)
          and 'model = "deepseek-flash"' in profile_text
          and 'model_reasoning_effort = "high"' in profile_text, str(profile_text))
    check("persistence: unrelated edits keep targeting user config",
          other_write is not None and [e["keyPath"] for e in other_write[1]] == ["status_line"],
          str(writes))
    response = write_response.get("result", {})
    check("persistence: TUI receives a ConfigWriteResponse for the profile",
          response.get("status") == "ok" and response.get("filePath") == profile
          and isinstance(response.get("version"), str), json.dumps(write_response))
    check("persistence: selection tracked for cold resume",
          bridge.selected == {"model": mp.DEEPSEEK_MODEL_SLUG, "provider": "deepseek",
                              "effort": "high"}, str(bridge.selected))

    original_write = bridge._write_profile
    def denied(edits):
        raise PermissionError("synthetic write failure")
    bridge._write_profile = denied
    for method, params in [("config/value/write", {"keyPath": "model", "value": "gpt-6-astra"}),
                           ("config/batchWrite", {"edits": [
                               {"keyPath": "model", "value": "gpt-6-astra"},
                               {"keyPath": "unrelated", "value": True}]})]:
        failed = await tui.request(method, params)
        check(f"failed profile write responds: {method}", "error" in failed)
    bridge._write_profile = original_write
    await server.emit("thread/settings/updated", {"threadId": deepseek_tid, "threadSettings": {
        "model": "deepseek-flash", "modelProvider": "deepseek", "sandboxPolicy": {"type": "readOnly"},
        "approvalPolicy": "never", "approvalsReviewer": "user"}})
    check("permissions: latest confirmed restriction replaces startup settings",
          bridge._threads[deepseek_tid].base_params.get("sandbox") == "read-only")
    clamped = bridge._clamp({"collaborationMode": {"mode": "plan", "settings": {
        "model": "deepseek-flash", "reasoning_effort": "xhigh"}}}, bridge._threads[deepseek_tid])
    check("plan: unsupported GPT effort becomes DeepSeek high",
          clamped["collaborationMode"]["settings"]["reasoning_effort"] == "high")

    check("history: no item/history notifications replayed by the switch",
          not tui.notifications("item/completed") and not tui.notifications("item/started"))
    started = tui.notifications("thread/started")
    started_ids = [
        (m.get("params", {}).get("thread", {}) or {}).get("id") for m in started
    ]
    check("history: switching never creates replacement threads",
          len(started_ids) == 3 and all(tid in bridge._threads for tid in started_ids),
          str(started_ids))
    return bridge, server, tui


async def token_scenario():
    token = "session-token"
    app = mp.websocket_app(mp.Bridge(None, None, "/tmp/unused.toml"), token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                anonymous = response.status
            async with session.get(url, headers={"Authorization": "Bearer wrong"}) as response:
                wrong = response.status
            ws_url = url.replace("http://", "ws://")
            async with session.ws_connect(
                ws_url, headers={"Authorization": f"Bearer {token}"}
            ) as websocket:
                authorized = websocket.closed is False
                await websocket.close()
        check("token: anonymous loopback request rejected", anonymous == 401, str(anonymous))
        check("token: wrong bearer token rejected", wrong == 401, str(wrong))
        check("token: correct bearer token opens the app-server websocket", authorized)
    finally:
        await runner.cleanup()


def static_scenario():
    native = [{"slug": "gpt-5.6-terra", "display_name": "GPT"},
              {"slug": mp.DEEPSEEK_MODEL_SLUG, "display_name": "DeepSeek-Flash"}]
    extra = mp.load_extra_catalog()
    merged = mp.merge_catalog(native, extra)["models"]
    slugs = [m["slug"] for m in merged]
    check("catalog: native models preserved", "gpt-5.6-terra" in slugs, str(slugs))
    check("catalog: deepseek-flash de-duplicated", slugs.count(mp.DEEPSEEK_MODEL_SLUG) == 1)
    deepseek = next((m for m in merged if m["slug"] == mp.DEEPSEEK_MODEL_SLUG), {})
    check("catalog: deepseek display name is DeepSeek V4.1 Flash",
          deepseek.get("display_name") == "DeepSeek V4.1 Flash",
          str(deepseek.get("display_name")))
    check("catalog: local deepseek definition merged in",
          bool(deepseek.get("model_messages") or deepseek.get("context_window")))
    check("routing helper: only deepseek-flash leaves openai",
          mp.provider_for_model(mp.DEEPSEEK_MODEL_SLUG) == "deepseek"
          and mp.provider_for_model("gpt-5.6-terra") == "openai"
          and mp.provider_for_model(None) == "openai")
    target = os.path.join(tempfile.mkdtemp(prefix="picker-profile-"), "codex-ds.config.toml")
    created = mp.ensure_profile_file(target)
    check("profile: missing profile file created", created and os.path.exists(target))
    check("profile: existing profile file preserved", mp.ensure_profile_file(target) is False)
    cleaned = mp.strip_conflicting_args(
        ["resume", "--last", "--remote", "ws://x", "--profile", "other", "-m", "gpt-5.6-terra"]
    )
    check("args: adapter-owned flags stripped before forwarding",
          cleaned == ["resume", "--last", "-m", "gpt-5.6-terra"], str(cleaned))

    profile = os.path.join(tempfile.mkdtemp(prefix="picker-toml-"), "codex-ds.config.toml")
    with open(profile, "w") as handle:
        handle.write(
            'model = "gpt-5.6-terra"\n'
            'model_reasoning_effort = "xhigh"\n'
            'approval_policy = "never"\n'
            "\n"
            "[tui]\n"
            'model = "not-a-top-level-key"\n'
        )
    mp.apply_profile_edits(profile, [
        {"keyPath": "model", "mergeStrategy": "replace", "value": "deepseek-flash"},
        {"keyPath": "model_reasoning_effort", "mergeStrategy": "replace", "value": "high"},
        {"keyPath": "plan_mode_reasoning_effort", "mergeStrategy": "replace", "value": "high"},
    ])
    with open(profile) as handle:
        text = handle.read()
    check("profile file: existing top-level keys replaced in place",
          'model = "deepseek-flash"' in text
          and text.count('model = "deepseek-flash"') == 1
          and 'model_reasoning_effort = "high"' in text, text)
    check("profile file: table keys and lookalikes untouched",
          'model = "not-a-top-level-key"' in text and 'approval_policy = "never"' in text,
          text)
    check("profile file: new key inserted into the root table",
          text.index("plan_mode_reasoning_effort") < text.index("[tui]"), text)


async def native_scenario():
    """Exercise the installed Codex protocol without making model API calls."""
    with tempfile.TemporaryDirectory(prefix="picker-native-") as scratch:
        home = os.path.join(scratch, "home")
        os.mkdir(home)
        with open(os.path.join(home, "config.toml"), "w") as handle:
            handle.write('model = "gpt-6-astra"\n')
        catalog = os.path.join(scratch, "models.json")
        with open(catalog, "w") as handle:
            json.dump(mp.merge_catalog(mp.load_native_catalog(home), mp.load_extra_catalog()), handle)
        bridge = mp.Bridge(None, None, os.path.join(home, mp.PROFILE_FILENAME))
        tui = FakeTui(bridge)
        bridge.set_sender(tui.send)
        server = await mp.CodexServer.spawn(home, catalog, bridge)
        bridge.set_server_sender(server.send)
        try:
            await tui.request("initialize", {"clientInfo": {"name": "picker-check", "version": "1"},
                                             "capabilities": {"experimentalApi": True}})
            started = await tui.request("thread/start", {"model": "gpt-6-astra", "cwd": scratch,
                "approvalPolicy": "never", "sandbox": "read-only", "historyMode": "paginated"})
            tid = started["result"]["thread"]["id"]
            switched = await tui.request("thread/settings/update", {"threadId": tid, "model": "deepseek-flash"})
            check("native: empty paginated thread switches without changing id",
                  "result" in switched and bridge._threads[tid].provider == "deepseek"
                  and bridge._threads[tid].server_id == tid, str(switched))
            await tui.request("thread/inject_items", {"threadId": tid, "items": [{
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "PICKER_HISTORY_MARKER"}]}]})
            # Confirm a permission change is retained through a provider reload.
            await tui.request("thread/settings/update", {"threadId": tid,
                "sandboxPolicy": {"type": "readOnly"}, "approvalPolicy": "never"})
            back = await tui.request("thread/settings/update", {"threadId": tid, "model": "gpt-6-astra"})
            check("native: switches back and preserves current permissions",
                  "result" in back and bridge._threads[tid].provider == "openai"
                  and bridge._threads[tid].base_params.get("sandbox") == "read-only", str(back))
            rollout = bridge._threads[tid].rollout_path
            with open(rollout) as handle:
                check("native: history survives provider changes", "PICKER_HISTORY_MARKER" in handle.read())
            # Simulate a stale provider from saved metadata on cold resume.
            await tui.request("thread/unsubscribe", {"threadId": tid})
            bridge._threads.clear()
            cold = await tui.request("thread/resume", {"threadId": tid, "model": "deepseek-flash", "excludeTurns": True})
            check("native: cold resume uses the selected provider",
                  cold.get("result", {}).get("modelProvider") == "deepseek", str(cold.get("error")))
        finally:
            await server.stop()


async def main():
    await bridge_scenario()
    await token_scenario()
    static_scenario()
    if "--native" in sys.argv:
        await native_scenario()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
