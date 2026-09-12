#!/usr/bin/env python3
"""Native /model provider switching via an authenticated local app-server bridge.

Usage: model-picker.py [interactive codex arguments]
Native Codex owns authentication and history. Model defaults are saved separately
in codex-ds.config.toml; ordinary Codex configuration is left to native Codex.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib

from collections import defaultdict

import aiohttp
from aiohttp import web

DEEPSEEK_MODEL_SLUG = "deepseek-flash"
DEEPSEEK_MODEL_DISPLAY = "DeepSeek V4.1 Flash"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_PROVIDER_NAME = "DeepSeek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
DEEPSEEK_ENV_KEY = "DEEPSEEK_API_KEY"
DEFAULT_PROVIDER = "openai"

PROFILE_NAME = "codex-ds"
PROFILE_FILENAME = f"{PROFILE_NAME}.config.toml"

# Task-specific environment variable holding the loopback bearer token.
REMOTE_TOKEN_ENV = "CODEX_PICKER_WS_TOKEN"

INTERNAL_ID_PREFIX = "picker-"
MODEL_CONFIG_KEYS = ("model", "model_reasoning_effort", "plan_mode_reasoning_effort")
DEEPSEEK_EFFORTS = ("low", "high", "max")
DEEPSEEK_EFFORT = "high"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_SOURCE = os.path.join(SCRIPT_DIR, "models.json")

LOG_PREFIX = "[model-picker]"
DEBUG = bool(os.environ.get("MODEL_PICKER_DEBUG"))


def log(message: str) -> None:
    """Diagnostics only: never message bodies, tokens or auth material."""
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def debug(message: str) -> None:
    if DEBUG:
        log(message)


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------
def provider_for_model(model):
    """Route model slugs to providers.  Only deepseek-flash leaves OpenAI."""
    if isinstance(model, str) and model.strip() == DEEPSEEK_MODEL_SLUG:
        return DEEPSEEK_PROVIDER
    return DEFAULT_PROVIDER


def load_native_catalog(codex_home):
    """Return the raw native catalog as a list of model dicts."""
    env = dict(os.environ)
    env["CODEX_HOME"] = codex_home
    for extra in ([], ["--bundled"]):
        try:
            completed = subprocess.run(
                ["codex", "debug", "models", *extra],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            debug(f"native catalog probe failed: {exc!r}")
            continue
        if completed.returncode != 0:
            debug(f"native catalog probe exit {completed.returncode}")
            continue
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            continue
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list) and models:
            return models
    return []


def load_extra_catalog(path=CATALOG_SOURCE):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        debug(f"extra catalog unavailable: {exc!r}")
        return []
    models = payload.get("models") if isinstance(payload, dict) else payload
    return models if isinstance(models, list) else []


def merge_catalog(native_models, extra_models):
    """Merge native + local catalogs, de-duplicating by slug."""
    merged = {}
    order = []
    for source in (native_models or [], extra_models or []):
        for model in source:
            if not isinstance(model, dict):
                continue
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            if slug in merged:
                merged[slug].update(model)
            else:
                merged[slug] = dict(model)
                order.append(slug)
    models = []
    for slug in order:
        entry = merged[slug]
        if slug == DEEPSEEK_MODEL_SLUG:
            entry["display_name"] = DEEPSEEK_MODEL_DISPLAY
            entry["description"] = "DeepSeek V4.1 Flash, billed through your DeepSeek API key."
        models.append(entry)
    return {"models": models}


def ensure_profile_file(path):
    """Create the native codex-ds profile file when it does not exist."""
    if os.path.exists(path):
        return False
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return True


def apply_profile_edits(path, edits):
    """Apply keyPath/value edits to the top-level table of a TOML file.

    Returns the new content version.  Nothing but the profile file is touched.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    expected = tomllib.loads("\n".join(lines))

    for edit in edits or []:
        key = edit.get("keyPath")
        value = edit.get("value")
        if key not in MODEL_CONFIG_KEYS or (value is not None and not isinstance(value, str)):
            raise ValueError("profile edits must be model/effort strings or null")
        if value is None:
            expected.pop(key, None)
        else:
            expected[key] = value
        new_line = f"{key} = {json.dumps(value, ensure_ascii=False)}" if value is not None else None
        replaced = False
        output = []
        in_root = True
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_root = False
            if in_root and not replaced:
                body = stripped.split("#", 1)[0].strip()
                head, separator, _ = body.partition("=")
                if separator and head.strip() == key:
                    replaced = True
                    if new_line is not None:
                        output.append(new_line)
                    continue
            output.append(line)
        if new_line is not None and not replaced:
            insert_at = len(output)
            for index, line in enumerate(output):
                if line.strip().startswith("["):
                    insert_at = index
                    break
            output.insert(insert_at, new_line)
        lines = output

    content = "\n".join(lines) + ("\n" if lines else "")
    # Refuse unusual TOML layouts instead of risking unrelated profile settings.
    if tomllib.loads(content) != expected:
        raise ValueError("profile layout cannot be updated without changing unrelated settings")
    fd, temporary = tempfile.mkstemp(prefix=".codex-ds-", dir=directory or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# provider CLI overrides
# ---------------------------------------------------------------------------
def deepseek_provider_overrides():
    return [
        "-c",
        f'model_providers.{DEEPSEEK_PROVIDER}.name="{DEEPSEEK_PROVIDER_NAME}"',
        "-c",
        f'model_providers.{DEEPSEEK_PROVIDER}.base_url="{DEEPSEEK_BASE_URL}"',
        "-c",
        f'model_providers.{DEEPSEEK_PROVIDER}.wire_api="responses"',
        "-c",
        f'model_providers.{DEEPSEEK_PROVIDER}.env_key="{DEEPSEEK_ENV_KEY}"',
        "-c",
        f"model_providers.{DEEPSEEK_PROVIDER}.requires_openai_auth=false",
    ]


def catalog_override(path):
    return ["-c", f"model_catalog_json={json.dumps(path)}"]


def strip_conflicting_args(args):
    """Drop remote/profile flags the adapter owns (see module docstring)."""
    value_options = {"--remote", "--remote-auth-token-env", "-p", "--profile"}
    cleaned = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--":
            cleaned.extend(args[index:])
            break
        if argument in value_options:
            index += 2
            continue
        if any(argument.startswith(option + "=") for option in value_options):
            index += 1
            continue
        cleaned.append(argument)
        index += 1
    return cleaned


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------
class ThreadState:
    """What the adapter knows about one native thread."""

    __slots__ = (
        "client_id",
        "server_id",
        "model",
        "provider",
        "base_params",
        "rollout_path",
                "ephemeral",
    )

    def __init__(self, client_id, server_id, model, provider, base_params):
        self.client_id = client_id
        self.server_id = server_id
        self.model = model
        self.provider = provider
        self.base_params = dict(base_params or {})
        self.rollout_path = None
        self.ephemeral = False


class SwitchResult:
    __slots__ = ("ok", "error", "response")

    def __init__(self, ok, error=None, response=None):
        self.ok = ok
        self.error = error
        self.response = response


class Bridge:
    """Provider-aware JSON-RPC bridge between the native TUI and app-server."""

    def __init__(self, send_to_client, send_to_server, profile_path):
        self._send_to_client = send_to_client
        self._send_to_server = send_to_server
        self.profile_path = profile_path

        self._id_seq = 0
        self._internal = {}
        self._forwarded = {}
        self._threads = {}  # client thread id -> ThreadState
        self._locks = defaultdict(asyncio.Lock)
        self._tasks = set()
        self.selected = None  # last model/effort selection of this session

    # -- transport -----------------------------------------------------
    async def _client(self, message):
        sender = self._send_to_client
        if sender is None:
            debug(f"client unavailable, dropping {message.get('method') or 'response'}")
            return
        await sender(message)

    async def _server(self, message):
        await self._send_to_server(message)

    def set_sender(self, sender):
        self._send_to_client = sender

    def set_server_sender(self, sender):
        self._send_to_server = sender

    # -- internals -----------------------------------------------------
    def _next_internal_id(self):
        self._id_seq += 1
        return f"{INTERNAL_ID_PREFIX}{self._id_seq}"

    async def internal_request(self, method, params, timeout=30.0):
        """Send a request the TUI never sees; return its raw response or None."""
        request_id = self._next_internal_id()
        future = asyncio.get_running_loop().create_future()
        self._internal[request_id] = future
        await self._server(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._internal.pop(request_id, None)
            debug(f"internal {method} timed out")
            return None

    @staticmethod
    def _error_message(response):
        if not isinstance(response, dict):
            return "app-server did not answer the provider switch request"
        error = response.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "unknown error")
        return "app-server did not apply the provider switch"

    # -- client -> server ---------------------------------------------
    async def handle_client_message(self, message):
        if not isinstance(message, dict):
            return
        if "method" in message and "id" in message:
            await self._handle_client_request(message)
            return
        if "method" in message:
            await self._handle_client_notification(message)
            return
        await self._handle_client_response(message)

    async def _handle_client_request(self, message):
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        debug(f"tui -> app-server {method}")
        if method in ("thread/start", "thread/resume", "thread/fork"):
            prepared = self._prepare_thread_params(method, params)
            # Remember the TUI's own params: provider-specific additions must
            # never leak back into a later GPT switch.
            self._forwarded[message["id"]] = (method, prepared, dict(params))
            await self._server({**message, "params": prepared})
            return
        if method == "turn/start":
            self._spawn(self._handle_turn_start(message, params))
            return
        if method == "thread/settings/update":
            self._spawn(self._handle_settings_update(message, params))
            return
        if method in ("config/batchWrite", "config/value/write"):
            self._spawn(self._handle_config_write(message, params))
            return
        await self._server(message)

    async def _handle_client_notification(self, message):
        await self._server(message)

    async def _handle_client_response(self, message):
        # Response to an app-server initiated request: forward untouched.
        await self._server(message)

    def _spawn(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- server -> client ---------------------------------------------
    async def handle_server_message(self, message):
        if not isinstance(message, dict):
            return
        if "method" in message and "id" in message:
            await self._client(message)
            return
        if "method" in message:
            if message["method"] == "thread/settings/updated":
                params = message.get("params", {})
                state = self._threads.get(params.get("threadId"))
                if state:
                    self._remember_settings(params.get("threadSettings", {}), state)
            await self._client(message)
            return
        await self._handle_server_response(message)

    async def _handle_server_response(self, message):
        response_id = message.get("id")
        future = self._internal.pop(response_id, None)
        if future is not None:
            if not future.done():
                future.set_result(message)
            return
        forwarded = self._forwarded.pop(response_id, None)
        if forwarded is not None:
            self._observe_thread_response(forwarded, message)
            result = message.get("result", {})
            tid = result.get("thread", {}).get("id")
            state = self._threads.get(tid)
            # Rollout metadata can retain the provider from before /model.
            target = forwarded[1].get("model") or (state.model if state else None)
            if state and target and provider_for_model(target) != state.provider:
                self._spawn(self._finish_resume(message, tid, state, target))
                return
        await self._client(message)

    async def _finish_resume(self, message, thread_id, state, target):
        switched = await self._switch_provider(thread_id, target,
                                               provider_for_model(target), state)
        if not switched.ok:
            await self._client({"id": message["id"], "error": {
                "code": -32000, "message": switched.error}})
            return
        # Keep the requested history page; replace only the configuration fields.
        result = {**message["result"], **(switched.response or {})}
        result["thread"] = message["result"]["thread"]
        await self._client({**message, "result": result})

    def _deepseek_config(self, params):
        config = params.get("config")
        merged = dict(config) if isinstance(config, dict) else {}
        if merged.get("model_reasoning_effort") not in DEEPSEEK_EFFORTS:
            merged["model_reasoning_effort"] = DEEPSEEK_EFFORT
        merged["plan_mode_reasoning_effort"] = DEEPSEEK_EFFORT
        merged["web_search"] = "disabled"
        params["config"] = merged
        if "serviceTier" in params:
            params["serviceTier"] = None
        return params

    def _prepare_thread_params(self, method, params):
        params = dict(params)
        model = params.get("model")
        if not model and self.selected is not None:
            model = self.selected.get("model")
        if model:
            params["model"] = model
            params["modelProvider"] = provider_for_model(model)
            if params["modelProvider"] == DEEPSEEK_PROVIDER:
                params = self._deepseek_config(params)
        return params

    def _observe_thread_response(self, forwarded, message):
        method, request_params, client_params = forwarded
        result = message.get("result")
        if not isinstance(result, dict):
            return
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        server_id = thread.get("id") or request_params.get("threadId")
        client_id = server_id
        if not client_id or not server_id:
            return
        model = result.get("model") or request_params.get("model")
        provider = result.get("modelProvider") or request_params.get("modelProvider")
        if method in ("thread/start", "thread/fork") or client_id not in self._threads:
            state = ThreadState(client_id, server_id, model, provider, client_params)
            self._threads[client_id] = state
        else:
            state = self._threads[client_id]
            state.server_id = server_id
            state.model = model
            state.provider = provider
            state.base_params.update(client_params)
        state.rollout_path = thread.get("path")
        state.ephemeral = thread.get("ephemeral", False)
        self._remember_settings(result, state)

    # -- turn/start ----------------------------------------------------
    async def _handle_turn_start(self, message, params):
        params = dict(params)
        thread_id = params.get("threadId") or params.get("thread_id")
        target_model = params.get("model")
        collaboration = params.get("collaborationMode")
        if not target_model and isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict):
                target_model = settings.get("model")
        state = self._threads.get(thread_id)
        if target_model and state is not None and state.provider:
            provider = provider_for_model(target_model)
            if provider != state.provider:
                result = await self._switch_provider(
                    thread_id, target_model, provider, state
                )
                if not result.ok:
                    await self._client(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32000, "message": result.error},
                        }
                    )
                    return
            params = self._clamp(params, state)
        await self._server({**message, "params": params})

    # -- thread/settings/update ----------------------------------------
    async def _handle_settings_update(self, message, params):
        params = dict(params)
        thread_id = params.get("threadId") or params.get("thread_id")
        collaboration = params.get("collaborationMode")
        collaboration_model = None
        if isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict):
                collaboration_model = settings.get("model")
        target_model = params.get("model") or collaboration_model
        state = self._threads.get(thread_id)

        if state is None or not target_model:
            await self._server({**message, "params": self._clamp(params, state) if state else params})
            return
        target_provider = provider_for_model(target_model)
        if target_provider == state.provider:
            await self._server(
                {**message, "params": self._clamp(params, state)}
            )
            return

        result = await self._switch_provider(thread_id, target_model, target_provider, state)
        if not result.ok:
            await self._client(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32000, "message": result.error},
                }
            )
            return
        remaining = self._remaining_settings(params, target_model, state)
        response = await self.internal_request("thread/settings/update", remaining)
        if response is None:
            await self._client({"jsonrpc": "2.0", "id": message["id"],
                                "error": {"code": -32000, "message": "settings update timed out"}})
            return
        if "error" in response:
            await self._client(
                {"jsonrpc": "2.0", "id": message["id"], "error": response["error"]}
            )
            return
        await self._client(
            {"jsonrpc": "2.0", "id": message["id"], "result": response.get("result", {})}
        )

    def _clamp(self, params, state):
        params = dict(params)
        if state.provider == DEEPSEEK_PROVIDER:
            effort = params.get("effort")
            if effort is not None and effort not in DEEPSEEK_EFFORTS:
                params["effort"] = DEEPSEEK_EFFORT
            if params.get("serviceTier"):
                params["serviceTier"] = None
            collaboration = params.get("collaborationMode")
            if isinstance(collaboration, dict):
                settings = dict(collaboration.get("settings") or {})
                if settings.get("reasoning_effort") not in DEEPSEEK_EFFORTS:
                    settings["reasoning_effort"] = DEEPSEEK_EFFORT
                params["collaborationMode"] = {**collaboration, "settings": settings}
        return params

    def _remember_settings(self, params, state):
        """Preserve the server-confirmed permissions across provider reloads."""
        for key in ("cwd", "approvalPolicy", "approvalsReviewer", "serviceTier"):
            if key in params:
                state.base_params[key] = params[key]
        if params.get("model"):
            state.model = params["model"]
        if params.get("modelProvider"):
            state.provider = params["modelProvider"]
        active = params.get("activePermissionProfile")
        if active:
            state.base_params["permissions"] = active["id"]
            state.base_params.pop("sandbox", None)
        elif "sandboxPolicy" in params or "sandbox" in params:
            policy = params.get("sandboxPolicy", params.get("sandbox"))
            if isinstance(policy, dict):
                mode = {"readOnly": "read-only", "workspaceWrite": "workspace-write",
                        "dangerFullAccess": "danger-full-access"}.get(policy.get("type"))
                if mode:
                    state.base_params.pop("permissions", None)
                    state.base_params["sandbox"] = mode
                    if mode == "workspace-write":
                        config = dict(state.base_params.get("config") or {})
                        state.base_params["config"] = config
                        config["sandbox_workspace_write"] = {
                            "writable_roots": policy.get("writableRoots", []),
                            "network_access": policy.get("networkAccess", False),
                            "exclude_tmpdir_env_var": policy.get("excludeTmpdirEnvVar", False),
                            "exclude_slash_tmp": policy.get("excludeSlashTmp", False),
                        }

    def _remaining_settings(self, params, target_model, state):
        remaining = {
            key: value
            for key, value in params.items()
            if key not in ("model", "thread_id")
        }
        remaining["threadId"] = params.get("threadId") or params.get("thread_id")
        collaboration = remaining.get("collaborationMode")
        if isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict) and settings.get("model"):
                collaboration = dict(collaboration)
                settings = dict(settings)
                settings["model"] = target_model
                collaboration["settings"] = settings
                remaining["collaborationMode"] = collaboration
        if state.provider == DEEPSEEK_PROVIDER:
            remaining = self._clamp(remaining, state)
        return {key: value for key, value in remaining.items() if value is not None}

    # -- provider switch ------------------------------------------------
    async def _switch_provider(self, thread_id, target_model, target_provider, state):
        async with self._locks[thread_id]:
            if state.provider == target_provider:
                return SwitchResult(True)
            # Loading history materializes even a new, empty rollout. In 0.154
            # includeTurns may return "list_turns is not supported yet" after
            # that flush; the verified resume below is the authoritative check.
            if state.ephemeral:
                return SwitchResult(False, "Provider switching requires a saved thread; start without ephemeral mode.")
            if target_provider == DEEPSEEK_PROVIDER and not os.environ.get(DEEPSEEK_ENV_KEY):
                return SwitchResult(False, "Export DEEPSEEK_API_KEY before launching codex_ds.")
            if not (state.rollout_path and os.path.exists(state.rollout_path)):
                await self.internal_request("thread/read", {
                    "threadId": state.server_id, "includeTurns": True,
                })
            return await self._resume_switch(thread_id, target_model, target_provider, state)

    async def _resume_switch(self, thread_id, target_model, target_provider, state):
        old_model = state.model
        old_provider = state.provider
        server_id = state.server_id
        await self.internal_request("thread/unsubscribe", {"threadId": server_id})
        resume_params = {"threadId": server_id, "model": target_model,
                         "modelProvider": target_provider, "excludeTurns": True}
        for key in ("cwd", "config", "approvalPolicy", "approvalsReviewer",
                    "sandbox", "permissions", "serviceTier", "runtimeWorkspaceRoots",
                    "baseInstructions", "developerInstructions"):
            if key in state.base_params:
                value = state.base_params[key]
                resume_params[key] = dict(value) if isinstance(value, dict) else value
        if target_provider == DEEPSEEK_PROVIDER:
            resume_params = self._deepseek_config(resume_params)
        response = await self.internal_request("thread/resume", resume_params)
        result = response.get("result") if isinstance(response, dict) else None
        applied = (
            isinstance(result, dict)
            and result.get("model") == target_model
            and result.get("modelProvider") == target_provider
        )
        if applied:
            state.model = target_model
            state.provider = target_provider
            if isinstance(result.get("thread"), dict) and result["thread"].get("id"):
                state.server_id = result["thread"]["id"]
            return SwitchResult(True, response=result)

        # Never leave the TUI detached from its thread: rejoin old settings.
        rejoin = await self.internal_request("thread/resume", {"threadId": server_id, "excludeTurns": True})
        rejoined = isinstance(rejoin, dict) and isinstance(rejoin.get("result"), dict)
        detail = self._error_message(response)
        if not rejoined:
            detail += "; the previous provider could not be rejoined"
        message = (
            f"Could not switch thread {thread_id} to {target_model} "
            f"({target_provider}): {detail}. The thread is busy, has another "
            f"subscriber, or the current model/provider could not be replaced."
        )
        if rejoined and old_provider:
            state.model = old_model
            state.provider = old_provider
        debug(f"switch rejected for {thread_id}")
        return SwitchResult(False, message)

    # -- config persistence ---------------------------------------------
    async def _handle_config_write(self, message, params):
        edits = params.get("edits", [{"keyPath": params.get("keyPath"),
                                     "value": params.get("value")}])
        model_edits = [e for e in edits if e.get("keyPath") in MODEL_CONFIG_KEYS]
        if not model_edits:
            await self._server(message)
            return
        try:
            result = self._write_profile(model_edits)
        except (OSError, ValueError) as exc:
            await self._client({"id": message["id"], "error": {
                "code": -32600, "message": f"could not write the codex-ds profile: {exc}"}})
            return
        for edit in model_edits:
            self._note_selection(edit["keyPath"], edit.get("value"))
        other = [e for e in edits if e not in model_edits]
        if other:
            remaining = await self.internal_request("config/batchWrite", {**params, "edits": other})
            if remaining is None or "error" in remaining:
                await self._client({"id": message["id"], "error": (remaining or {}).get(
                    "error", {"code": -32000, "message": "user config write timed out"})})
                return
        await self._client({"id": message["id"], "result": result})

    def _write_profile(self, edits):
        version = apply_profile_edits(self.profile_path, edits)
        return {
            "status": "ok",
            "filePath": self.profile_path,
            "version": version,
            "overriddenMetadata": None,
        }

    def _note_selection(self, key_path, value):
        if key_path == "model" and isinstance(value, str):
            self.selected = {
                "model": value,
                "provider": provider_for_model(value),
                "effort": (self.selected or {}).get("effort"),
            }
        elif key_path == "model_reasoning_effort" and isinstance(value, str):
            selection = dict(self.selected or {})
            selection["effort"] = value
            self.selected = selection


