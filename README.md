# 4yi CAD Companion

This FreeCAD addon provides the Phase 4 Workbench surface for 4yi-cad.

It has two modes:

- Desktop companion: users can open the 4yi CAD panel from FreeCAD, inspect the
  active document/selection, send panel actions to the platform when configured,
  and export a support bundle.
- Remote session bridge: GUI containers set `CAD_BRIDGE_MODE=freecad_addon`, so
  the addon autostarts an in-process bridge that reads real `FreeCADGui`
  selection/document state and executes bridge commands in FreeCAD transactions.

## Manual Install

Copy this directory to the FreeCAD user `Mod` directory:

```text
macOS: ~/Library/Application Support/FreeCAD/Mod/fouryi_cad_companion
Linux: ~/.local/share/FreeCAD/Mod/fouryi_cad_companion
Windows: %APPDATA%/FreeCAD/Mod/fouryi_cad_companion
```

Restart FreeCAD and switch to the `4yi CAD` workbench.

## Remote Bridge Environment

The remote GUI Docker image configures these values automatically:

- `CAD_BRIDGE_MODE=freecad_addon`
- `CAD_BRIDGE_AUTOSTART=1`
- `CAD_BRIDGE_ALLOW_MACRO_EXEC=1`
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
