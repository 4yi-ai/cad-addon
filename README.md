# 4yi CAD Companion

This FreeCAD addon provides the Phase 4 Workbench surface for 4yi-cad.

It has two modes:

- Desktop companion: users can select an object, describe a change in natural
  language, review a typed edit plan, preview safe dimensional changes directly
  in the 3D canvas, apply or cancel them, and undo the last applied transaction.
  Complex edits continue through the cloud revision generator and load back
  through the same bridge.
- Remote session bridge: GUI containers set `CAD_BRIDGE_MODE=freecad_addon`, so
  the addon autostarts an in-process bridge that reads real `FreeCADGui`
  selection/document state and executes bridge commands in FreeCAD transactions.

## Manual Install

The current source of truth is the
[`4yi-ai/4yi-cad`](https://github.com/4yi-ai/4yi-cad) repository. Do not install
from the old standalone `4yi-ai/cad-addon` repository: it is not automatically
synchronized with this repository.

1. In FreeCAD, open **View → Panels → Python console**, then run
   `print(App.getUserAppDataDir() + "Mod")`. The printed value is the
   authoritative add-on directory for the running FreeCAD version. FreeCAD
   1.1.3 on macOS was verified to use
   `~/Library/Application Support/FreeCAD/v1-1/Mod`.
2. On macOS, open **Finder → Go → Go to Folder…** and paste the printed path.
   **Tools → Addon Manager → gear → Open Addons Folder** is only an optional
   shortcut; some window layouts make that action easy to miss.
3. Download the
   [main-branch source ZIP](https://github.com/4yi-ai/4yi-cad/archive/refs/heads/main.zip)
   and extract it.
4. Completely quit FreeCAD. In the extracted archive, copy
   `4yi-cad-main/freecad-addon/fouryi_cad_companion` into the verified `Mod`
   folder. When upgrading, back up and replace the existing
   `fouryi_cad_companion` directory.
5. Restart FreeCAD and switch to the **4yi CAD** workbench.
6. Open **4yi Support Bundle** and verify that `addon_version` matches the
   version in [`package.xml`](package.xml) (currently `0.5.2`).

The folder name must remain exactly `fouryi_cad_companion`; avoid an extra
archive nesting level such as `fouryi_cad_companion/fouryi_cad_companion`.

## Remote Bridge Environment

The remote GUI Docker image configures these values automatically:

- `CAD_BRIDGE_MODE=freecad_addon`
- `CAD_BRIDGE_AUTOSTART=1`
- `CAD_BRIDGE_ALLOW_MACRO_EXEC=1` (legacy remote commands only; the natural-
  language panel never attaches executable Python to user prompts)
- `CAD_REMOTE_SESSION_ID`
- `CAD_WORKBENCH_SESSION_ID`
- `CAD_BRIDGE_HEARTBEAT_URL`
- `CAD_BRIDGE_POLL_URL`
- `CAD_BRIDGE_COMMAND_RESULT_URL_BASE`
- `CAD_BRIDGE_COMMAND_QUEUE_URL`
- `CAD_BRIDGE_SAVE_URL`
- `CAD_PANEL_ACTION_URL`

The standalone `freecad-bridge-client.py` remains available as a fallback when
`CAD_BRIDGE_MODE=standalone`.