# ---------------------------------------------------------------------------
# websocket gateway
# ---------------------------------------------------------------------------
def websocket_app(bridge, token):
    """Loopback app-server endpoint that requires the session bearer token."""

    expected = f"Bearer {token}"

    async def handler(request):
        header = request.headers.get("Authorization", "")
        if not secrets.compare_digest(header, expected):
            return web.Response(status=401, text="unauthorized")
        websocket = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
        await websocket.prepare(request)

        async def send_to_tui(message):
            await websocket.send_str(json.dumps(message))

        bridge.set_sender(send_to_tui)
        try:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except ValueError:
                        continue
                    await bridge.handle_client_message(payload)
                elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            bridge.set_sender(None)
        return websocket

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/{tail:.*}", handler)
    return app


# ---------------------------------------------------------------------------
# process plumbing
# ---------------------------------------------------------------------------
class CodexServer:
    def __init__(self, process, bridge):
        self.process = process
        self.bridge = bridge
        self._lock = asyncio.Lock()
        self._reader = None
        self._stderr = None

    @classmethod
    async def spawn(cls, codex_home, catalog_path, bridge):
        env = dict(os.environ)
        env["CODEX_HOME"] = codex_home
        argv = [
            "codex",
            "app-server",
            "--stdio",
            *deepseek_provider_overrides(),
            *(catalog_override(catalog_path) if catalog_path else []),
        ]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024,
            env=env,
        )
        server = cls(process, bridge)
        server._reader = asyncio.get_running_loop().create_task(server._read_stdout())
        server._stderr = asyncio.get_running_loop().create_task(server._read_stderr())
        return server

    async def send(self, message):
        async with self._lock:
            if self.process.stdin is None or self.process.returncode is not None:
                return
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self.process.stdin.drain()

    async def _read_stdout(self):
        stdout = self.process.stdout
        while stdout is not None:
            line = await stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                debug("app-server emitted a non-JSON line")
                continue
            await self.bridge.handle_server_message(payload)

    async def _read_stderr(self):
        stderr = self.process.stderr
        while stderr is not None:
            line = await stderr.readline()
            if not line:
                break
            if DEBUG:
                sys.stderr.write("[app-server] " + line.decode("utf-8", "replace"))
                sys.stderr.flush()

    async def stop(self):
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()


