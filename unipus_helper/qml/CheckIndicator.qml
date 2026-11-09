import QtQuick

Rectangle {
    property int checkState: Qt.Unchecked
    property bool highlighted: false
    implicitWidth: 18; implicitHeight: 18
    radius: 4
    color: checkState !== Qt.Unchecked ? Theme.accentFill : Theme.surface
    border.width: 1
    border.color: highlighted ? Theme.accent : checkState !== Qt.Unchecked ? Theme.accentFill : Theme.muted
    Icon { anchors.centerIn: parent; name: "check"; white: true; width: 14; height: 14; visible: parent.checkState === Qt.Checked }
    Rectangle { anchors.centerIn: parent; width: 8; height: 2; color: "white"; visible: parent.checkState === Qt.PartiallyChecked }
}
