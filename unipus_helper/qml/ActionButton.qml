import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: control
    property bool primary: false
    property bool quiet: false
    property bool destructive: false
    property bool accented: false
    property string iconName: ""
    property string hint: ""
    implicitHeight: 36
    implicitWidth: contentItem.implicitWidth + leftPadding + rightPadding
    leftPadding: 14
    rightPadding: 14
    hoverEnabled: true
    opacity: enabled ? 1 : 0.45
    font.pixelSize: 13
    background: Rectangle {
        radius: 8
        color: control.primary ? (control.down || control.hovered ? Theme.accentPressed : Theme.accentFill)
             : control.down ? Theme.hover : control.hovered ? Theme.raised
             : control.quiet ? "transparent" : Theme.surface
        border.width: control.primary || control.quiet ? 0 : 1
        border.color: control.activeFocus ? Theme.accent : Theme.line
        Rectangle {
            anchors.fill: parent
            anchors.margins: -2
            color: "transparent"
            radius: 10
            border.color: Theme.accent
            visible: control.visualFocus
        }
    }
    ToolTip.visible: hovered && hint !== ""
    ToolTip.text: hint
    ToolTip.delay: 650
    HoverHandler { enabled: control.enabled; cursorShape: Qt.PointingHandCursor }
    contentItem: RowLayout {
        spacing: 8
        Icon { name: control.iconName || "list"; visible: control.iconName !== ""; white: control.primary; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
        UiText { text: control.text; font.pixelSize: 13; font.weight: Font.Medium; color: control.primary ? "white" : control.destructive ? Theme.error : control.accented ? Theme.accent : Theme.ink; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
    }
}
