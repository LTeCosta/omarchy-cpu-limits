import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Driver for service.smoke.sh: one Service and two bar widgets (two
// monitors). The left bar's shell hands out the service, as the built-in bar
// does; the right bar's shell answers null, as a replacement bar does, so that
// widget reaches the service through ServiceLink.js. A fake omarchy-shell and
// pkexec are first on PATH. Prints "SMOKE ok - ..." or "SMOKE not ok - ..."
// per assertion and "SMOKE done <failures>" at the end.
ShellRoot {
  id: root

  readonly property string pluginId: "io.github.ltecosta.cpu-limits"
  readonly property string fakeDir: Quickshell.env("FAKE_DIR")
  readonly property string statusPath: Quickshell.env("SMOKE_STATUS")
  readonly property string writeStatus: Quickshell.env("SMOKE_WRITE_STATUS")

  property int failures: 0
  property int step: -1
  property string stepName: ""
  property var waiting: null
  property var waitDeadline: 0
  property var service: null
  property var previous: null

  Component {
    id: serviceComponent
    Service { }
  }

  Component {
    id: barComponent
    QtObject {
      property var target: null
      property QtObject shell: QtObject {
        property var lookup: null
        function serviceFor(id) { return lookup && id === root.pluginId ? lookup.target : null }
      }
      property color foreground: "white"
      property color barForeground: "white"
      property color urgent: "red"
      property string fontFamily: "sans-serif"
      property bool vertical: false
      property int barSize: 30
      property bool foregroundAnimationEnabled: false
      function showTooltip(_item, _text) {}
      function hideTooltip(_item) {}
      function switchPanelFrom(_item, _direction) { return false }
      Component.onCompleted: shell.lookup = this
    }
  }

  Component {
    id: panelComponent
    Panel { }
  }

  property var leftBar: barComponent.createObject(root, { target: null })
  property var replacementBar: barComponent.createObject(root, { target: null })
  property var left: null
  property var right: null

  function newService() { return serviceComponent.createObject(root, { statusPath: root.statusPath }) }

  function check(condition, message) {
    if (!condition) failures++
    console.log("SMOKE " + (condition ? "ok" : "not ok") + " - " + stepName + ": " + message)
  }

  function ipc(method) {
    var answer = IpcRegistry.call(pluginId, method, [])
    return answer.ran ? answer.output : null
  }

  function stateIpc() {
    var text = ipc("state")
    try { return JSON.parse(text) } catch (error) { return {} }
  }

  function handlers() {
    return IpcRegistry.handlers.filter(function(handler) { return handler && handler.enabled && handler.target === root.pluginId }).length
  }

  function panelCount(svc) { return svc ? Object.keys(svc.panels).length : -1 }

  function temp(widget) { return widget.status && widget.status.thermal ? widget.status.thermal.package_c : null }

  // then(output) after an argv command; one at a time.
  function exec(argv, then) {
    tool.then = then
    tool.command = argv
    tool.running = true
  }

  function calls(then) { exec(["sh", "-c", "sleep 0.5; cat \"$1\"", "sh", root.fakeDir + "/calls"], then) }

  Process {
    id: tool
    property var then: null
    stdout: StdioCollector { id: toolOut; waitForEnd: true }
    onExited: function(_exitCode, _exitStatus) {
      var then = tool.then
      Qt.callLater(function() { if (then) then(toolOut.text) })
    }
  }

  // then() once predicate() holds; a check fails after `ms` otherwise.
  function waitFor(predicate, ms, what, then) {
    waitDeadline = Date.now() + ms
    waiting = { predicate: predicate, what: what, then: then }
  }

  Timer {
    interval: 25
    running: root.waiting !== null
    repeat: true
    onTriggered: {
      var wait = root.waiting
      var held = false
      try { held = wait.predicate() } catch (error) {}
      if (held) { root.waiting = null; wait.then(); return }
      if (Date.now() < root.waitDeadline) return
      root.waiting = null
      root.check(false, "timed out waiting for " + wait.what)
      root.next()
    }
  }

  Timer {
    interval: 120000
    running: true
    onTriggered: { root.check(false, "the run timed out"); console.log("SMOKE done " + root.failures); Qt.quit() }
  }

  Component.onCompleted: Qt.callLater(next)

  function next() {
    step++
    if (step >= steps.length) { console.log("SMOKE done " + failures); Qt.quit(); return }
    stepName = steps[step].name
    steps[step].run()
  }

  property var steps: [
    { name: "start", run: function() {
      root.service = root.newService()
      root.leftBar.target = root.service
      root.left = panelComponent.createObject(root, { bar: root.leftBar })
      root.right = panelComponent.createObject(root, { bar: root.replacementBar })
      var svc = root.service
      root.waitFor(function() { return svc.parsed.status !== null && svc.ipcReady }, 5000, "the service to load", function() {
        root.check(root.left.shellService === svc && root.left.service === svc, "the left widget gets the service from its bar's shell")
        root.check(root.right.shellService === null && root.right.service === svc,
          "the widget under a replacement bar gets it through ServiceLink")
        root.check(root.temp(root.left) === 77 && root.temp(root.right) === 77 && !root.right.parsed.stale,
          "both widgets show the status (" + root.temp(root.left) + ", " + root.temp(root.right) + ")")
        root.check(root.panelCount(svc) === 2 && svc.openPanels === 0, "both widgets reported their panel, none open")
        root.check(root.handlers() === 1, "one IPC handler for the plugin (" + root.handlers() + ")")
        root.next()
      })
    } },

    { name: "updates", run: function() {
      root.exec([root.writeStatus, "81"], function() {
        root.service.refresh()
        root.waitFor(function() { return root.temp(root.left) === 81 && root.temp(root.right) === 81 }, 3000,
          "both widgets to follow the status", function() {
            root.check(true, "one re-read reaches both widgets")
            root.next()
          })
      })
    } },

    { name: "open panels", run: function() {
      var svc = root.service
      root.right.open()
      root.check(svc.openPanels === 1, "the service counts the open panel")
      var state = root.stateIpc()
      root.check(state.opened === true && state.selected === "performance" && state.kind === "throttling" && state.dirty === false,
        "state reports the open panel (" + JSON.stringify(state) + ")")
      root.right.selectProfile("quiet")
      root.left.open()
      var selected = root.stateIpc().selected
      root.check(svc.openPanels === 2 && (selected === "quiet" || selected === "performance"),
        "with two panels open, state reports one of them (" + selected + ")")
      root.left.close()
      root.right.close()
      state = root.stateIpc()
      root.check(svc.openPanels === 0 && state.opened === false, "closing leaves no panel open")
      root.next()
    } },

    { name: "a save on one monitor", run: function() {
      root.right.open()
      root.right.selectProfile("quiet")
      root.right.restoreDefaults()
      root.check(root.right.busy && !root.left.busy, "only the saving widget waits for the helper")
      root.waitFor(function() { return !root.right.busy && root.temp(root.left) === 60 && root.temp(root.right) === 60 }, 5000,
        "the save to refresh both widgets", function() {
          root.check(root.right.actionNotice === "Quiet uses the default limits" && root.left.actionNotice === "",
            "only the saving panel reports it (" + root.right.actionNotice + ")")
          root.right.close()
          root.calls(function(text) {
            root.check(text.indexOf("/usr/local/libexec/workstation-cpu-limits --reset quiet") >= 0, "the widget ran pkexec with the reset")
            root.next()
          })
        })
    } },

    { name: "IPC routing", run: function() {
      root.ipc("open"); root.ipc("show"); root.ipc("close"); root.ipc("hide"); root.ipc("toggle")
      root.check(root.right.opened === false && root.left.opened === false, "no widget opens itself from IPC")
      root.calls(function(text) {
        root.check(text.indexOf("shell summon " + root.pluginId) >= 0 && text.indexOf("shell hide " + root.pluginId) >= 0
          && text.indexOf("shell toggle " + root.pluginId) >= 0, "panel actions go to the focused monitor through the shell")
        root.next()
      })
    } },

    { name: "a reloaded service", run: function() {
      root.previous = root.service
      root.service = root.newService()
      var svc = root.service
      root.leftBar.target = svc
      root.previous.destroy()
      root.waitFor(function() {
        return root.left.service === svc && root.right.service === svc && svc.parsed.status !== null && svc.ipcReady
          && root.handlers() === 1
      }, 5000, "both widgets to follow the new service", function() {
        root.check(root.panelCount(svc) === 2, "both widgets reported to the new service (" + root.panelCount(svc) + ")")
        root.exec([root.writeStatus, "70"], function() {
          svc.refresh()
          root.waitFor(function() { return root.temp(root.left) === 70 && root.temp(root.right) === 70 }, 3000,
            "updates from the new service", function() {
              root.check(true, "both widgets follow the new service's status")
              root.next()
            })
        })
      })
    } },

    { name: "the service gone", run: function() {
      root.leftBar.target = null
      root.service.destroy()
      root.service = null
      root.waitFor(function() { return root.left.service === null && root.right.service === null }, 3000,
        "both widgets to let go", function() {
          root.check(root.left.parsed.status === null && root.right.parsed.error === "Reading CPU status…"
            && root.right.barKind === "error", "both widgets show the neutral reading state")
          root.check(root.handlers() === 0, "no IPC handler is left")
          root.right.open()
          root.right.close()
          root.calls(function(text) {
            root.check(text.indexOf(root.pluginId + " refresh") >= 0, "a refresh goes over IPC")
            root.next()
          })
        })
    } },

    { name: "a service that loads later", run: function() {
      root.service = root.newService()
      var svc = root.service
      root.waitFor(function() {
        return root.left.service === svc && root.right.service === svc && root.temp(root.left) === 70 && root.temp(root.right) === 70
      }, 5000, "both widgets to find the late service", function() {
        root.check(root.left.shellService === null && root.panelCount(svc) === 2, "both reach it through ServiceLink and report to it")
        root.next()
      })
    } },

    { name: "a rebuilt bar", run: function() {
      var svc = root.service
      var extra = panelComponent.createObject(root, { bar: root.replacementBar })
      root.check(extra.service === svc && root.panelCount(svc) === 3, "a new widget finds the service and reports")
      extra.open()
      root.check(svc.openPanels === 1, "and counts while open")
      extra.destroy()
      root.waitFor(function() { return root.panelCount(svc) === 2 }, 2000, "the widget to leave", function() {
        root.check(svc.openPanels === 0, "a destroyed widget no longer counts")
        root.next()
      })
    } }
  ]
}
