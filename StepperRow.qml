import QtQuick
import qs.Commons
import qs.Ui

// One adjustable limit: title, − value +, a track filled to the limit with a
// marker at the live value, and a caption line. Keyboard: h/l adjust; Enter
// runs the reset action when it is shown. The mouse wheel scrolls the panel.
CursorSurface {
  id: row

  property string title: ""
  property string detail: ""
  property string valueText: ""
  property string caption: ""
  property string trailingCaption: ""
  property real fraction: 0
  property real markerFraction: -1
  property bool canDecrease: true
  property bool canIncrease: true
  property bool showReset: false
  property string resetTooltip: ""
  property bool edited: false
  // Colors the trailing caption: the shown value is above a limit that holds it lower.
  property bool warn: false
  property string fontFamily: Style.font.family

  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color faint: Util.alpha(foreground, 0.5)
  readonly property bool keyboardEnabled: visible && enabled

  signal adjust(int direction)
  signal resetRequested()
  signal pointerEntered()

  function keyboardActivate() { if (row.showReset) row.resetRequested() }
  function keyboardAdjust(direction) { row.adjust(direction) }

  width: parent ? parent.width : implicitWidth
  implicitHeight: content.implicitHeight + Style.space(12)

  HoverHandler {
    onHoveredChanged: if (hovered) row.pointerEntered()
  }

  Column {
    id: content
    x: Style.space(8)
    width: parent.width - Style.space(16)
    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(5)

    Item {
      width: parent.width
      implicitHeight: Math.max(titleColumn.implicitHeight, controls.implicitHeight)

      Column {
        id: titleColumn
        anchors.left: parent.left
        anchors.right: controls.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          width: parent.width
          text: row.title
          color: row.foreground
          font.family: row.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: text !== ""
          text: row.detail
          color: row.dim
          font.family: row.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Row {
        id: controls
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        PanelActionButton {
          visible: row.showReset
          iconText: "󰦛"
          tooltipText: row.resetTooltip
          foreground: row.foreground
          fontFamily: row.fontFamily
          onHovered: function(on) { if (on) row.pointerEntered() }
          onClicked: row.resetRequested()
        }

        PanelActionButton {
          iconText: "󰍴"
          enabled: row.canDecrease
          foreground: row.foreground
          fontFamily: row.fontFamily
          onHovered: function(on) { if (on) row.pointerEntered() }
          onClicked: row.adjust(-1)
        }

        Text {
          textFormat: Text.PlainText
          width: Style.space(82)
          anchors.verticalCenter: parent.verticalCenter
          horizontalAlignment: Text.AlignHCenter
          text: row.valueText
          color: row.edited ? Color.accent : row.foreground
          font.family: row.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }

        PanelActionButton {
          iconText: "󰐕"
          enabled: row.canIncrease
          foreground: row.foreground
          fontFamily: row.fontFamily
          onHovered: function(on) { if (on) row.pointerEntered() }
          onClicked: row.adjust(1)
        }
      }
    }

    Rectangle {
      id: track
      width: parent.width
      height: Style.space(5)
      radius: Style.cornerRadius
      color: Style.selectedFillFor(row.foreground, Color.accent)

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: track.width * Math.max(0, Math.min(1, row.fraction))
        height: track.height
        radius: Style.cornerRadius
        color: row.edited ? Color.accent : row.foreground
        Behavior on width { NumberAnimation { duration: Style.duration(120); easing.type: Easing.OutCubic } }
      }

      Rectangle {
        visible: row.markerFraction >= 0
        x: Math.round(track.width * Math.max(0, Math.min(1, row.markerFraction))) - width / 2
        width: Style.space(2)
        height: track.height + Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        radius: width / 2
        color: Color.accent
        Behavior on x { NumberAnimation { duration: Style.duration(400); easing.type: Easing.OutCubic } }
      }
    }

    Item {
      width: parent.width
      implicitHeight: Math.max(captionText.implicitHeight, trailingText.implicitHeight)
      visible: row.caption !== "" || row.trailingCaption !== ""

      Text {
        id: captionText
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.right: trailingText.left
        anchors.rightMargin: Style.space(8)
        text: row.caption
        color: row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        id: trailingText
        textFormat: Text.PlainText
        anchors.right: parent.right
        text: row.trailingCaption
        color: row.warn ? Color.accent : row.faint
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }
}
