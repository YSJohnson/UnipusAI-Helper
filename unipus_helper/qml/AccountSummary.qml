import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: identity
    property string account: ""
    property string model: ""
    property string status: ""
    property bool busy: false
    property bool ready: false
    property bool hasError: false
    implicitHeight: 68
    radius: 10; color: Theme.surface; border.color: Theme.line
    RowLayout {
        anchors.fill: parent; anchors.margins: 12; spacing: 12
        Rectangle {
            width: 32; height: 32; radius: 8; color: Theme.navigation
            Icon { name: "account"; anchors.centerIn: parent; width: 20; height: 20 }
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 6
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                UiText { text: "账号"; font.pixelSize: 13; color: Theme.muted; Layout.preferredWidth: 26 }
                UiText { objectName: "accountName"; text: identity.account; font.pixelSize: 13; font.weight: Font.Medium; Layout.fillWidth: true; Layout.minimumWidth: 0 }
                Rectangle { width: 6; height: 6; radius: 3; color: identity.hasError ? Theme.error : identity.busy ? Theme.accent : identity.ready ? "#45d579" : Theme.muted }
                UiText { objectName: "runtimeStatus"; text: identity.status; font.pixelSize: 13; color: identity.hasError ? Theme.error : Theme.secondary; Layout.maximumWidth: 88 }
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                UiText { text: "模型"; font.pixelSize: 13; color: Theme.muted; Layout.preferredWidth: 26 }
                UiText {
                    id: modelText; objectName: "modelName"; text: identity.model
                    font.pixelSize: 13; font.weight: Font.Medium; color: Theme.accent; Layout.fillWidth: true; Layout.minimumWidth: 0
                    HoverHandler { id: modelHover }
                    ToolTip.visible: modelHover.hovered && modelText.truncated
                    ToolTip.text: modelText.text
                    ToolTip.delay: 650
                }
            }
        }
    }
}
