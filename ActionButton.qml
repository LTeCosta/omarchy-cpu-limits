import QtQuick
import qs.Commons
import qs.Ui

// A bordered panel button that is also a keyboard cursor stop. The panel
// passes its `cursorItem`, so the highlight follows the shared cursor, and
// handles `activated()` for both a click and Enter.
Button {
  id: action

  property var cursorItem: null
  readonly property bool keyboardEnabled: visible && enabled

  signal activated()
  signal pointerEntered()

  function keyboardActivate() { if (action.enabled) action.activated() }

  bordered: true
  hasCursor: cursorItem === action
  fontSize: Style.font.bodySmall
  onHovered: function(on) { if (on) action.pointerEntered() }
  onClicked: action.activated()
}
