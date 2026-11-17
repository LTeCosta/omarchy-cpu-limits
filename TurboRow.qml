import QtQuick
import qs.Commons
import qs.Ui

// The turbo boost row: a title, what the current choice means, and a switch
// the row owns. Click or Enter toggles; h/l turn it off/on.
CursorSurface {
  id: row

  property bool checked: false
  property bool edited: false
  property bool busy: false
  property string fontFamily: Style.font.family

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property bool keyboardEnabled: visible && enabled

  signal turboRequested(bool on)
  signal pointerEntered()

  function keyboardActivate() { if (row.enabled) row.turboRequested(!row.checked) }
  function keyboardAdjust(direction) { if (row.enabled && row.checked !== (direction > 0)) row.turboRequested(direction > 0) }

  width: parent ? parent.width : implicitWidth
  implicitHeight: content.implicitHeight + Style.space(12)

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onContainsMouseChanged: if (containsMouse) row.pointerEntered()
    onClicked: row.keyboardActivate()
  }

  Item {
    id: content
    x: Style.space(8)
    width: parent.width - Style.space(16)
    anchors.verticalCenter: parent.verticalCenter
    implicitHeight: Math.max(labels.implicitHeight, toggle.implicitHeight)

    Column {
      id: labels
      anchors.left: parent.left
      anchors.right: toggle.left
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(1)

      Text {
        textFormat: Text.PlainText
        text: "Turbo boost"
        color: row.edited ? Color.accent : row.foreground
        font.family: row.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: row.checked ? "Above base clock, up to the ceilings" : "Every core at or below its base clock"
        color: row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    ToggleSwitch {
      id: toggle
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      interactive: false
      checked: row.checked
      busy: row.busy
      foreground: row.foreground
    }
  }
}
