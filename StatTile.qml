import QtQuick
import qs.Commons
import qs.Ui

// Live reading: a large value over a small-caps label, in a bordered tile.
CursorSurface {
  id: tile

  property string value: "—"
  property string label: ""
  property bool alert: false
  property color alertColor: Color.urgent
  property string fontFamily: Style.font.family

  bordered: true
  implicitHeight: column.implicitHeight + Style.space(14)

  Column {
    id: column
    anchors.centerIn: parent
    width: parent.width - Style.space(8)
    spacing: Style.space(2)

    Text {
      textFormat: Text.PlainText
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: tile.value
      color: tile.alert ? tile.alertColor : tile.foreground
      font.family: tile.fontFamily
      font.pixelSize: Style.font.heading
      font.bold: true
      elide: Text.ElideRight
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: tile.label.toUpperCase()
      color: Qt.darker(tile.foreground, 1.4)
      font.family: tile.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: 1.0
      elide: Text.ElideRight
    }
  }
}
