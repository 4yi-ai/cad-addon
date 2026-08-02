from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import platform
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


ADDON_VERSION = "0.4.0"
USER_AGENT = "4yi-freecad-companion/0.4.0"
COMMAND_OPEN_PANEL = "FourYi_OpenPanel"
COMMAND_START_BRIDGE = "FourYi_StartBridge"
COMMAND_STOP_BRIDGE = "FourYi_StopBridge"
COMMAND_EXPORT_SUPPORT_BUNDLE = "FourYi_ExportSupportBundle"
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


def post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
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
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.freecad,application/octet-stream"},
    )
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
        http_post: JsonPost = post_json,
    ) -> None:
        self.env = env if env is not None else os.environ
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
    mode = (os.environ.get("CAD_BRIDGE_MODE") or "").strip().lower()
    if mode not in {"freecad_addon", "addon", "in_process"}:
        return
    if not truthy(os.environ.get("CAD_BRIDGE_AUTOSTART")):
        return
    if not (os.environ.get("CAD_BRIDGE_POLL_URL") or "").strip():
        return
    if QtCore is not None:
        QtCore.QTimer.singleShot(1500, start_remote_bridge)
    else:
        start_remote_bridge()


def autostart_companion_panel() -> None:
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
    selection = selection or current_selection()
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


def submit_panel_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    env = os.environ
    return post_json(
        panel_action_url(env),
        {
            "action": action,
            "prompt": payload.get("prompt"),
            "selection": payload.get("selection") or current_selection(),
            "macro": payload.get("macro"),
            "patch_id": payload.get("patch_id"),
            "metadata": {
                "source": "freecad_panel",
                "addon_version": ADDON_VERSION,
                "document_tree": current_document_tree(),
            },
        },
        env_float(env, "CAD_BRIDGE_HTTP_TIMEOUT_SECONDS", 10.0),
    )


def queue_bridge_command(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = os.environ
    return post_json(
        command_queue_url(env),
        {
            "op": op,
            "input": payload,
            "base_version_id": env.get("CAD_CURRENT_VERSION_ID") or None,
        },
        env_float(env, "CAD_BRIDGE_HTTP_TIMEOUT_SECONDS", 10.0),
    )


def submit_prompt_from_panel(prompt: str) -> dict[str, Any]:
    selection = current_selection()
    macro = macro_for_selected_numeric_edit(prompt, selection)
    payload = {"prompt": prompt, "selection": selection, "macro": macro}
    try:
        return submit_panel_action("prompt", payload)
    except Exception:
        return queue_bridge_command("run_macro", {"instruction": prompt, "selection": selection, "macro": macro})


def redacted_environment(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
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
    ]
    result = {}
    for key in keys:
        value = env.get(key)
        result[key] = bool(value) if key.endswith("_URL") else value
    return result


def collect_diagnostics(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
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
    env = env or os.environ
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
        self.prompt_input.setPlaceholderText("Prompt / modify selected object")
        self.patch_id_input = QtWidgets.QLineEdit()
        self.patch_id_input.setPlaceholderText("Patch ID")
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        buttons = QtWidgets.QGridLayout()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.start_button = QtWidgets.QPushButton("Start Bridge")
        self.stop_button = QtWidgets.QPushButton("Stop Bridge")
        self.explain_button = QtWidgets.QPushButton("Explain Object")
        self.prompt_button = QtWidgets.QPushButton("Send Prompt")
        self.generate_patch_button = QtWidgets.QPushButton("Generate Patch")
        self.accept_patch_button = QtWidgets.QPushButton("Accept Patch")
        self.reject_patch_button = QtWidgets.QPushButton("Reject Patch")
        self.bundle_button = QtWidgets.QPushButton("Support Bundle")
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
            "Project %s | Revision %s | Bridge %s"
            % (
                os.environ.get("CAD_PROJECT_ID") or os.environ.get("CAD_WORKBENCH_SESSION_ID") or "not configured",
                os.environ.get("CAD_CURRENT_VERSION_ID") or "not configured",
                "running" if diagnostics["bridge"]["running"] else "stopped",
            )
        )
        self.context_label.setText(
            "Document %s | Selection %s"
            % (doc.get("name") or "none", active.get("label") or active.get("name") or "none")
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

    def send_prompt(self) -> None:
        try:
            result = submit_prompt_from_panel(self.prompt_input.text())
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.output.setPlainText(str(exc))

    def panel_action(self, action: str) -> None:
        try:
            result = submit_panel_action(
                action,
                {
                    "prompt": self.prompt_input.text(),
                    "patch_id": self.patch_id_input.text(),
                },
            )
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.output.setPlainText(str(exc))

    def export_bundle(self) -> None:
        path = export_support_bundle()
        self.output.setPlainText("Support bundle written to %s" % path)

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
    if Gui is not None and hasattr(Gui, "Control"):
        Gui.Control.showDialog(panel)
    else:
        panel.form.show()


class OpenPanelCommand:
    def GetResources(self):
        return {
            "MenuText": "Open 4yi CAD Panel",
            "ToolTip": "Open the 4yi CAD companion panel.",
        }

    def Activated(self):
        show_panel()

    def IsActive(self):
        return App is not None


class StartBridgeCommand:
    def GetResources(self):
        return {
            "MenuText": "Start 4yi Bridge",
            "ToolTip": "Start the 4yi remote-session bridge.",
        }

    def Activated(self):
        start_remote_bridge()

    def IsActive(self):
        return App is not None


class StopBridgeCommand:
    def GetResources(self):
        return {
            "MenuText": "Stop 4yi Bridge",
            "ToolTip": "Stop the 4yi remote-session bridge.",
        }

    def Activated(self):
        stop_remote_bridge()

    def IsActive(self):
        return _BRIDGE_RUNTIME is not None and _BRIDGE_RUNTIME.running


class ExportSupportBundleCommand:
    def GetResources(self):
        return {
            "MenuText": "Export 4yi Support Bundle",
            "ToolTip": "Write diagnostics for the 4yi CAD companion.",
        }

    def Activated(self):
        path = export_support_bundle()
        app_console("message", "support bundle written to %s" % path)

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
    }
    for name, command in mapping.items():
        try:
            Gui.addCommand(name, command)
        except Exception:
            pass
    _COMMANDS_REGISTERED = True
