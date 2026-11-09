import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: control
    property bool selected: false
    property string iconName: "list"
    property string count: ""
    implicitHeight: 40
    leftPadding: 14; rightPadding: 14
    hoverEnabled: true
    HoverHandler { cursorShape: Qt.PointingHandCursor }
    background: Rectangle {
        radius: 8
        color: control.selected ? Theme.navigation : control.hovered ? Theme.hover : "transparent"
        border.width: control.activeFocus ? 1 : 0
        border.color: control.activeFocus ? Theme.accent : Theme.line
    }
    contentItem: RowLayout {
        spacing: 12
        Icon { name: control.iconName; Layout.preferredWidth: 18; Layout.preferredHeight: 18 }
        UiText { text: control.text; Layout.fillWidth: true; color: control.selected ? Theme.ink : Theme.secondary; font.weight: control.selected ? Font.DemiBold : Font.Medium }
        UiText { text: control.count; color: control.selected ? Theme.ink : Theme.secondary; font.pixelSize: 13 }
    }
}