class Session:
    def __init__(self, user_args, codex_home):
        self.user_args = user_args
        self.codex_home = codex_home
        self.catalog_dir = None
        self.catalog_path = None
        self.token = secrets.token_urlsafe(32)
        self.bridge = None
        self.server = None
        self.tui = None
        self.ws_runner = None
        self.stop_event = asyncio.Event()

    async def start(self):
        profile_path = os.path.join(self.codex_home, PROFILE_FILENAME)
        if ensure_profile_file(profile_path):
            log(f"created profile {PROFILE_FILENAME}")

        self.catalog_dir = tempfile.mkdtemp(prefix="model-picker-")
        self.catalog_path = os.path.join(self.catalog_dir, "models.json")
        native = load_native_catalog(self.codex_home)
        extra = load_extra_catalog()
        merged = merge_catalog(native, extra)
        if not native or not any(m.get("slug") == DEEPSEEK_MODEL_SLUG for m in extra):
            raise SystemExit(f"{LOG_PREFIX} could not load both native and DeepSeek model catalogs")
        if merged["models"]:
            with open(self.catalog_path, "w", encoding="utf-8") as handle:
                json.dump(merged, handle)
            log(f"model catalog: {len(merged['models'])} models")
            catalog_args = catalog_override(self.catalog_path)
        self.bridge = Bridge(self._no_client, self._no_server, profile_path)
        self.server = await CodexServer.spawn(
            self.codex_home, self.catalog_path if catalog_args else None, self.bridge
        )
        self.bridge.set_server_sender(self.server.send)

        runner = web.AppRunner(websocket_app(self.bridge, self.token), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.ws_runner = runner
        port = runner.addresses[0][1]

        env = dict(os.environ)
        env["CODEX_HOME"] = self.codex_home
        env[REMOTE_TOKEN_ENV] = self.token
        argv = [
            "codex",
            "--remote",
            f"ws://127.0.0.1:{port}",
            "--remote-auth-token-env",
            REMOTE_TOKEN_ENV,
            "-p",
            PROFILE_NAME,
            *catalog_args,
            *deepseek_provider_overrides(),
            *strip_conflicting_args(self.user_args),
        ]
        try:
            self.tui = await asyncio.create_subprocess_exec(*argv, env=env)
        except OSError as exc:
            raise SystemExit(f"{LOG_PREFIX} could not start the native codex TUI: {exc}")
        debug(f"tui attached to loopback port {port}")

    async def _no_client(self, message):
        debug("no TUI connected; dropping message")

    async def _no_server(self, message):
        debug("app-server unavailable; dropping message")

    async def wait(self):
        tui_wait = asyncio.ensure_future(self.tui.wait())
        server_wait = asyncio.ensure_future(self.server.process.wait())
        stop_wait = asyncio.ensure_future(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {tui_wait, server_wait, stop_wait, self.server._reader},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (tui_wait, server_wait, stop_wait):
                task.cancel()
        if self.server._reader in done and self.server._reader.exception():
            raise RuntimeError("native app-server response reader failed") from self.server._reader.exception()
        if stop_wait in done:
            debug("shutdown requested")
        elif server_wait in done:
            debug("app-server exited; stopping the adapter")
        else:
            debug("TUI exited; stopping the adapter")
        return self.tui.returncode if self.tui.returncode is not None else 1

    def request_stop(self):
        self.stop_event.set()

    async def shutdown(self):
        if self.tui is not None and self.tui.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.tui.terminate()
            try:
                await asyncio.wait_for(self.tui.wait(), timeout=10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.tui.kill()
                await self.tui.wait()
        if self.server is not None:
            await self.server.stop()
        if self.ws_runner is not None:
            await self.ws_runner.cleanup()
        if self.catalog_dir:
            shutil.rmtree(self.catalog_dir, ignore_errors=True)


def resolve_codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex"
    )


async def run_session(user_args, codex_home):
    session = Session(user_args, codex_home)
    loop = asyncio.get_running_loop()
    interrupts = {"count": 0}

    def on_interrupt():
        # The first Ctrl-C belongs to the TUI (interrupt a turn / quit).  A
        # second one tears the adapter and its children down.
        interrupts["count"] += 1
        if interrupts["count"] > 1:
            session.request_stop()

    for signal_name, handler in (
        ("SIGINT", on_interrupt),
        ("SIGTERM", session.request_stop),
        ("SIGHUP", session.request_stop),
    ):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, handler)
    try:
        await session.start()
        return await session.wait()
    finally:
        await session.shutdown()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    return asyncio.run(run_session(args, resolve_codex_home()))


if __name__ == "__main__":
    sys.exit(main())
