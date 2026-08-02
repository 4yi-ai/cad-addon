from __future__ import annotations

try:
    import FreeCAD as App
except Exception:
    App = None

try:
    import FreeCADGui as Gui
except Exception:
    Gui = None


class FourYiCadCompanionWorkbench(Gui.Workbench):
    MenuText = "4yi CAD"
    ToolTip = "4yi CAD companion panel and remote-session bridge."
    Icon = ""

    def Initialize(self):
        import FourYiCadCompanion

        FourYiCadCompanion.register_commands()
        command_names = FourYiCadCompanion.commands()
        self.appendToolbar("4yi CAD", command_names)
        self.appendMenu("4yi CAD", command_names)
        FourYiCadCompanion.autostart_remote_bridge()
        FourYiCadCompanion.autostart_companion_panel()

    def GetClassName(self):
        return "Gui::PythonWorkbench"


if Gui is not None:
    Gui.addWorkbench(FourYiCadCompanionWorkbench())
    try:
        import FourYiCadCompanion

        FourYiCadCompanion.register_commands()
        FourYiCadCompanion.autostart_remote_bridge()
        FourYiCadCompanion.autostart_companion_panel()
    except Exception as exc:
        if App is not None:
            App.Console.PrintError("4yi CAD addon startup failed: %s\n" % exc)
