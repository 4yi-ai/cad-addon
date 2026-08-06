from __future__ import annotations

import base64
import contextlib
import io
import json
import locale
import os
import platform
import queue
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import FreeCAD as App
except Exception:
    App = None

try:
    import FreeCADGui as Gui
except Exception:
    Gui = None

try:
    from PySide import QtCore, QtWidgets
except Exception:
    try:
        from PySide2 import QtCore, QtWidgets
    except Exception:
        try:
            from PySide6 import QtCore, QtWidgets
        except Exception:
            QtCore = None
            QtWidgets = None


def _ui_language() -> str:
    """Return 'zh' or 'en' from FreeCAD's UI language, defaulting to 'en'.

    Reads User parameter:BaseApp/Preferences/General -> Language (e.g. "Chinese
    Simplified", "English"). Falls back to the OS locale, then 'en'.
    """
    lang = ""
    if App is not None:
        try:
            lang = App.ParamGet("User parameter:BaseApp/Preferences/General").GetString("Language", "") or ""
        except Exception:
            lang = ""
    lang = lang.strip().lower()
    if "chinese" in lang or lang.startswith("zh"):
        return "zh"
    if not lang:
        try:
            loc = (locale.getdefaultlocale()[0] or "").lower()
            if loc.startswith("zh"):
                return "zh"
        except Exception:
            pass
    return "en"


def t(zh: str, en: str) -> str:
    """Pick the zh or en string for the current FreeCAD UI language."""
    return zh if _ui_language() == "zh" else en


ADDON_VERSION = "0.4.2"
USER_AGENT = "4yi-freecad-companion/0.4.2"
PARAM_GROUP_PATH = "User parameter:BaseApp/Preferences/Mod/FourYiCad"
COMMAND_OPEN_PANEL = "FourYi_OpenPanel"
COMMAND_START_BRIDGE = "FourYi_StartBridge"
COMMAND_STOP_BRIDGE = "FourYi_StopBridge"
COMMAND_EXPORT_SUPPORT_BUNDLE = "FourYi_ExportSupportBundle"
COMMAND_CONNECTION_SETTINGS = "FourYi_ConnectionSettings"
SUPPORTED_COMMANDS = [
    "inspect_document",
    "load_model",
    "select_object",
    "run_macro",
    "save_revision",
    "capture_screenshot",
]
RECENT_EVENTS: list[dict[str, Any]] = []
_BRIDGE_RUNTIME: "InProcessBridgeRuntime | None" = None
_COMMANDS_REGISTERED = False
_PANEL_DIALOG = None
_PANEL_AUTOSTARTED = False

JsonPost = Callable[[str, dict[str, Any], float], dict[str, Any]]


class BridgeCommandError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def commands() -> list[str]:
    return [
        COMMAND_OPEN_PANEL,
        COMMAND_START_BRIDGE,
        COMMAND_STOP_BRIDGE,
        COMMAND_EXPORT_SUPPORT_BUNDLE,
        COMMAND_CONNECTION_SETTINGS,
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.1, value)


