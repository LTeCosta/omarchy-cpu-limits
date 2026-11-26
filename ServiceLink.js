.pragma library
// The plugin's private link from its bar widgets to its one service. A
// .pragma library script has one copy per QML engine and URL, so Service.qml
// and every monitor's Panel.qml, importing it by the same relative path, share
// it. The widget asks the shell first (bar.shell.serviceFor); a replacement
// bar gives widgets a shell without the plugin's service, and this link is
// how they reach it then.

var service = null
var listeners = []

function notify() {
  var current = service
  var snapshot = listeners.slice()
  for (var i = 0; i < snapshot.length; i++) {
    try { snapshot[i](current) } catch (error) { console.warn("CPU Limits service listener failed: " + error) }
  }
}

// A newer service replaces the registered one: a reloaded plugin builds its
// new service before the old one is torn down.
function register(next) {
  if (!next || service === next) return
  service = next
  notify()
}

// Clears the link only while `old` is still the registered service.
function unregister(old) {
  if (!old || service !== old) return
  service = null
  notify()
}

function current() { return service }

function subscribe(fn) {
  if (typeof fn === "function" && listeners.indexOf(fn) < 0) listeners.push(fn)
}

function unsubscribe(fn) {
  var index = listeners.indexOf(fn)
  if (index >= 0) listeners.splice(index, 1)
}
