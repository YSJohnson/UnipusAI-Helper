import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: identity
    property string account: ""
    property string model: ""
    property string status: ""
    property string apiStatus: ""
    property bool apiConfigured: false
    property bool apiTesting: false
    property bool apiOk: false
    property bool apiFail: false
    property bool busy: false
    property bool ready: false
    property bool hasError: false
    signal testApiRequested()
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
                    font.pixelSize: 13; font.weight: Font.Medium
                    color: identity.apiConfigured ? Theme.accent : Theme.muted
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    HoverHandler { id: modelHover }
                    ToolTip.visible: modelHover.hovered && modelText.truncated
                    ToolTip.text: modelText.text
                    ToolTip.delay: 650
                }
                UiText {
                    visible: !identity.apiConfigured
                    objectName: "apiUnconfigured"
                    text: "仅官方答案"
                    font.pixelSize: 12
                    color: Theme.muted
                }
                Button {
                    id: apiTestButton
                    visible: identity.apiConfigured
                    objectName: "apiStatus"
                    enabled: !identity.apiTesting
                    hoverEnabled: true
                    leftPadding: 8; rightPadding: 8; topPadding: 2; bottomPadding: 2
                    implicitHeight: 22
                    Layout.maximumWidth: 140
                    text: identity.apiTesting ? "检测中…" : (identity.apiStatus === "未检测" || !identity.apiStatus ? "测试接口" : identity.apiStatus)
                    Accessible.name: "测试模型接口"
                    onClicked: identity.testApiRequested()
                    HoverHandler { cursorShape: apiTestButton.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor }
                    background: Rectangle {
                        radius: 5
                        color: apiTestButton.down ? Theme.navigation : apiTestButton.hovered && apiTestButton.enabled ? Theme.hover : Theme.canvas
                        border.width: 1
                        border.color: apiTestButton.activeFocus ? Theme.accent : Theme.subtleLine
                    }
                    contentItem: UiText {
                        text: apiTestButton.text
                        font.pixelSize: 12
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        color: identity.apiTesting ? Theme.muted : identity.apiOk ? "#45d579" : identity.apiFail ? Theme.error : Theme.secondary
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: identity.apiTesting ? "正在检测模型接口连通性" : (identity.apiStatus && identity.apiStatus !== "未检测" ? identity.apiStatus + "（点击重新测试）" : "点击测试模型 API 连通性")
                    ToolTip.delay: 450
                }
            }
        }
    }
}