def env_int(env: dict[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def workspace(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    return Path(env.get("CAD_SESSION_WORKSPACE") or "/workspace")


def addon_params():
    """FreeCAD.ParamGet(PARAM_GROUP_PATH), or None when FreeCAD is unavailable."""
    if App is None:
        return None
    try:
        return App.ParamGet(PARAM_GROUP_PATH)
    except Exception:
        return None


def local_session_id(params=None) -> str:
    """Read the persisted local remote-session id, generating+persisting one on first use."""
    params = params if params is not None else addon_params()
    if params is not None:
        existing = (params.GetString("LocalSessionId", "") or "").strip()
        if existing:
            return existing
    session_id = "local-%s" % secrets.token_hex(6)
    if params is not None:
        try:
            params.SetString("LocalSessionId", session_id)
        except Exception:
            pass
    return session_id


def remote_overlay_env(
    base_env: dict[str, str] | None = None,
    params=None,
) -> dict[str, str]:
    """Derive the effective process env for remote (user-machine) workbench mode.

    Container/kiosk mode (base_env already carries CAD_BRIDGE_POLL_URL) is left
    entirely untouched -- the FreeCAD ParamGet param layer must not participate.
    Otherwise, when a non-empty ServerUrl param is configured, synthesize the
    bridge/control-plane URLs (and bearer token, if any) from it.
    """
    base_env = base_env if base_env is not None else os.environ
    if (base_env.get("CAD_BRIDGE_POLL_URL") or "").strip():
        return dict(base_env)

    params = params if params is not None else addon_params()
    server_url = ""
    api_token = ""
    if params is not None:
        server_url = (params.GetString("ServerUrl", "") or "").strip()
        api_token = (params.GetString("ApiToken", "") or "").strip()
    if not server_url:
        return dict(base_env)

    base = server_url.rstrip("/")
    session_id = local_session_id(params)
    overlay = {
        "CAD_BRIDGE_MODE": "workbench",
        "CAD_BRIDGE_AUTOSTART": "1",
        "CAD_REMOTE_SESSION_ID": session_id,
        "CAD_BRIDGE_POLL_URL": "%s/api/freecad/sessions/%s/bridge/poll" % (base, session_id),
        "CAD_BRIDGE_HEARTBEAT_URL": "%s/api/freecad/sessions/%s/bridge/heartbeat" % (base, session_id),
        "CAD_BRIDGE_SAVE_URL": "%s/api/freecad/sessions/%s/save" % (base, session_id),
        "CAD_CONTROL_PLANE_URL": base,
        # Remote mode polls the cloud over the internet on the GUI thread, so a
        # tight interval visibly stutters the UI (kiosk mode talks to localhost
        # and stays at its 2s default). Poll less often here until the bridge
        # HTTP is moved off the GUI thread. An explicit env value still wins.
        "CAD_BRIDGE_POLL_INTERVAL_SECONDS": (
            base_env.get("CAD_BRIDGE_POLL_INTERVAL_SECONDS") or "10"
        ),
        # A panel prompt runs the full cloud agent loop (LLM + FreeCADCmd), which
        # takes tens of seconds. The kiosk image sets this to 300 in its env, but
        # the remote overlay must carry it too or "Send Prompt" reads-time-out at
        # the 10s default. An explicit env value still wins.
        "CAD_PANEL_ACTION_HTTP_TIMEOUT_SECONDS": (
            base_env.get("CAD_PANEL_ACTION_HTTP_TIMEOUT_SECONDS") or "300"
        ),
    }
    if api_token:
        overlay["CAD_API_TOKEN"] = api_token
    merged = dict(base_env)
    merged.update(overlay)
    return merged


def auth_headers(env: dict[str, str]) -> dict[str, str]:
    token = ((env or {}).get("CAD_API_TOKEN") or "").strip()
    if not token:
        return {}
    return {"Authorization": "Bearer %s" % token}


def test_connection(server_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """GET {server_url}/healthz (no auth). Returns (ok, short message)."""
    url = "%s/healthz" % (server_url or "").rstrip("/")
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s: %s" % (exc.code, exc.reason)
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        return False, str(exc)
    if 200 <= status < 300:
        return True, "OK (HTTP %s)" % status
    return False, "HTTP %s" % status


def save_connection_params(server_url: str, api_token: str, params=None) -> None:
    """Persist ServerUrl (stripped) and, only if non-empty, ApiToken.

    An empty api_token must NOT overwrite an existing stored token -- this is
    what lets a user re-save just the ServerUrl without re-entering (or
    accidentally clearing) a previously-configured token.
    """
    params = params if params is not None else addon_params()
    if params is None:
        return
    params.SetString("ServerUrl", (server_url or "").strip())
    token = (api_token or "").strip()
    if token:
        params.SetString("ApiToken", token)


# Computed once at import time: in container/kiosk mode (CAD_BRIDGE_POLL_URL
# already set) this is exactly os.environ, unchanged. In remote (user-machine)
# mode with a configured ServerUrl param, it carries the synthesized bridge
# URLs + bearer token. All URL/session/token-derivation call sites below read
# from this instead of os.environ directly.
EFFECTIVE_ENV: dict[str, str] = remote_overlay_env()


def append_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    RECENT_EVENTS.append(
        {
            "type": event_type,
            "payload": payload or {},
            "at": utc_now(),
        }
    )
    del RECENT_EVENTS[:-80]


def app_console(level: str, message: str) -> None:
    prefix = "[4yi CAD] "
    if App is None:
        print(prefix + message)
        return
    console = getattr(App, "Console", None)
    if console is None:
        print(prefix + message)
        return
    text = prefix + message + "\n"
    if level == "error" and hasattr(console, "PrintError"):
        console.PrintError(text)
    elif level == "warning" and hasattr(console, "PrintWarning"):
        console.PrintWarning(text)
    elif hasattr(console, "PrintMessage"):
        console.PrintMessage(text)
    else:
        print(text)


# --- Main-thread task pump -------------------------------------------------
# FreeCAD's document/Qt objects are not thread-safe, so any work that touches
# them must run on the GUI (main) thread. Background workers enqueue callables
# here; a QTimer on the main thread drains and runs them. This lets slow HTTP
# (panel prompts, generation) run off the GUI thread without freezing the UI,
# while the results are applied back on the main thread.
_MAIN_THREAD_TASKS: "queue.Queue" = queue.Queue()
_MAIN_THREAD_PUMP = None


def post_to_main_thread(fn) -> None:
    """Queue a zero-arg callable to run on the GUI (main) thread."""
    _MAIN_THREAD_TASKS.put(fn)


def drain_main_thread_tasks() -> int:
    """Run all queued main-thread tasks. Returns how many ran. Safe to call on
    the main thread only (that is where the pump QTimer invokes it)."""
    ran = 0
    while True:
        try:
            fn = _MAIN_THREAD_TASKS.get_nowait()
        except queue.Empty:
            break
        ran += 1
        try:
            fn()
        except Exception as exc:
            app_console("warning", "main-thread task failed: %s" % exc)
    return ran


def ensure_main_thread_pump() -> None:
    global _MAIN_THREAD_PUMP
    if QtCore is None or _MAIN_THREAD_PUMP is not None:
        return
    _MAIN_THREAD_PUMP = QtCore.QTimer()
    _MAIN_THREAD_PUMP.setInterval(150)
    _MAIN_THREAD_PUMP.timeout.connect(drain_main_thread_tasks)
    _MAIN_THREAD_PUMP.start()


def run_in_background(work, on_done) -> None:
    """Run blocking `work()` off the GUI thread; deliver its result to
    `on_done(result, error)` back on the main thread via the pump. Exactly one
    of (result, error) is set. Falls back to synchronous when Qt is absent."""
    if QtCore is None:
        try:
            on_done(work(), None)
        except Exception as exc:
            on_done(None, exc)
        return
    ensure_main_thread_pump()

    def _runner() -> None:
        try:
            res = work()
            post_to_main_thread(lambda: on_done(res, None))
        except Exception as exc:
            post_to_main_thread(lambda: on_done(None, exc))

    threading.Thread(target=_runner, daemon=True).start()


def active_document():
    return getattr(App, "ActiveDocument", None) if App is not None else None


def freecad_version() -> str:
    if App is None:
        return "unavailable"
    try:
        version = App.Version()
        if isinstance(version, (list, tuple)):
            return ".".join(str(part) for part in version[:3])
        return str(version)
    except Exception:
        return "unknown"


def active_workbench_name() -> str | None:
    if Gui is None:
        return None
    try:
        workbench = Gui.activeWorkbench()
    except Exception:
        return None
    if workbench is None:
        return None
    for attr in ("name", "Name"):
        value = getattr(workbench, attr, None)
        if callable(value):
            try:
                return str(value())
            except Exception:
                pass
        elif value:
            return str(value)
    return workbench.__class__.__name__


def object_name(obj) -> str:
    return str(getattr(obj, "Name", "") or getattr(obj, "Label", "") or "")


def serializable_value(value) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "Value"):
        payload = {"value": getattr(value, "Value", None)}
        unit = getattr(value, "Unit", None)
        if unit is not None:
            payload["unit"] = str(unit)
        return payload
    if hasattr(value, "Base") and hasattr(value, "Rotation"):
        base = getattr(value, "Base", None)
        rotation = getattr(value, "Rotation", None)
        axis = getattr(rotation, "Axis", None)
        return {
            "base": vector_value(base),
            "axis": vector_value(axis),
            "angle_degrees": getattr(rotation, "Angle", 0) * 180 / 3.141592653589793,
        }
    if isinstance(value, (list, tuple)):
        return [serializable_value(item) for item in value[:50]]
    text = str(value)
    return text if len(text) <= 500 else text[:500] + "..."


def vector_value(value) -> list[float] | None:
    if value is None:
        return None
    try:
        return [float(value.x), float(value.y), float(value.z)]
    except Exception:
        return None


def object_summary(obj) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for prop in (
        "Length",
        "Width",
        "Height",
        "Radius",
        "Diameter",
        "HoleDiameter",
        "Placement",
        "Visibility",
    ):
        if hasattr(obj, prop):
            try:
                props[prop] = serializable_value(getattr(obj, prop))
            except Exception as exc:
                props[prop] = {"error": str(exc)}
    return {
        "name": object_name(obj),
        "label": str(getattr(obj, "Label", "") or object_name(obj)),
        "type_id": str(getattr(obj, "TypeId", "") or obj.__class__.__name__),
        "visibility": bool(getattr(obj, "Visibility", True)),
        "parents": [object_name(item) for item in list(getattr(obj, "InList", []) or [])[:40]],
        "children": [object_name(item) for item in list(getattr(obj, "OutList", []) or [])[:80]],
        "properties": props,
    }


def document_tree_from_document(doc) -> dict[str, Any]:
    if doc is None:
        return {
            "schema": "4yi.freecad.bridge.document_tree.v2",
            "document": None,
            "objects": [],
            "source": "freecad_addon",
        }
    objects = list(getattr(doc, "Objects", []) or [])
    return {
        "schema": "4yi.freecad.bridge.document_tree.v2",
        "document": {
            "name": str(getattr(doc, "Name", "") or ""),
            "label": str(getattr(doc, "Label", "") or getattr(doc, "Name", "") or ""),
            "file_name": str(getattr(doc, "FileName", "") or ""),
        },
        "objects": [object_summary(obj) for obj in objects[:500]],
        "object_count": len(objects),
        "source": "freecad_addon",
    }


def current_document_tree() -> dict[str, Any]:
    return document_tree_from_document(active_document())


def selection_from_gui(gui_module=None) -> dict[str, Any]:
    gui_module = gui_module if gui_module is not None else Gui
    selected: list[dict[str, Any]] = []
    if gui_module is not None:
        try:
            selection_ex = gui_module.Selection.getSelectionEx()
        except Exception:
            selection_ex = []
        for item in selection_ex or []:
            obj = getattr(item, "Object", None)
            references = list(getattr(item, "SubElementNames", []) or [])
            selected.append(
                {
                    "name": object_name(obj),
                    "label": str(getattr(obj, "Label", "") or object_name(obj)),
                    "type_id": str(getattr(obj, "TypeId", "") or ""),
                    "references": references,
                    "reference": references[0] if references else None,
                }
            )
    active = selected[0] if selected else None
    return {
        "schema": "4yi.freecad.bridge.selection.v2",
        "objects": selected,
        "active_object": active,
        "source": "freecad_addon",
        "updated_at": utc_now(),
    }


def current_selection() -> dict[str, Any]:
    return selection_from_gui(Gui)


def bridge_id(env: dict[str, str]) -> str:
    explicit = (env.get("CAD_BRIDGE_ID") or "").strip()
    if explicit:
        return explicit
    session_id = (env.get("CAD_REMOTE_SESSION_ID") or env.get("CAD_SESSION_ID") or "unknown").strip()
    return f"4yi-freecad-addon-{session_id}"


def build_bridge_payload(env: dict[str, str], *, event: str = "heartbeat") -> dict[str, Any]:
    doc = active_document()
    return {
        "bridge_id": bridge_id(env),
        "freecad_version": freecad_version(),
        "document_name": str(getattr(doc, "Name", "") or "") if doc else None,
        "active_document_path": str(getattr(doc, "FileName", "") or "") if doc else None,
        "current_version_id": env.get("CAD_CURRENT_VERSION_ID") or None,
        "workbench": active_workbench_name(),
        "selection": current_selection(),
        "document_tree": current_document_tree(),
        "console_tail": [
            "%s %s" % (item["at"], item["type"])
            for item in RECENT_EVENTS[-20:]
        ],
        "capabilities": [
            *SUPPORTED_COMMANDS,
            "panel_prompt",
            "panel_explain_object",
            "panel_generate_patch",
            "panel_accept_patch",
            "panel_reject_patch",
            "support_bundle",
        ],
        "metadata": {
            "event": event,
            "client": "freecad-addon",
            "client_schema": "4yi.freecad.bridge.addon.v1",
            "addon_version": ADDON_VERSION,
            "workspace": str(workspace(env)),
            "project_id": env.get("CAD_PROJECT_ID") or None,
            "workbench_session_id": env.get("CAD_WORKBENCH_SESSION_ID") or None,
        },
    }


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    headers.update(auth_headers(env or {}))
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s from %s: %s" % (exc.code, url, body)) from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def command_input(command: dict[str, Any]) -> dict[str, Any]:
    payload = command.get("input") or {}
    return payload if isinstance(payload, dict) else {}


def command_result_url(env: dict[str, str], command_id: str) -> str:
    explicit = (env.get("CAD_BRIDGE_COMMAND_RESULT_URL_BASE") or "").strip()
    if explicit:
        return "%s/%s/result" % (explicit.rstrip("/"), command_id)
    poll_url = (env.get("CAD_BRIDGE_POLL_URL") or "").strip()
    if poll_url.endswith("/bridge/poll"):
        return "%s/commands/%s/result" % (poll_url[: -len("/poll")], command_id)
    raise RuntimeError("CAD_BRIDGE_COMMAND_RESULT_URL_BASE is required")


def command_queue_url(env: dict[str, str]) -> str:
    explicit = (env.get("CAD_BRIDGE_COMMAND_QUEUE_URL") or "").strip()
    if explicit:
        return explicit
    poll_url = (env.get("CAD_BRIDGE_POLL_URL") or "").strip()
    if poll_url.endswith("/bridge/poll"):
        return "%s/commands" % poll_url[: -len("/bridge/poll")]
    raise RuntimeError("CAD_BRIDGE_COMMAND_QUEUE_URL is required")


def panel_action_url(env: dict[str, str]) -> str:
    explicit = (env.get("CAD_PANEL_ACTION_URL") or "").strip()
    if explicit:
        return explicit
    poll_url = (env.get("CAD_BRIDGE_POLL_URL") or "").strip()
    if poll_url.endswith("/bridge/poll"):
        return "%s/panel/actions" % poll_url[: -len("/bridge/poll")]
    raise RuntimeError("CAD_PANEL_ACTION_URL is required")


def panel_action_timeout(env: dict[str, str]) -> float:
    return env_float(
        env,
        "CAD_PANEL_ACTION_HTTP_TIMEOUT_SECONDS",
        env_float(env, "CAD_BRIDGE_HTTP_TIMEOUT_SECONDS", 10.0),
    )


def write_command_journal(
    command: dict[str, Any],
    payload: dict[str, Any],
    *,
    env: dict[str, str],
) -> dict[str, Any]:
    root = workspace(env)
    root.mkdir(parents=True, exist_ok=True)
    command_id = str(command.get("command_id") or command.get("id") or uuid.uuid4().hex)
    macro = str(payload.get("macro") or payload.get("script") or "")
    macro_path = root / ("bridge-macro-%s.py" % command_id)
    macro_path.write_text(macro, encoding="utf-8")
    journal_path = root / "bridge-command-journal.jsonl"
    entry = {
        "schema": "4yi.freecad.bridge.command_journal.v1",
        "command_id": command_id,
        "op": command.get("op"),
        "instruction": payload.get("instruction") or payload.get("prompt"),
        "macro_path": str(macro_path),
        "created_at": utc_now(),
    }
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "macro": {"kind": "workspace_file", "path": str(macro_path)},
        "command_journal": {"kind": "workspace_file", "path": str(journal_path)},
    }


def safe_workspace_filename(value: str | None, default: str) -> str:
    raw = (value or "").strip() or default
    name = Path(raw).name
    safe = "".join(ch for ch in name if ch.isalnum() or ch in {"-", "_", "."})
    safe = safe or default
    if not safe.lower().endswith(".fcstd"):
        safe = "%s.FCStd" % safe
    return safe


def resolve_control_plane_url(path_or_url: str, env: dict[str, str]) -> str:
    value = path_or_url.strip()
    if not value:
        raise BridgeCommandError("fcstd_source_required", "load_model requires fcstd_url or fcstd_b64")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value
    base = (
        env.get("CAD_CONTROL_PLANE_URL")
        or env.get("CAD_GUI_SESSION_CONTROL_PLANE_URL")
        or ""
    ).strip()
    if not base:
        raise BridgeCommandError(
            "control_plane_url_required",
            "relative fcstd_url requires CAD_CONTROL_PLANE_URL",
            details={"fcstd_url": value},
        )
    if value.startswith("/"):
        return urllib.parse.urljoin(base.rstrip("/") + "/", value.lstrip("/"))
    return urllib.parse.urljoin(base.rstrip("/") + "/", value)


def _url_is_control_plane(url: str, env: dict[str, str]) -> bool:
    """True iff `url`'s host matches the configured control-plane host.

    The Bearer token is only for our control plane. A load_model command may
    carry an already-absolute artifact URL (e.g. a presigned S3/CDN link);
    attaching the token to such a third-party host would leak it into that
    host's access logs. Only attach auth when the resolved host is ours.
    """
    base = (
        env.get("CAD_CONTROL_PLANE_URL")
        or env.get("CAD_GUI_SESSION_CONTROL_PLANE_URL")
        or ""
    ).strip()
    if not base:
        return False
    try:
        base_host = urllib.parse.urlparse(base).netloc.lower()
        target_host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return False
    return bool(base_host) and base_host == target_host


def load_model_bytes(payload: dict[str, Any], env: dict[str, str], timeout: float) -> bytes:
    fcstd_b64 = payload.get("fcstd_b64")
    if fcstd_b64:
        try:
            return base64.b64decode(str(fcstd_b64), validate=True)
        except Exception as exc:
            raise BridgeCommandError("invalid_fcstd_b64", "load_model fcstd_b64 is invalid") from exc

    fcstd_url = payload.get("fcstd_url") or payload.get("artifact_url") or payload.get("url")
    if not fcstd_url:
        raise BridgeCommandError("fcstd_source_required", "load_model requires fcstd_url or fcstd_b64")
    url = resolve_control_plane_url(str(fcstd_url), env)
    headers = {"Accept": "application/vnd.freecad,application/octet-stream"}
    if _url_is_control_plane(url, env):
        headers.update(auth_headers(env))
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BridgeCommandError(
            "fcstd_download_failed",
            "load_model could not download FCStd artifact",
            details={"status": exc.code, "body": body[:1000], "url": url},
        ) from exc
    if not data:
        raise BridgeCommandError("empty_fcstd", "load_model received an empty FCStd artifact")
    return data


def close_existing_documents() -> None:
    if App is None or not hasattr(App, "closeDocument"):
        return
    try:
        docs = App.listDocuments() if hasattr(App, "listDocuments") else {}
        names = list(docs.keys()) if isinstance(docs, dict) else []
    except Exception:
        names = []
    for name in names:
        try:
            App.closeDocument(str(name))
        except Exception:
            pass


def fit_active_view() -> None:
    if Gui is None:
        return
    try:
        view = Gui.ActiveDocument.ActiveView
        if hasattr(view, "fitAll"):
            view.fitAll()
    except Exception:
        pass


def execute_load_model(payload: dict[str, Any], env: dict[str, str], timeout: float) -> dict[str, Any]:
    if App is None:
        raise BridgeCommandError("freecad_unavailable", "FreeCAD is not available")
    root = workspace(env)
    root.mkdir(parents=True, exist_ok=True)
    filename = safe_workspace_filename(
        payload.get("filename") or payload.get("name"),
        "current-session.FCStd",
    )
    path = root / filename
    path.write_bytes(load_model_bytes(payload, env, timeout))
    if payload.get("close_existing", True) is not False:
        close_existing_documents()

    before_names = document_object_names(active_document())
    doc = None
    if hasattr(App, "openDocument"):
        doc = App.openDocument(str(path))
    doc = doc or active_document()
    if doc is not None and hasattr(App, "setActiveDocument"):
        try:
            App.setActiveDocument(str(getattr(doc, "Name", "")))
        except Exception:
            pass
    recompute = {"status": "not_run"}
    if doc is not None and payload.get("recompute", True) is not False and hasattr(doc, "recompute"):
        doc.recompute()
        recompute = {"status": "ok"}
    env["SESSION_FCSTD_PATH"] = str(path)
    if payload.get("version_id"):
        env["CAD_CURRENT_VERSION_ID"] = str(payload["version_id"])
    fit_active_view()
    after_names = document_object_names(doc)
    append_event("model_loaded", {"path": str(path), "version_id": payload.get("version_id")})
    return {
        "document_tree": document_tree_from_document(doc),
        "selection": current_selection(),
        "active_document_path": str(path),
        "loaded_model": {
            "path": str(path),
            "filename": path.name,
            "version_id": payload.get("version_id"),
        },
        "changed_objects": sorted(before_names ^ after_names),
        "console": ["Loaded %s" % path.name],
        "recompute_status": recompute,
        "undo": {"available": document_undo_available(doc), "source": "freecad_addon"},
    }


def document_object_names(doc) -> set[str]:
    if doc is None:
        return set()
    return {object_name(obj) for obj in list(getattr(doc, "Objects", []) or []) if object_name(obj)}


def find_document_object(doc, name: str):
    if doc is None:
        return None
    if hasattr(doc, "getObject"):
        try:
            target = doc.getObject(name)
            if target is not None:
                return target
        except Exception:
            pass
    for obj in list(getattr(doc, "Objects", []) or []):
        if object_name(obj) == name or str(getattr(obj, "Label", "") or "") == name:
            return obj
    return None


def execute_select_object(payload: dict[str, Any]) -> dict[str, Any]:
    if Gui is None:
        raise BridgeCommandError("freecad_gui_unavailable", "FreeCADGui is not available")
    doc = active_document()
    if doc is None:
        raise BridgeCommandError("active_document_required", "No active FreeCAD document")
    selector = payload.get("selector") if isinstance(payload.get("selector"), dict) else {}
    object_ref = (
        payload.get("object_name")
        or payload.get("name")
        or selector.get("object_name")
        or selector.get("name")
        or selector.get("label")
    )
    if not object_ref:
        raise BridgeCommandError(
            "selection_target_required",
            "select_object requires object_name, name, or selector.name",
        )
    target = find_document_object(doc, str(object_ref))
    if target is None:
        raise BridgeCommandError(
            "selection_target_not_found",
            "Selected object was not found: %s" % object_ref,
        )
    reference = payload.get("reference") or selector.get("reference")
    Gui.Selection.clearSelection()
    if reference:
        Gui.Selection.addSelection(doc.Name, target.Name, str(reference))
    else:
        Gui.Selection.addSelection(doc.Name, target.Name)
    return {
        "selection": current_selection(),
        "changed_objects": [target.Name],
        "console": ["Selected %s" % target.Name],
        "recompute_status": {"status": "not_run"},
        "undo": {"available": document_undo_available(doc), "source": "freecad_addon"},
    }


def document_undo_available(doc) -> bool:
    if doc is None:
        return False
    try:
        return bool(getattr(doc, "UndoMode", False))
    except Exception:
        return False


def execute_run_macro(
    command: dict[str, Any],
    payload: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    if App is None:
        raise BridgeCommandError("freecad_unavailable", "FreeCAD is not available")
    macro = str(payload.get("macro") or payload.get("script") or "")
    if not macro.strip():
        raise BridgeCommandError("macro_required", "run_macro requires macro or script")
    artifact_refs = write_command_journal(command, payload, env=env)
    if not truthy(env.get("CAD_BRIDGE_ALLOW_MACRO_EXEC")):
        raise BridgeCommandError(
            "macro_execution_disabled",
            "run_macro is disabled; set CAD_BRIDGE_ALLOW_MACRO_EXEC=1 for remote bridge sessions",
            details={"artifact_refs": artifact_refs},
        )

    before_doc = active_document()
    before_names = document_object_names(before_doc)
    stdout = io.StringIO()
    stderr = io.StringIO()
    transaction_open = False
    transaction_doc = before_doc
    try:
        if transaction_doc is not None and hasattr(transaction_doc, "openTransaction"):
            transaction_doc.openTransaction("4yi bridge %s" % (command.get("command_id") or command.get("id")))
            transaction_open = True
        namespace = {
            "__name__": "__4yi_bridge_macro__",
            "App": App,
            "FreeCAD": App,
            "Gui": Gui,
            "FreeCADGui": Gui,
        }
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(macro, "<4yi-bridge-macro>", "exec"), namespace, namespace)
        after_doc = active_document()
        recompute = {"status": "not_run"}
        if after_doc is not None and payload.get("recompute", True) is not False:
            after_doc.recompute()
            recompute = {"status": "ok"}
        if transaction_open and transaction_doc is not None and hasattr(transaction_doc, "commitTransaction"):
            transaction_doc.commitTransaction()
        after_names = document_object_names(after_doc)
        changed = sorted((before_names ^ after_names) or document_object_names(after_doc) & selected_object_names())
        return {
            "document_tree": current_document_tree(),
            "selection": current_selection(),
            "changed_objects": changed,
            "console": [line for line in (stdout.getvalue() + stderr.getvalue()).splitlines() if line],
            "recompute_status": recompute,
            "undo": {"available": document_undo_available(after_doc), "source": "freecad_addon"},
            "artifact_refs": artifact_refs,
        }
    except Exception as exc:
        if transaction_open and transaction_doc is not None and hasattr(transaction_doc, "abortTransaction"):
            try:
                transaction_doc.abortTransaction()
            except Exception:
                pass
        raise BridgeCommandError(
            "macro_execution_failed",
            str(exc),
            details={
                "traceback": traceback.format_exc(limit=20),
                "artifact_refs": artifact_refs,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
            },
        ) from exc


def selected_object_names() -> set[str]:
    selection = current_selection()
    return {
        str(item.get("name"))
        for item in selection.get("objects", [])
        if isinstance(item, dict) and item.get("name")
    }


def execute_save_revision(
    payload: dict[str, Any],
    env: dict[str, str],
    http_post: JsonPost,
    timeout: float,
) -> dict[str, Any]:
    save_url = (env.get("CAD_BRIDGE_SAVE_URL") or "").strip()
    if not save_url:
        raise BridgeCommandError("save_url_not_configured", "CAD_BRIDGE_SAVE_URL is required")
    root = workspace(env)
    root.mkdir(parents=True, exist_ok=True)
    target_path = Path(str(payload.get("fcstd_path") or root / "output.FCStd"))
    doc = active_document()
    if doc is not None:
        if hasattr(doc, "saveCopy"):
            doc.saveCopy(str(target_path))
        elif hasattr(doc, "saveAs"):
            doc.saveAs(str(target_path))
    if not target_path.exists():
        raise BridgeCommandError(
            "fcstd_not_found",
            "save_revision could not create or find an FCStd file",
            details={"path": str(target_path)},
        )
    result = http_post(
        save_url,
        {
            "message": payload.get("message") or "Saved from 4yi FreeCAD panel",
            "fcstd_b64": base64.b64encode(target_path.read_bytes()).decode("ascii"),
            "base_version_id": payload.get("base_version_id") or env.get("CAD_CURRENT_VERSION_ID") or None,
            "preview_png_b64": payload.get("preview_png_b64"),
            "artifacts": payload.get("artifacts") or {},
            "include_derivatives": bool(payload.get("include_derivatives", True)),
        },
        timeout,
    )
    version = result.get("version") or {}
    if version.get("id"):
        env["CAD_CURRENT_VERSION_ID"] = version["id"]
    return {
        "save": result,
        "artifact_refs": result.get("artifact_refs") or {},
        "changed_objects": [],
        "console": ["Saved %s" % target_path.name],
        "recompute_status": {"status": "not_run"},
        "undo": {"available": document_undo_available(doc), "source": "freecad_addon"},
    }


def execute_capture_screenshot(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    root = workspace(env)
    root.mkdir(parents=True, exist_ok=True)
    path = Path(str(payload.get("path") or root / "screenshot.png"))
    if Gui is not None:
        try:
            view = Gui.ActiveDocument.ActiveView
            width = int(payload.get("width") or 1600)
            height = int(payload.get("height") or 1000)
            view.saveImage(str(path), width, height, "Current")
        except Exception:
            if not path.exists():
                raise
    if not path.exists():
        raise BridgeCommandError(
            "screenshot_not_available",
            "capture_screenshot could not capture a GUI screenshot",
            details={"path": str(path)},
        )
    return {
        "screenshot_png_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
        "artifact_refs": {"screenshot_png": {"kind": "workspace_file", "path": str(path)}},
        "changed_objects": [],
        "console": ["Captured screenshot %s" % path.name],
        "recompute_status": {"status": "not_run"},
        "undo": {"available": False, "source": "freecad_addon"},
    }


def execute_command(
    command: dict[str, Any],
    env: dict[str, str],
    http_post: JsonPost = post_json,
    timeout: float = 10.0,
) -> dict[str, Any]:
    started_at = utc_now()
    op = command.get("op")
    payload = command_input(command)
    try:
        if op == "inspect_document":
            result = {
                "document_tree": current_document_tree(),
                "selection": current_selection(),
                "changed_objects": [],
                "console": [],
                "recompute_status": {"status": "not_run"},
                "undo": {"available": document_undo_available(active_document()), "source": "freecad_addon"},
            }
        elif op == "load_model":
            result = execute_load_model(payload, env, timeout)
        elif op == "select_object":
            result = execute_select_object(payload)
        elif op == "run_macro":
            result = execute_run_macro(command, payload, env)
        elif op == "save_revision":
            result = execute_save_revision(payload, env, http_post, timeout)
        elif op == "capture_screenshot":
            result = execute_capture_screenshot(payload, env)
        else:
            raise BridgeCommandError(
                "unsupported_command",
                "unsupported bridge command op: %s" % op,
                details={"supported_commands": SUPPORTED_COMMANDS},
            )
        return build_result_payload(command, env, "completed", result, None, started_at)
    except BridgeCommandError as exc:
        return build_result_payload(
            command,
            env,
            "failed",
            {
                "error": exc.to_dict(),
                "changed_objects": [],
                "console": [],
                "recompute_status": {"status": "not_run", "error": exc.code},
                "undo": {"available": False, "source": "freecad_addon"},
                "artifact_refs": exc.details.get("artifact_refs") if isinstance(exc.details, dict) else {},
            },
            exc.message,
            started_at,
        )
    except Exception as exc:
        return build_result_payload(
            command,
            env,
            "failed",
            {
                "error": {
                    "code": "freecad_addon_exception",
                    "message": str(exc),
                    "details": {"traceback": traceback.format_exc(limit=20)},
                },
                "changed_objects": [],
                "console": [],
                "recompute_status": {"status": "not_run", "error": "freecad_addon_exception"},
                "undo": {"available": False, "source": "freecad_addon"},
            },
            str(exc),
            started_at,
        )


def build_result_payload(
    command: dict[str, Any],
    env: dict[str, str],
    status: str,
    result: dict[str, Any],
    error: str | None,
    started_at: str,
) -> dict[str, Any]:
    transaction = {
        "id": "txn_%s" % uuid.uuid4().hex,
        "command_id": command.get("command_id") or command.get("id"),
        "op": command.get("op"),
        "started_at": started_at,
        "completed_at": utc_now(),
        "ok": status == "completed",
        "undo_available": bool((result.get("undo") or {}).get("available")),
        "recompute_status": result.get("recompute_status") or {"status": "not_run"},
    }
    merged = {
        "schema": "4yi.freecad.bridge.command_result.v2",
        "transaction": transaction,
        "changed_objects": result.get("changed_objects") or [],
        "console": result.get("console") or [],
        "recompute_status": transaction["recompute_status"],
        "undo": result.get("undo") or {"available": False, "source": "freecad_addon"},
        **result,
    }
    return {
        "status": status,
        "result": merged,
        "error": error,
        "current_version_id": env.get("CAD_CURRENT_VERSION_ID") or None,
        "metadata": {
            "bridge_id": bridge_id(env),
            "transaction_id": transaction["id"],
            "op": command.get("op"),
            "client": "freecad-addon",
        },
    }


class InProcessBridgeRuntime:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        http_post: JsonPost | None = None,
    ) -> None:
        self.env = env if env is not None else EFFECTIVE_ENV
        # The bridge endpoints (heartbeat/poll/command-result/save) are under
        # the server's guarded prefix, so in remote mode every call must carry
        # the Bearer token. The JsonPost seam is 3-arg (url, payload, timeout)
        # and the two loop test fixtures inject 3-arg fakes, so rather than
        # widen the alias we bind self.env into the default post_json here —
        # an injected http_post passes through unchanged, and in container/kiosk
        # mode env carries no CAD_API_TOKEN so this is a no-op (auth_headers
        # stays empty). Default is None (not post_json) so the wrapper resolves
        # the module-level post_json at call time, honoring monkeypatching.
        if http_post is None:
            self.http_post = lambda url, payload, timeout: post_json(
                url, payload, timeout, self.env
            )
        else:
            self.http_post = http_post
        self.timer = None
        self.running = False
        self.busy = False
        self.last_error = ""
        self.last_summary: dict[str, Any] = {}

    def start(self) -> None:
        if self.running:
            return
        if not (self.env.get("CAD_BRIDGE_POLL_URL") or "").strip():
            raise RuntimeError("CAD_BRIDGE_POLL_URL is required")
        self.running = True
        interval_ms = int(env_float(self.env, "CAD_BRIDGE_POLL_INTERVAL_SECONDS", 2.0) * 1000)
        if QtCore is not None:
            self.timer = QtCore.QTimer()
            self.timer.setInterval(interval_ms)
            self.timer.timeout.connect(self.tick)
            self.timer.start()
            QtCore.QTimer.singleShot(250, self.tick)
        else:
            self.tick()
        append_event("bridge_started", {"bridge_id": bridge_id(self.env)})
        app_console("message", "remote bridge started")

    def stop(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        self.running = False
        append_event("bridge_stopped", {"bridge_id": bridge_id(self.env)})
        app_console("message", "remote bridge stopped")

    def tick(self) -> None:
        if not self.running or self.busy:
            return
        self.busy = True
        try:
            self.last_summary = self.run_once()
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            append_event("bridge_error", {"error": self.last_error})
            app_console("warning", "remote bridge error: %s" % self.last_error)
        finally:
            self.busy = False

    def run_once(self) -> dict[str, Any]:
        timeout = env_float(self.env, "CAD_BRIDGE_HTTP_TIMEOUT_SECONDS", 5.0)
        heartbeat_url = (self.env.get("CAD_BRIDGE_HEARTBEAT_URL") or "").strip()
        poll_url = (self.env.get("CAD_BRIDGE_POLL_URL") or "").strip()
        if heartbeat_url:
            self.http_post(heartbeat_url, build_bridge_payload(self.env, event="heartbeat"), timeout)
        poll_response = self.http_post(
            poll_url,
            {
                **build_bridge_payload(self.env, event="poll"),
                "max_commands": env_int(self.env, "CAD_BRIDGE_MAX_COMMANDS", 10),
            },
            timeout,
        )
        commands_to_run = poll_response.get("commands") or []
        results = []
        for command in commands_to_run:
            command_id = command.get("command_id") or command.get("id")
            if not command_id:
                continue
            result_payload = execute_command(command, self.env, self.http_post, timeout)
            self.http_post(command_result_url(self.env, str(command_id)), result_payload, timeout)
            append_event(
                "bridge_command_result",
                {"command_id": command_id, "op": command.get("op"), "status": result_payload["status"]},
            )
            results.append({"command_id": command_id, "status": result_payload["status"]})
        return {
            "heartbeat": bool(heartbeat_url),
            "command_count": len(commands_to_run),
            "results": results,
            "at": utc_now(),
        }


def active_bridge_runtime() -> InProcessBridgeRuntime | None:
    return _BRIDGE_RUNTIME


def start_remote_bridge() -> InProcessBridgeRuntime:
    global _BRIDGE_RUNTIME
    if _BRIDGE_RUNTIME is None:
        _BRIDGE_RUNTIME = InProcessBridgeRuntime()
    _BRIDGE_RUNTIME.start()
    return _BRIDGE_RUNTIME


def stop_remote_bridge() -> None:
    if _BRIDGE_RUNTIME is not None:
        _BRIDGE_RUNTIME.stop()


def autostart_remote_bridge() -> None:
    mode = (EFFECTIVE_ENV.get("CAD_BRIDGE_MODE") or "").strip().lower()
    if mode not in {"freecad_addon", "addon", "in_process", "workbench"}:
        return
    if not truthy(EFFECTIVE_ENV.get("CAD_BRIDGE_AUTOSTART")):
        return
    if not (EFFECTIVE_ENV.get("CAD_BRIDGE_POLL_URL") or "").strip():
        return
    if QtCore is not None:
        QtCore.QTimer.singleShot(1500, start_remote_bridge)
    else:
        start_remote_bridge()


def autostart_companion_panel() -> None:
    # Local on/off switch only -- not derived from URL/session/token, so it
    # deliberately keeps reading the raw process environment.
    if not truthy(os.environ.get("CAD_COMPANION_PANEL_AUTOSTART")):
        return
    global _PANEL_AUTOSTARTED
    if _PANEL_AUTOSTARTED:
        return
    _PANEL_AUTOSTARTED = True

    def _open_panel() -> None:
        try:
            show_panel()
        except Exception as exc:
            if App is not None:
                App.Console.PrintError("4yi CAD panel autostart failed: %s\n" % exc)

    delay_ms = int(env_float(os.environ, "CAD_COMPANION_PANEL_DELAY_SECONDS", 2.5) * 1000)
    if QtCore is not None:
        QtCore.QTimer.singleShot(delay_ms, _open_panel)
    else:
        _open_panel()


def parse_measurement_value(text: str) -> float | None:
    import re

    matches = re.findall(r"(-?\d+(?:\.\d+)?)\s*(?:mm|毫米)?", text or "", flags=re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def macro_for_selected_numeric_edit(text: str, selection: dict[str, Any] | None = None) -> str:
    # `is None` (not `or`): a pre-gathered selection must never fall through to a
    # live GUI read, which would run off the main thread from a background action.
    if selection is None:
        selection = current_selection()
    active = selection.get("active_object") or {}
    object_name_value = active.get("name") or active.get("label") or ""
    value = parse_measurement_value(text)
    target = "None" if value is None else str(value)
    return "\n".join(
        [
            "import FreeCAD as App",
            "doc = App.ActiveDocument",
            "instruction = %s" % json.dumps(text or ""),
            "object_name = %s" % json.dumps(object_name_value),
            "target_mm = %s" % target,
            "if doc is None:",
            "    raise RuntimeError('No active FreeCAD document')",
            "if not object_name:",
            "    raise RuntimeError('No active FreeCAD selection')",
            "obj = doc.getObject(object_name)",
            "if obj is None:",
            "    raise RuntimeError('Selected object was not found: %s' % object_name)",
            "changed = []",
            "if target_mm is not None:",
            "    for prop in ('Diameter', 'HoleDiameter', 'Radius'):",
            "        if hasattr(obj, prop):",
            "            setattr(obj, prop, target_mm / 2.0 if prop == 'Radius' else target_mm)",
            "            changed.append('%s.%s' % (object_name, prop))",
            "            break",
            "if not changed:",
            "    raise RuntimeError('No editable diameter/radius property found on %s' % object_name)",
            "doc.recompute()",
            "print('4yi bridge changed %s' % ', '.join(changed))",
        ]
    )


def macro_for_prompt_if_selected_numeric_edit(
    text: str,
    selection: dict[str, Any] | None = None,
) -> str | None:
    # `is None` (not `or`): a pre-gathered selection must never fall through to a
    # live GUI read, which would run off the main thread from a background action.
    if selection is None:
        selection = current_selection()
    active = selection.get("active_object") or {}
    object_name_value = active.get("name") or active.get("label") or ""
    if not object_name_value or parse_measurement_value(text) is None:
        return None
    return macro_for_selected_numeric_edit(text, selection)


def submit_panel_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    env = EFFECTIVE_ENV
    # GUI/document reads must happen on the main thread. When the caller runs
    # this off the GUI thread (non-blocking panel actions) it pre-gathers these
    # and passes them in; only fall back to a live read for main-thread callers.
    selection = payload.get("selection")
    if selection is None:
        selection = current_selection()
    document_tree = payload.get("document_tree")
    if document_tree is None:
        document_tree = current_document_tree()
    return post_json(
        panel_action_url(env),
        {
            "action": action,
            "prompt": payload.get("prompt"),
            "selection": selection,
            "macro": payload.get("macro"),
            "patch_id": payload.get("patch_id"),
            "metadata": {
                "source": "freecad_panel",
                "addon_version": ADDON_VERSION,
                "document_tree": document_tree,
            },
        },
        panel_action_timeout(env),
        env,
    )


def queue_bridge_command(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = EFFECTIVE_ENV
    return post_json(
        command_queue_url(env),
        {
            "op": op,
            "input": payload,
            "base_version_id": env.get("CAD_CURRENT_VERSION_ID") or None,
        },
        env_float(env, "CAD_BRIDGE_HTTP_TIMEOUT_SECONDS", 10.0),
        env,
    )


def submit_prompt_from_panel(
    prompt: str,
    selection: dict[str, Any] | None = None,
    document_tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # selection/document_tree are pre-gathered on the main thread by the panel
    # so this whole function can run on a background thread (pure HTTP). Only
    # read live GUI state when called directly on the main thread.
    if selection is None:
        selection = current_selection()
    macro = macro_for_prompt_if_selected_numeric_edit(prompt, selection)
    payload = {
        "prompt": prompt,
        "selection": selection,
        "macro": macro,
        "document_tree": document_tree,
    }
    try:
        return submit_panel_action("prompt", payload)
    except Exception:
        if macro:
            return queue_bridge_command("run_macro", {"instruction": prompt, "selection": selection, "macro": macro})
        raise


def redacted_environment(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or EFFECTIVE_ENV
    keys = [
        "CAD_BRIDGE_MODE",
        "CAD_REMOTE_SESSION_ID",
        "CAD_WORKBENCH_SESSION_ID",
        "CAD_PROJECT_ID",
        "CAD_CURRENT_VERSION_ID",
        "CAD_CONTROL_PLANE_URL",
        "CAD_BRIDGE_HEARTBEAT_URL",
        "CAD_BRIDGE_POLL_URL",
        "CAD_BRIDGE_COMMAND_RESULT_URL_BASE",
        "CAD_BRIDGE_SAVE_URL",
        "CAD_PANEL_ACTION_URL",
        "CAD_PANEL_ACTION_HTTP_TIMEOUT_SECONDS",
    ]
    result = {}
    for key in keys:
        value = env.get(key)
        result[key] = bool(value) if key.endswith("_URL") else value
    return result


def collect_diagnostics(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or EFFECTIVE_ENV
    return {
        "schema": "4yi.freecad.support_bundle.v1",
        "created_at": utc_now(),
        "addon_version": ADDON_VERSION,
        "freecad_version": freecad_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "environment": redacted_environment(env),
        "bridge": {
            "running": bool(_BRIDGE_RUNTIME and _BRIDGE_RUNTIME.running),
            "last_error": _BRIDGE_RUNTIME.last_error if _BRIDGE_RUNTIME else "",
            "last_summary": _BRIDGE_RUNTIME.last_summary if _BRIDGE_RUNTIME else {},
        },
        "selection": current_selection(),
        "document_tree": current_document_tree(),
        "recent_events": list(RECENT_EVENTS[-80:]),
        "release_gate": {
            "freecad_1_1_x": freecad_version().startswith("1.1."),
            "matrix_targets": ["macOS", "Windows", "Linux"],
            "current_platform": platform.system(),
            "status": "local_diagnostics_only",
        },
    }


def export_support_bundle(env: dict[str, str] | None = None) -> Path:
    env = env or EFFECTIVE_ENV
    root = workspace(env)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("4yi-freecad-support-bundle-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    path.write_text(
        json.dumps(collect_diagnostics(env), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    append_event("support_bundle_exported", {"path": str(path)})
    return path


class CompanionTaskPanel:
    def __init__(self) -> None:
        if QtWidgets is None:
            raise RuntimeError("Qt widgets are not available")
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("4yi CAD")
        layout = QtWidgets.QVBoxLayout(self.form)
        self.status_label = QtWidgets.QLabel("")
        self.context_label = QtWidgets.QLabel("")
        self.context_label.setWordWrap(True)
        self.prompt_input = QtWidgets.QLineEdit()
        self.prompt_input.setPlaceholderText(t("输入指令 / 修改选中对象", "Prompt / modify selected object"))
        self.patch_id_input = QtWidgets.QLineEdit()
        self.patch_id_input.setPlaceholderText(t("补丁 ID", "Patch ID"))
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        buttons = QtWidgets.QGridLayout()
        self.refresh_button = QtWidgets.QPushButton(t("刷新", "Refresh"))
        self.start_button = QtWidgets.QPushButton(t("启动桥接", "Start Bridge"))
        self.stop_button = QtWidgets.QPushButton(t("停止桥接", "Stop Bridge"))
        self.explain_button = QtWidgets.QPushButton(t("解释对象", "Explain Object"))
        self.prompt_button = QtWidgets.QPushButton(t("发送指令", "Send Prompt"))
        self.generate_patch_button = QtWidgets.QPushButton(t("生成补丁", "Generate Patch"))
        self.accept_patch_button = QtWidgets.QPushButton(t("接受补丁", "Accept Patch"))
        self.reject_patch_button = QtWidgets.QPushButton(t("拒绝补丁", "Reject Patch"))
        self.bundle_button = QtWidgets.QPushButton(t("支持包", "Support Bundle"))
        for index, button in enumerate(
            [
                self.refresh_button,
                self.start_button,
                self.stop_button,
                self.explain_button,
                self.prompt_button,
                self.generate_patch_button,
                self.accept_patch_button,
                self.reject_patch_button,
                self.bundle_button,
            ]
        ):
            buttons.addWidget(button, index // 3, index % 3)
        layout.addWidget(self.status_label)
        layout.addWidget(self.context_label)
        layout.addWidget(self.prompt_input)
        layout.addWidget(self.patch_id_input)
        layout.addLayout(buttons)
        layout.addWidget(self.output)
        self.refresh_button.clicked.connect(self.refresh)
        self.start_button.clicked.connect(self.start_bridge)
        self.stop_button.clicked.connect(self.stop_bridge)
        self.explain_button.clicked.connect(self.explain_object)
        self.prompt_button.clicked.connect(self.send_prompt)
        self.generate_patch_button.clicked.connect(lambda: self.panel_action("generate_patch"))
        self.accept_patch_button.clicked.connect(lambda: self.panel_action("accept_patch"))
        self.reject_patch_button.clicked.connect(lambda: self.panel_action("reject_patch"))
        self.bundle_button.clicked.connect(self.export_bundle)
        self.refresh()

    def refresh(self) -> None:
        diagnostics = collect_diagnostics()
        active = diagnostics["selection"].get("active_object") or {}
        doc = diagnostics["document_tree"].get("document") or {}
        self.status_label.setText(
            t("项目 %s | 版本 %s | 桥接 %s", "Project %s | Revision %s | Bridge %s")
            % (
                EFFECTIVE_ENV.get("CAD_PROJECT_ID")
                or EFFECTIVE_ENV.get("CAD_WORKBENCH_SESSION_ID")
                or t("未配置", "not configured"),
                EFFECTIVE_ENV.get("CAD_CURRENT_VERSION_ID") or t("未配置", "not configured"),
                t("运行中", "running") if diagnostics["bridge"]["running"] else t("已停止", "stopped"),
            )
        )
        self.context_label.setText(
            t("文档 %s | 选择 %s", "Document %s | Selection %s")
            % (
                doc.get("name") or t("无", "none"),
                active.get("label") or active.get("name") or t("无", "none"),
            )
        )
        self.output.setPlainText(json.dumps(diagnostics, ensure_ascii=False, indent=2))

    def start_bridge(self) -> None:
        try:
            start_remote_bridge()
            self.refresh()
        except Exception as exc:
            self.output.setPlainText(str(exc))

    def stop_bridge(self) -> None:
        stop_remote_bridge()
        self.refresh()

    def explain_object(self) -> None:
        try:
            result = submit_panel_action("explain_object", {"prompt": self.prompt_input.text()})
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception:
            self.output.setPlainText(json.dumps(current_selection(), ensure_ascii=False, indent=2))

    def _run_action_async(self, work, pending_text: str) -> None:
        # A panel action drives the cloud agent loop (tens of seconds). `work`
        # must be PURE HTTP — all GUI/document reads are gathered on the main
        # thread by the caller and passed in as data. Run the HTTP off the GUI
        # thread so the UI stays responsive; show the result back on the main
        # thread. The generated model itself is loaded by the bridge poll's
        # load_model command. A busy guard prevents overlapping requests from
        # double-clicks (which would race and land out of order).
        if getattr(self, "_action_busy", False):
            return
        self._action_busy = True
        self.output.setPlainText(pending_text)

        def done(result, error) -> None:
            self._action_busy = False
            if error is not None:
                self.output.setPlainText(str(error))
            else:
                self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))

        run_in_background(work, done)

    def send_prompt(self) -> None:
        prompt = self.prompt_input.text()
        selection = current_selection()          # main thread
        document_tree = current_document_tree()  # main thread
        self._run_action_async(
            lambda: submit_prompt_from_panel(
                prompt, selection=selection, document_tree=document_tree
            ),
            t("已发送,云端生成中…(模型就绪后会自动载入)", "Sent — generating in the cloud… (the model loads when ready)"),
        )

    def panel_action(self, action: str) -> None:
        payload = {
            "prompt": self.prompt_input.text(),
            "patch_id": self.patch_id_input.text(),
            "selection": current_selection(),          # main thread
            "document_tree": current_document_tree(),   # main thread
        }
        self._run_action_async(
            lambda: submit_panel_action(action, payload),
            t("处理中…", "Working…"),
        )

    def export_bundle(self) -> None:
        path = export_support_bundle()
        self.output.setPlainText(t("支持包已写入 %s", "Support bundle written to %s") % path)

    def accept(self) -> bool:
        return True

    def reject(self) -> bool:
        return True


def show_panel() -> None:
    if QtWidgets is None:
        raise RuntimeError("Qt widgets are not available")
    global _PANEL_DIALOG
    panel = CompanionTaskPanel()
    _PANEL_DIALOG = panel
    # Floating window rather than the Task panel: Gui.Control.showDialog raises
    # "Active task dialog found" when another task dialog is up and no-ops on the
    # Start page. A top-level QWidget.show() is reliable in every context.
    panel.form.show()
    panel.form.raise_()
    panel.form.activateWindow()


class OpenPanelCommand:
    def GetResources(self):
        return {
            "MenuText": t("打开 4yi CAD 面板", "Open 4yi CAD Panel"),
            "ToolTip": t("打开 4yi CAD 助手面板。", "Open the 4yi CAD companion panel."),
        }

    def Activated(self):
        show_panel()

    def IsActive(self):
        return App is not None


class StartBridgeCommand:
    def GetResources(self):
        return {
            "MenuText": t("启动 4yi 桥接", "Start 4yi Bridge"),
            "ToolTip": t("启动 4yi 远程会话桥接。", "Start the 4yi remote-session bridge."),
        }

    def Activated(self):
        start_remote_bridge()

    def IsActive(self):
        return App is not None


class StopBridgeCommand:
    def GetResources(self):
        return {
            "MenuText": t("停止 4yi 桥接", "Stop 4yi Bridge"),
            "ToolTip": t("停止 4yi 远程会话桥接。", "Stop the 4yi remote-session bridge."),
        }

    def Activated(self):
        stop_remote_bridge()

    def IsActive(self):
        return _BRIDGE_RUNTIME is not None and _BRIDGE_RUNTIME.running


class ExportSupportBundleCommand:
    def GetResources(self):
        return {
            "MenuText": t("导出 4yi 支持包", "Export 4yi Support Bundle"),
            "ToolTip": t("导出 4yi CAD 助手的诊断信息。", "Write diagnostics for the 4yi CAD companion."),
        }

    def Activated(self):
        path = export_support_bundle()
        app_console("message", "support bundle written to %s" % path)

    def IsActive(self):
        return App is not None


class ConnectionSettingsDialog:
    """「4yi: 连接设置…」dialog: ServerUrl + ApiToken, test-connection, save.

    Pure Qt assembly around test_connection()/save_connection_params(); no
    logic lives here (both functions are unit-tested without Qt/FreeCAD).
    The API token is only ever passed to save_connection_params() -- it is
    never logged, appended to RECENT_EVENTS, or written into a support
    bundle.
    """

    def __init__(self) -> None:
        if QtWidgets is None:
            raise RuntimeError("Qt widgets are not available")
        params = addon_params()
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle(t("4yi CAD - 连接设置", "4yi CAD - Connection Settings"))
        layout = QtWidgets.QVBoxLayout(self.form)

        layout.addWidget(QtWidgets.QLabel(t("服务器地址", "Server URL")))
        self.server_url_input = QtWidgets.QLineEdit()
        self.server_url_input.setPlaceholderText("https://cad.example.com")
        if params is not None:
            self.server_url_input.setText(params.GetString("ServerUrl", "") or "")
        layout.addWidget(self.server_url_input)

        layout.addWidget(QtWidgets.QLabel(t("API Token", "API Token")))
        self.api_token_input = QtWidgets.QLineEdit()
        self.api_token_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.api_token_input.setPlaceholderText(t("留空则保留已保存的 Token", "Leave blank to keep the saved token"))
        layout.addWidget(self.api_token_input)

        buttons = QtWidgets.QHBoxLayout()
        self.test_button = QtWidgets.QPushButton(t("测试连接", "Test connection"))
        self.save_button = QtWidgets.QPushButton(t("保存", "Save"))
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.result_label = QtWidgets.QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.test_button.clicked.connect(self.on_test_connection)
        self.save_button.clicked.connect(self.on_save)

    def on_test_connection(self) -> None:
        server_url = self.server_url_input.text().strip()
        if not server_url:
            self.result_label.setText(t("请先填写 Server URL", "Please enter the Server URL first"))
            return
        ok, message = test_connection(server_url)
        self.result_label.setText(("✓ " + message) if ok else ("✗ " + message))

    def on_save(self) -> None:
        server_url = self.server_url_input.text()
        api_token = self.api_token_input.text()
        save_connection_params(server_url, api_token)
        self.api_token_input.clear()
        self.result_label.setText(t("已保存,重启 FreeCAD 生效", "Saved. Restart FreeCAD to apply."))

    def accept(self) -> bool:
        return True

    def reject(self) -> bool:
        return True


_CONNECTION_SETTINGS_DIALOG = None


def show_connection_settings() -> None:
    if QtWidgets is None:
        raise RuntimeError("Qt widgets are not available")
    global _CONNECTION_SETTINGS_DIALOG
    dialog = ConnectionSettingsDialog()
    _CONNECTION_SETTINGS_DIALOG = dialog
    # Show as a standalone floating window. Gui.Control.showDialog (the Task
    # panel) silently no-ops on the Start page / without an active document, so
    # the menu click appears to do nothing. A top-level QWidget.show() is
    # reliable in every context.
    dialog.form.show()
    dialog.form.raise_()
    dialog.form.activateWindow()


class ConnectionSettingsCommand:
    def GetResources(self):
        return {
            "MenuText": t("4yi: 连接设置...", "4yi: Connection Settings..."),
            "ToolTip": t(
                "配置 4yi CAD Server URL / API Token,并测试连接。",
                "Configure the 4yi CAD Server URL / API Token and test the connection.",
            ),
        }

    def Activated(self):
        show_connection_settings()

    def IsActive(self):
        return App is not None


def register_commands() -> None:
    global _COMMANDS_REGISTERED
    if _COMMANDS_REGISTERED or Gui is None:
        return
    mapping = {
        COMMAND_OPEN_PANEL: OpenPanelCommand(),
        COMMAND_START_BRIDGE: StartBridgeCommand(),
        COMMAND_STOP_BRIDGE: StopBridgeCommand(),
        COMMAND_EXPORT_SUPPORT_BUNDLE: ExportSupportBundleCommand(),
        COMMAND_CONNECTION_SETTINGS: ConnectionSettingsCommand(),
    }
    for name, command in mapping.items():
        try:
            Gui.addCommand(name, command)
        except Exception:
            pass
    _COMMANDS_REGISTERED = True
