import QtQuick
import QtQuick.Layouts

Rectangle {
    id: overview
    property var counts: ({ all: 0, done: 0, pending: 0 })
    readonly property real completion: counts.all > 0 ? counts.done / counts.all : 0
    property bool compact: false
    color: Theme.surface; radius: 12; border.color: Theme.line
    implicitHeight: compact ? 64 : 72
    RowLayout {
        anchors.fill: parent; anchors.leftMargin: overview.compact ? 16 : 24; anchors.rightMargin: overview.compact ? 16 : 24
        spacing: overview.compact ? 16 : 24
        Repeater {
            model: [{ key: "all", title: "总任务", icon: "list" }, { key: "done", title: "已完成", icon: "completed" }, { key: "pending", title: "未完成", icon: "pending" }]
            RowLayout {
                id: stat
                required property var modelData
                Layout.fillWidth: true; spacing: overview.compact ? 12 : 16
                Icon { name: stat.modelData.icon; Layout.preferredWidth: 22; Layout.preferredHeight: 22 }
                ColumnLayout {
                    Layout.fillWidth: true; spacing: 0
                    UiText { text: stat.modelData.title; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                    UiText {
                        objectName: "overview_" + stat.modelData.key
                        text: overview.counts[stat.modelData.key]; color: stat.modelData.key === "done" ? Theme.success : Theme.ink
                        font.pixelSize: overview.compact ? 24 : 26; font.weight: Font.DemiBold; font.features: { "tnum": 1 }
                    }
                }
                Rectangle { width: 1; height: 40; color: Theme.line }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true; Layout.preferredWidth: overview.width * 0.27; Layout.minimumWidth: overview.compact ? 140 : 160; spacing: 8
            RowLayout {
                spacing: 8
                UiText { text: "完成进度"; color: Theme.secondary; font.pixelSize: 13 }
                UiText { objectName: "overview_progress"; text: (overview.completion * 100).toFixed(1) + "%"; font.pixelSize: 14; font.weight: Font.Medium; font.features: { "tnum": 1 } }
            }
            ProgressTrack { objectName: "courseProgress"; Layout.fillWidth: true; value: overview.completion; implicitHeight: 8 }
        }
    }
}
