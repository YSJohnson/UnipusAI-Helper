import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: row
    required property int index
    required property string taskId
    required property string title
    required property string status
    required property string statusName
    required property bool chosen
    required property bool isRequired
    required property var scored
    required property string sectionHeader
    required property bool selectable
    required property string scoreText
    required property bool scoreAbnormal
    property bool editable: true
    height: sectionHeader ? 84 : 48

    function toggleSelection(keyboard) {
        if (!row.editable || !row.selectable) return
        row.ListView.view.currentIndex = row.index
        if (keyboard || !taskControl.activeFocus)
            taskControl.forceActiveFocus(keyboard ? Qt.TabFocusReason : Qt.MouseFocusReason)
        workbench.selectTask(row.taskId, !row.chosen)
    }

    function focusTask() { taskControl.forceActiveFocus(Qt.TabFocusReason) }
    function focusIndex(targetIndex) {
        const view = row.ListView.view
        const next = Math.max(0, Math.min(view.count - 1, targetIndex))
        view.currentIndex = next
        view.positionViewAtIndex(next, ListView.Contain)
        Qt.callLater(function() {
            const target = view.itemAtIndex(next)
            if (target) target.focusTask()
        })
    }

    function moveFocus(step) { focusIndex(row.index + step) }

    Rectangle {
        width: parent.width; height: 36; visible: row.sectionHeader !== ""
        color: Theme.raised
        UiText {
            id: sectionTitle; x: 24; height: parent.height; width: parent.width - 48
            text: row.sectionHeader; color: Theme.secondary; font.pixelSize: 13
        }
        HoverHandler { id: sectionHover }
        ToolTip.visible: sectionHover.hovered && sectionTitle.truncated
        ToolTip.text: row.sectionHeader
        ToolTip.delay: 650
    }
    ItemDelegate {
        id: taskControl
        objectName: "task_" + row.taskId
        anchors.fill: parent; anchors.topMargin: row.sectionHeader ? 36 : 0
        enabled: row.editable; hoverEnabled: true
        // The model owns selection. Do not let a rejected click leave a local
        // checked state out of sync with the Python controller.
        checkable: false; checked: row.chosen
        focusPolicy: Qt.StrongFocus
        leftPadding: 24; rightPadding: 24; topPadding: 0; bottomPadding: 0
        Accessible.role: Accessible.CheckBox
        Accessible.name: row.title + "，" + (row.isRequired ? "必修" : "选修") + "，" + row.statusName
        Accessible.checkable: true
        Accessible.checked: row.chosen
        Accessible.onPressAction: row.toggleSelection(false)
        Accessible.onToggleAction: row.toggleSelection(false)
        Accessible.description: row.selectable ? "点击整行或按空格勾选任务" : "已排除不计分题目，关闭底部选项后可勾选"
        onClicked: row.toggleSelection(false)
        Keys.onSpacePressed: row.toggleSelection(true)
        Keys.onReturnPressed: row.toggleSelection(true)
        Keys.onEnterPressed: row.toggleSelection(true)
        Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                row.focusIndex(event.key === Qt.Key_Home ? 0 : row.ListView.view.count - 1)
                event.accepted = true
            } else if (event.key === Qt.Key_A && (event.modifiers & Qt.ControlModifier)) {
                workbench.selectVisible("all")
                event.accepted = true
            }
        }
        Keys.onUpPressed: row.moveFocus(-1)
        Keys.onDownPressed: row.moveFocus(1)
        background: Rectangle {
            radius: 7
            color: taskControl.down && row.selectable ? Theme.selectionHover : row.chosen ? (taskControl.hovered ? Theme.selectionHover : Theme.selection) : taskControl.hovered ? Theme.raised : "transparent"
            border.width: row.chosen || taskControl.visualFocus ? 1 : 0
            border.color: taskControl.visualFocus ? Theme.accent : Theme.selectionLine
        }
        HoverHandler { enabled: row.editable; cursorShape: row.selectable ? Qt.PointingHandCursor : Qt.ForbiddenCursor }
        contentItem: RowLayout {
            spacing: 16
            CheckIndicator {
                objectName: "taskCheck_" + row.taskId
                Layout.preferredWidth: 18; Layout.preferredHeight: 18; Layout.rightMargin: 2
                checkState: row.chosen ? Qt.Checked : Qt.Unchecked
                opacity: row.selectable ? 1 : 0.45
                highlighted: row.selectable && (taskControl.hovered || taskControl.visualFocus)
            }
            UiText {
                id: taskTitle
                text: row.title; Layout.fillWidth: true; Layout.minimumWidth: 0
                font.pixelSize: 15; font.weight: row.status === "doing" ? Font.DemiBold : Font.Normal
            }
            Rectangle {
                objectName: "requirement_" + row.taskId
                Layout.preferredWidth: 48; Layout.preferredHeight: 24; radius: 6
                color: row.isRequired ? Theme.compulsory : Theme.raised; border.color: row.isRequired ? Theme.compulsory : Theme.line
                UiText { anchors.centerIn: parent; text: row.isRequired ? "必修" : "选修"; color: row.isRequired ? Theme.requiredInk : Theme.secondary; font.pixelSize: 13 }
            }
            UiText { text: row.scored === false ? "不计分" : row.scored === null ? "待确认" : "计分"; color: Theme.secondary; font.pixelSize: 13; Layout.preferredWidth: 64; horizontalAlignment: Text.AlignHCenter }
            Rectangle {
                objectName: "score_" + row.taskId
                visible: row.scoreText !== ""
                Layout.preferredWidth: 64; Layout.preferredHeight: 24; radius: 6
                color: row.scoreAbnormal ? "#5c1f1f" : Theme.successSurface
                UiText {
                    anchors.centerIn: parent; text: row.scoreText; font.pixelSize: 13
                    color: row.scoreAbnormal ? Theme.error : Theme.success
                }
            }
            Rectangle {
                Layout.preferredWidth: 72; Layout.preferredHeight: 24; radius: 6
                color: row.status === "done" ? Theme.successSurface : row.status === "doing" ? Theme.navigation : Theme.raised
                border.width: row.status === "done" ? 0 : 1; border.color: Theme.line
                UiText {
                    anchors.centerIn: parent; text: row.statusName; font.pixelSize: 13
                    color: row.status === "done" ? Theme.success : row.status === "doing" ? Theme.accent : Theme.secondary
                }
            }
        }
        ToolTip.visible: hovered && (!row.selectable || taskTitle.truncated)
        ToolTip.text: row.selectable ? row.title : "已排除不计分题目，关闭底部选项后可勾选"
        ToolTip.delay: 650
    }
}
