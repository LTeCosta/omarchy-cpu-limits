import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "ServiceLink.js" as ServiceLink

// CPU Limits bar widget, one per monitor. The status that
// cpu-frequency-limits.service publishes in /run is read once for every
// monitor by the plugin's service (Service.qml), which also owns the IPC
// target. This widget keeps its panel, drafts and keyboard cursor, and saves
// settings through `pkexec workstation-cpu-limits --set|--reset`.
Panel {
  id: root
  moduleName: "io.github.ltecosta.cpu-limits"
  manageIpc: false

  readonly property string helper: "/usr/local/libexec/workstation-cpu-limits"
  // The shell's answer under the built-in bar; under a replacement bar, whose
  // shell has no plugin services, the service ServiceLink.js holds.
  readonly property var shellService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor(moduleName) : null
  property var linkedService: null
  readonly property var service: shellService ? shellService : linkedService
  // Kept so the same function can be unsubscribed.
  property var serviceListener: null
  // Until the service is there, a neutral "reading" state that marks nothing.
  readonly property var parsed: service && service.parsed ? service.parsed : Model.unavailable("Reading CPU status…")
  readonly property var status: parsed.status
  property string selectedProfile: ""
  property var drafts: ({})
  property bool busy: false
  property string actionError: ""
  property string actionNotice: ""
  property bool cursorActive: false
  property var cursorItem: null
  // Identifies this widget's panel in the service's report of open panels.
  readonly property string instanceKey: String(Math.random()).slice(2) + "-" + Date.now()
  readonly property var panelReport: ({ opened: opened, selected: selectedProfile, dirty: dirty })

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var profileNames: status && status.profiles ? Object.keys(status.profiles) : []
  readonly property var savedSettings: status && status.profiles && status.profiles[selectedProfile]
    ? Model.settingsOf(status.profiles[selectedProfile]) : null
  readonly property var editSettings: drafts[selectedProfile] ? drafts[selectedProfile] : savedSettings
  readonly property bool dirty: !!drafts[selectedProfile]
  readonly property bool selectedCustom: !!(status && status.profiles && status.profiles[selectedProfile]
    && status.profiles[selectedProfile].custom)
  readonly property bool controlsEnabled: !!status && !parsed.stale && !busy && !!editSettings
  readonly property string barKind: Model.barKind(parsed)
  readonly property var power: status ? status.power : null
  readonly property var thermal: status ? status.thermal : null
  readonly property var tiles: Model.liveTiles(status)
  readonly property real minW: Model.limit(status, "min_w")
  readonly property real maxW: Model.limit(status, "max_w")
  readonly property int otherDrafts: Model.otherDraftCount(drafts, selectedProfile)
  readonly property int groupCount: editSettings && status && status.groups ? status.groups.length : 0
  readonly property bool powerAvailable: editSettings !== null && power !== null
  readonly property var powerRows: [
    { key: "pl1_w", title: "PL1 · sustained", detail: "Long-term package power" },
    { key: "pl2_w", title: "PL2 · boost", detail: "Short-term package power" }
  ]
  // Stands in for a group while a delegate outlives the status it came from.
  readonly property var noGroup: ({ id: "", label: "", cpus: [], min_mhz: 0, max_mhz: 0, default_mhz: 0 })
  // Rebuilt only when what the chips show changes, so the status refresh
  // every two seconds does not recreate them under the pointer.
  readonly property string profileKey: !status ? "" : JSON.stringify([profileNames, status.profile,
    profileNames.map(function(name) { return status.profiles[name].custom === true }), Object.keys(drafts)])
  property var profileOptions: []
  onProfileKeyChanged: profileOptions = buildProfileOptions()

  function buildProfileOptions() {
    if (!status) return []
    return profileNames.map(function(name) {
      var custom = status.profiles[name].custom === true
      return {
        value: name,
        label: Model.profileLabel(name) + (drafts[name] ? " *" : ""),
        icon: status.profile === name ? "󰐊" : "",
        tooltip: (status.profile === name ? "Active now · " : "") + (custom ? "custom limits" : "default limits")
      }
    })
  }

  // The service's IPC target stands in while no service is reachable.
  function ipc(args) { Quickshell.execDetached(["omarchy-shell", moduleName].concat(args)) }

  // A re-read reaches every monitor's widget through the service.
  function refresh() { if (service) service.refresh(); else ipc(["refresh"]) }

  function reportPanel() { if (service) service.reportPanel(instanceKey, panelReport) }

  function setDraft(next) {
    actionError = ""
    drafts = Model.withDraft(drafts, selectedProfile, next, savedSettings)
  }

  function discard() {
    actionError = ""
    drafts = Model.withoutDraft(drafts, selectedProfile)
  }

  // `notice` replaces the helper's success message (used by --reset);
  // `discarding` counts other profiles' drafts that an "all" save drops.
  function runHelper(args, target, notice, discarding) {
    if (busy) return
    busy = true
    actionError = ""
    actionNotice = ""
    writer.target = target
    writer.notice = notice
    writer.discarding = discarding
    writer.command = ["pkexec", helper].concat(args)
    writer.running = true
  }

  function apply(allProfiles) {
    if (!controlsEnabled || (!allProfiles && !dirty)) return
    var target = allProfiles ? "all" : selectedProfile
    runHelper(Model.helperArgs(target, editSettings), target, "", allProfiles ? otherDrafts : 0)
  }

  function restoreDefaults() {
    if (!status || parsed.stale || busy) return
    runHelper(["--reset", selectedProfile], selectedProfile,
      Model.profileLabel(selectedProfile) + " uses the default limits", 0)
  }

  function resetFocusedPower() {
    if (cursorActive && cursorItem && cursorItem.showReset === true) cursorItem.resetRequested()
  }

  function selectProfile(name) {
    if (profileNames.indexOf(name) < 0) return
    selectedProfile = name
    actionError = ""
    actionNotice = ""
    ensureCursor()
  }

  // Keyboard cursor: every visible item that defines keyboardEnabled and
  // keyboardActivate is a stop, in declaration order; h/l call keyboardAdjust.
  function navigationItems() {
    var items = []
    collect(content, items)
    return items
  }

  function collect(item, items) {
    if (!item || item.visible !== true) return
    if (item.keyboardEnabled !== undefined && typeof item.keyboardActivate === "function") {
      if (item.keyboardEnabled === true) items.push(item)
      return
    }
    for (var i = 0; i < item.children.length; i++) collect(item.children[i], items)
  }

  function setCursor(item) {
    if (!item || item.keyboardEnabled !== true) return
    cursorActive = true
    cursorItem = item
  }

  function ensureCursor() {
    if (!cursorActive) return
    var items = navigationItems()
    if (items.indexOf(cursorItem) < 0) cursorItem = items.length ? items[0] : null
  }

  function moveCursor(dx, dy) {
    var items = navigationItems()
    if (!items.length) return
    if (dy === 0) {
      if (cursorActive && cursorItem && typeof cursorItem.keyboardAdjust === "function")
        cursorItem.keyboardAdjust(dx)
      return
    }
    if (!cursorActive || items.indexOf(cursorItem) < 0) {
      setCursor(dy < 0 ? items[items.length - 1] : items[0])
      return
    }
    var next = Math.max(0, Math.min(items.length - 1, items.indexOf(cursorItem) + dy))
    setCursor(items[next])
    scrollIntoView(items[next])
  }

  function scrollIntoView(item) {
    Qt.callLater(function() {
      if (!item) return
      var top = item.mapToItem(scroller.contentItem, 0, 0).y
      var margin = Style.space(6)
      if (top < scroller.contentY + margin) scroller.contentY = Math.max(0, top - margin)
      else if (top + item.height > scroller.contentY + scroller.height - margin)
        scroller.contentY = Math.min(Math.max(0, scroller.contentHeight - scroller.height),
          top + item.height + margin - scroller.height)
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    cursorActive = false
    cursorItem = null
    actionNotice = ""
    if (opened) {
      actionError = ""
      refresh()
      if (status) selectedProfile = status.profile
      scroller.contentY = 0
    }
  }

  onPanelReportChanged: reportPanel()
  onServiceChanged: reportPanel()
  Component.onCompleted: {
    serviceListener = function(next) { root.linkedService = next }
    ServiceLink.subscribe(serviceListener)
    linkedService = ServiceLink.current()
  }
  Component.onDestruction: {
    ServiceLink.unsubscribe(serviceListener)
    if (service) service.forgetPanel(instanceKey)
  }

  onStatusChanged: {
    if (!status) return
    if (profileNames.indexOf(selectedProfile) < 0) selectedProfile = status.profile
    // Prune only removes, so an unchanged count means nothing to drop.
    var pruned = Model.pruneDrafts(drafts, status.profiles)
    if (Object.keys(pruned).length !== Object.keys(drafts).length) drafts = pruned
  }

  Process {
    id: writer
    property string target: ""
    property string notice: ""
    property int discarding: 0
    stdout: StdioCollector { id: writerOut; waitForEnd: true }
    stderr: StdioCollector { id: writerErr; waitForEnd: true }
    onExited: function(exitCode, _exitStatus) {
      Qt.callLater(function() {
        root.busy = false
        var outcome = Model.helperOutcome(exitCode, writerErr.text, writer.target, writer.discarding, writerOut.text)
        if (outcome.ok) {
          root.drafts = Model.afterSave(root.drafts, writer.target)
          root.actionNotice = writer.notice || outcome.message
        } else {
          root.actionError = outcome.message
        }
        root.refresh()
      })
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰻠"
    labelText: Model.barLabel(root.parsed, String(root.setting("barLabel", "None")).toLowerCase())
    tooltipText: Model.tooltip(root.parsed)
    active: root.service !== null && root.barKind !== "ok"
    activeColor: root.barKind === "error" ? root.urgent : Color.accent
    onPressed: function(buttonCode) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(410))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(720))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: if (root.cursorActive && root.cursorItem) root.cursorItem.keyboardActivate()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(value) {
        if (value === "a" || value === "A") root.apply(false)
        else if (value === "u" || value === "U") root.discard()
        else if (value === "t" || value === "T") turboRow.keyboardActivate()
        else if (value === "f" || value === "F") root.resetFocusedPower()
        else if (value >= "1" && value <= "9") root.selectProfile(root.profileNames[Number(value) - 1])
      }

      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: content
          width: scroller.width
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            title: "CPU Limits"
            meta: Model.heroMeta(root.parsed)
            foreground: root.foreground; fontFamily: root.fontFamily
            iconOpacity: root.status && !root.parsed.stale ? 1.0 : 0.55
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: "󰻠"
                color: root.barKind === "error" ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              Text {
                textFormat: Text.PlainText
                visible: root.status !== null && !root.parsed.stale
                text: root.status && root.status.turbo ? "Turbo on" : "Turbo off"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            text: root.actionError !== "" ? root.actionError
              : (root.parsed.error !== "" ? root.parsed.error : (root.busy ? "Waiting for authentication…" : root.actionNotice))
            color: root.actionError !== "" || root.parsed.error !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---------- Live readings ----------
          Row {
            id: tiles
            visible: root.status !== null
            width: parent.width
            spacing: Style.space(6)
            readonly property real tileWidth: (width - spacing * 3) / 4

            Repeater {
              model: 4
              delegate: StatTile {
                required property int index
                readonly property var tile: root.tiles[index]
                width: tiles.tileWidth
                value: tile.value
                label: tile.label
                alert: tile.alert !== ""
                alertColor: tile.alert === "urgent" ? root.urgent : Color.accent
                foreground: root.foreground; fontFamily: root.fontFamily
              }
            }
          }

          Row {
            visible: root.thermal !== null
            width: parent.width
            spacing: Style.space(6)

            Text {
              textFormat: Text.PlainText
              text: root.thermal && root.thermal.throttling ? "󰈸" : "󰔏"
              color: root.thermal && root.thermal.throttling ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width - Style.space(24)
              text: Model.throttleText(root.thermal) + (Model.atPowerLimit(root.power) ? " · at PL1" : "")
              color: root.thermal && root.thermal.throttling ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            text: root.parsed.warnings ? root.parsed.warnings.join("\n") : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          PanelSeparator { foreground: root.foreground; visible: root.status !== null }

          // ---------- Profile ----------
          Item {
            visible: root.profileNames.length > 0
            width: parent.width
            implicitHeight: profileHeader.implicitHeight

            PanelSectionHeader {
              id: profileHeader
              text: "PROFILE SETTINGS"
              foreground: root.foreground; fontFamily: root.fontFamily
              anchors.left: parent.left
            }

            Text {
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.verticalCenter: profileHeader.verticalCenter
              text: root.status ? "active: " + Model.profileLabel(root.status.profile) : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Item {
            id: profileRow
            visible: root.profileNames.length > 0
            width: parent.width
            implicitHeight: profileGroup.implicitHeight
            readonly property bool keyboardEnabled: visible
            function keyboardActivate() {}
            function keyboardAdjust(direction) {
              var index = root.profileNames.indexOf(root.selectedProfile) + direction
              if (index >= 0 && index < root.profileNames.length) root.selectProfile(root.profileNames[index])
            }

            ButtonGroup {
              id: profileGroup
              width: parent.width
              focusable: false
              options: root.profileOptions
              value: root.selectedProfile
              foreground: root.foreground; fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              cursorIndex: root.cursorItem === profileRow ? root.profileNames.indexOf(root.selectedProfile) : -1
              onChanged: function(value) { root.selectProfile(value) }
              onHovered: function(_index, on) { if (on) root.setCursor(profileRow) }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.profileNames.length > 0
            width: parent.width
            text: root.status && root.selectedProfile === root.status.profile
              ? "Editing the active profile" + (root.status.epp ? " (EPP " + root.status.epp + ")" : "")
                + ". The power profile chooses which settings apply."
              : "Editing " + Model.profileLabel(root.selectedProfile) + "; it applies when that power profile is active."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // ---------- Frequency ceilings ----------
          PanelSectionHeader {
            visible: root.editSettings !== null
            text: "FREQUENCY CEILINGS"
            foreground: root.foreground; fontFamily: root.fontFamily
          }

          Repeater {
            model: root.groupCount

            delegate: StepperRow {
              id: ceilingRow
              required property int index
              readonly property var modelData: root.status && root.status.groups && root.status.groups[index]
                ? root.status.groups[index] : root.noGroup
              readonly property int ceiling: root.editSettings && root.editSettings.ceilings_mhz[modelData.id] !== undefined
                ? root.editSettings.ceilings_mhz[modelData.id] : 0
              readonly property int saved: root.savedSettings && root.savedSettings.ceilings_mhz[modelData.id] !== undefined
                ? root.savedSettings.ceilings_mhz[modelData.id] : ceiling
              width: content.width
              enabled: root.controlsEnabled && modelData !== root.noGroup
              title: modelData.label
              detail: Model.groupDetail(modelData)
              valueText: Model.mhzText(ceiling)
              fraction: modelData.max_mhz > 0 ? ceiling / modelData.max_mhz : 0
              markerFraction: modelData.avg_mhz && modelData.max_mhz > 0 ? modelData.avg_mhz / modelData.max_mhz : -1
              caption: Model.groupCaption(modelData)
              trailingCaption: ceiling === modelData.default_mhz ? "default" : "default " + modelData.default_mhz
              canDecrease: ceiling > modelData.min_mhz
              canIncrease: ceiling < modelData.max_mhz
              edited: ceiling !== saved
              foreground: root.foreground; fontFamily: root.fontFamily
              hasCursor: root.cursorItem === ceilingRow
              onPointerEntered: root.setCursor(ceilingRow)
              onAdjust: function(direction) {
                if (root.controlsEnabled && modelData !== root.noGroup)
                  root.setDraft(Model.stepCeiling(root.editSettings, modelData, direction, root.status))
              }
            }
          }

          TurboRow {
            id: turboRow
            visible: root.editSettings !== null
            width: parent.width
            enabled: root.controlsEnabled
            checked: root.editSettings ? root.editSettings.turbo : false
            edited: !!root.editSettings && !!root.savedSettings && root.editSettings.turbo !== root.savedSettings.turbo
            busy: root.busy
            foreground: root.foreground; fontFamily: root.fontFamily
            hasCursor: root.cursorItem === turboRow
            onPointerEntered: root.setCursor(turboRow)
            onTurboRequested: function(on) {
              if (!root.controlsEnabled) return
              var next = Model.settingsOf(root.editSettings)
              next.turbo = on
              root.setDraft(next)
            }
          }

          // ---------- Power limits ----------
          PanelSectionHeader {
            visible: root.editSettings !== null && root.power !== null
            text: "PACKAGE POWER LIMITS"
            foreground: root.foreground; fontFamily: root.fontFamily
          }

          Repeater {
            model: root.powerAvailable ? root.powerRows.length : 0

            delegate: StepperRow {
              id: powerRow
              required property int index
              readonly property var modelData: root.powerRows[index]
              readonly property var value: root.editSettings ? root.editSettings[modelData.key] : null
              readonly property var saved: root.savedSettings ? root.savedSettings[modelData.key] : value
              // Explicit value, else the selected profile's firmware value, else the MSR.
              readonly property var shown: Model.startValue(root.status, root.selectedProfile, root.editSettings, modelData.key)
              readonly property var firmware: Model.firmwareFor(root.status, root.selectedProfile, modelData.key)
              readonly property var mmio: root.power && root.power.mmio ? root.power.mmio[modelData.key] : null
              readonly property var packageW: root.power ? root.power.package_w : null
              width: content.width
              enabled: root.controlsEnabled
              title: modelData.title
              detail: modelData.detail
              valueText: Model.wattsText(shown)
              fraction: shown !== null && root.maxW > 0 ? shown / root.maxW : 0
              markerFraction: packageW !== null && packageW !== undefined && root.maxW > 0 ? packageW / root.maxW : -1
              caption: [modelData.key === "pl1_w" ? "package now " + Model.wattsText(packageW)
                  : "PL4 peak " + Model.wattsText(root.power ? root.power.pl4_w : null),
                Model.powerCaption(root.status, modelData.key)].filter(function(part) { return part !== "" }).join(" · ")
              trailingCaption: firmware === null ? "firmware not observed yet" : "firmware " + Model.wattsText(firmware)
              warn: mmio !== null && mmio !== undefined && shown !== null && shown > mmio
              canDecrease: shown === null || shown > root.minW
              canIncrease: shown === null || shown < root.maxW
              showReset: value !== null && value !== undefined
              resetTooltip: "Use the firmware value (f or Enter)"
              edited: value !== saved
              foreground: root.foreground; fontFamily: root.fontFamily
              hasCursor: root.cursorItem === powerRow
              onPointerEntered: root.setCursor(powerRow)
              onAdjust: function(direction) {
                if (root.controlsEnabled) root.setDraft(
                  Model.stepPower(root.editSettings, modelData.key, direction, root.status, root.selectedProfile))
              }
              onResetRequested: if (root.controlsEnabled) root.setDraft(
                Model.useFirmware(root.editSettings, modelData.key, root.status, root.selectedProfile))
            }
          }

          PanelSeparator { foreground: root.foreground; visible: root.editSettings !== null }

          // ---------- Actions ----------
          Row {
            id: actions
            visible: root.editSettings !== null
            width: parent.width
            spacing: Style.space(6)
            readonly property real buttonWidth: (width - spacing * 2) / 3

            ActionButton {
              id: applyButton
              width: actions.buttonWidth
              text: root.busy ? "Applying…" : "Apply"
              iconText: "󰄬"
              tooltipText: "Save and apply to " + Model.profileLabel(root.selectedProfile) + " (a)"
              selected: root.dirty
              enabled: root.dirty && root.controlsEnabled
              cursorItem: root.cursorItem
              foreground: root.foreground; fontFamily: root.fontFamily
              onPointerEntered: root.setCursor(applyButton)
              onActivated: root.apply(false)
            }

            ActionButton {
              id: applyAllButton
              width: actions.buttonWidth
              text: "All profiles"
              iconText: "󰘞"
              tooltipText: "Save these settings for every profile" + (root.otherDrafts > 0
                ? "; discards unsaved changes in " + root.otherDrafts + (root.otherDrafts === 1 ? " other profile" : " other profiles") : "")
              enabled: root.controlsEnabled
              cursorItem: root.cursorItem
              foreground: root.foreground; fontFamily: root.fontFamily
              onPointerEntered: root.setCursor(applyAllButton)
              onActivated: root.apply(true)
            }

            ActionButton {
              id: discardButton
              width: actions.buttonWidth
              text: "Discard"
              iconText: "󰜺"
              tooltipText: "Discard unsaved changes (u)"
              enabled: root.dirty && !root.busy
              cursorItem: root.cursorItem
              foreground: root.foreground; fontFamily: root.fontFamily
              onPointerEntered: root.setCursor(discardButton)
              onActivated: root.discard()
            }
          }

          ActionButton {
            id: restoreButton
            visible: root.selectedCustom
            width: parent.width
            leftAlign: true
            bordered: false
            text: "Restore the default limits for " + Model.profileLabel(root.selectedProfile)
            iconText: "󰦛"
            enabled: !root.busy && !root.parsed.stale
            cursorItem: root.cursorItem
            foreground: root.foreground; fontFamily: root.fontFamily
            onPointerEntered: root.setCursor(restoreButton)
            onActivated: root.restoreDefaults()
          }
        }
      }
    }
  }
}
