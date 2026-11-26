import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// CPU Limits service: the shell creates it once, however many monitors show
// the bar widget. It reads the status that cpu-frequency-limits.service
// publishes in /run for every widget and owns the plugin's IPC target.
// Each widget keeps its own panel, drafts and pkexec writer, reports its
// panel here, and binds to `parsed`.
Scope {
  id: root

  readonly property string pluginId: "io.github.ltecosta.cpu-limits"
  property var parsed: Model.unavailable("Reading CPU status…")
  // { instanceKey: { opened, selected, dirty } }, one entry per live widget.
  property var panels: ({})
  readonly property int openPanels: Model.openPanelCount(panels)
  // A re-enabled plugin briefly overlaps its old service; register the IPC
  // target only after the old one has released it.
  property bool ipcReady: false

  function refresh() { statusFile.reload() }

  function reportPanel(key, report) { panels = Model.withPanel(panels, String(key), report) }

  function forgetPanel(key) { panels = Model.withoutPanel(panels, String(key)) }

  // Panel IPC acts on the focused monitor's widget: the shell routes summon,
  // hide and toggle through the bar, which also prefers an open panel to hide.
  function shellPanel(action) { Quickshell.execDetached(["omarchy-shell", "shell", action, pluginId]) }

  function stateIpc() { return JSON.stringify(Model.stateReport(parsed, panels)) }

  Timer {
    interval: 150
    running: true
    onTriggered: root.ipcReady = true
  }

  ShellIpc {
    enabled: root.ipcReady
    target: root.pluginId
    function open(): void { root.shellPanel("summon") }
    function close(): void { root.shellPanel("hide") }
    function show(): void { root.shellPanel("summon") }
    function hide(): void { root.shellPanel("hide") }
    function toggle(): void { root.shellPanel("toggle") }
    function refresh(): void { root.refresh() }
    function state(): string { return root.stateIpc() }
  }

  FileView {
    id: statusFile
    path: "/run/workstation-cpu-limits/status.json"
    printErrors: false
    onLoaded: root.parsed = Model.parseStatus(text(), Date.now() / 1000)
    onLoadFailed: root.parsed = Model.parseStatus("", Date.now() / 1000)
  }

  // The service rewrites the status every two seconds. Follow it closely while
  // any monitor's panel is open; the bar icons only need a slower look.
  Timer {
    interval: root.openPanels > 0 ? 2000 : 10000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}
