import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ListView {
    id: view
    property var entries: []
    property bool followTail: true
    function followLatest() { if (followTail && count > 0) Qt.callLater(function() { view.positionViewAtEnd() }) }
    model: entries
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    spacing: 0
    onEntriesChanged: followLatest()
    onCountChanged: followLatest()
    onHeightChanged: followLatest()
    onWidthChanged: followLatest()
    onMovementStarted: followTail = false
    onMovementEnded: followTail = atYEnd
    onAtYEndChanged: if (atYEnd && !moving) followTail = true
    header: Rectangle {
        width: view.width; height: 36; color: Theme.raised
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 16
            UiText { text: "时间"; font.pixelSize: 13; color: Theme.muted; Layout.preferredWidth: 72 }
            UiText { text: "类型"; font.pixelSize: 13; color: Theme.muted; Layout.preferredWidth: 40 }
            UiText { text: "内容"; font.pixelSize: 13; color: Theme.muted; Layout.fillWidth: true }
        }
    }
    delegate: Rectangle {
        id: logRow
        required property var modelData
        readonly property color tone: modelData.level === "error" ? Theme.error : modelData.level === "warning" ? Theme.warning : modelData.level === "success" ? Theme.success : Theme.secondary
        readonly property string levelName: modelData.level === "error" ? "错误" : modelData.level === "warning" ? "提醒" : modelData.level === "success" ? "成功" : "信息"
        objectName: "log_" + modelData.id
        width: view.width
        height: Math.max(44, logContent.implicitHeight + 24)
        color: modelData.level === "error" ? Qt.rgba(0.94, 0.67, 0.65, 0.06) : modelData.level === "warning" ? Qt.rgba(0.89, 0.75, 0.53, 0.04) : "transparent"
        Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 2; color: logRow.tone; visible: logRow.modelData.level === "error" || logRow.modelData.level === "warning" }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.subtleLine }
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; anchors.topMargin: 12; anchors.bottomMargin: 12; spacing: 16
            UiText { text: logRow.modelData.time; color: Theme.muted; font.features: { "tnum": 1 }; font.pixelSize: 13; Layout.alignment: Qt.AlignTop; Layout.preferredWidth: 72; Layout.topMargin: 2 }
            UiText { text: logRow.levelName; color: logRow.tone; font.pixelSize: 13; font.weight: Font.Medium; Layout.alignment: Qt.AlignTop; Layout.preferredWidth: 40; Layout.topMargin: 2 }
            TextEdit {
                id: logContent
                objectName: "logMessage_" + logRow.modelData.id
                text: logRow.modelData.message; color: logRow.tone; font.family: Theme.fontFamily; font.weight: Font.Medium; font.pixelSize: 14
                font.hintingPreference: Font.PreferFullHinting; renderType: TextEdit.NativeRendering
                textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                selectedTextColor: "white"; selectionColor: Theme.accentFill
                Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.alignment: Qt.AlignTop
                Accessible.name: logRow.levelName + "，" + logRow.modelData.message
            }
        }
    }
    UiText { visible: view.count === 0; anchors.centerIn: parent; text: "暂无日志"; font.pixelSize: 14; color: Theme.muted }
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; onPressedChanged: { if (pressed) view.followTail = false; else view.followTail = view.atYEnd } }
}
