import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Design pass informed by unslop, Damir Sokolovsky (@heydamir), CC BY 4.0.
ApplicationWindow {
    id: window
    objectName: "workbenchWindow"
    minimumWidth: 960; minimumHeight: 640
    width: Math.max(minimumWidth, Math.min(1280, Screen.desktopAvailableWidth - 64))
    height: Math.max(minimumHeight, Math.min(840, Screen.desktopAvailableHeight - 64))
    visible: true
    title: "UnipusAI-Helper"
    color: Theme.canvas
    palette.window: Theme.surface
    palette.windowText: Theme.ink
    palette.base: Theme.canvas
    palette.text: Theme.ink
    palette.button: Theme.raised
    palette.buttonText: Theme.ink
    palette.highlight: Theme.accentFill
    palette.highlightedText: "white"
    property int currentPage: 0
    readonly property int pageMargin: width < 1100 ? 20 : 28
    readonly property bool compactHeight: height < 720
    readonly property var appState: workbench.state
    readonly property var summary: workbench.summary
    readonly property bool busy: !!appState.busy

    property bool returnToTasksWhenIdle: false

    function finishProcessingNavigation() {
        if (!returnToTasksWhenIdle || !appState || appState.busy) return
        returnToTasksWhenIdle = false
        // Leave stopped or failed runs available for inspection in the log.
        if (appState.status === "就绪" && !appState.error) currentPage = 0
    }
    onAppStateChanged: finishProcessingNavigation()

    function startProcessing(action) {
        if (!workbench.start(action)) return
        returnToTasksWhenIdle = true
        currentPage = 1
        // A fast operation may have finished before start() returned.
        finishProcessingNavigation()
    }

    function resetTaskPosition() { if (taskList) { taskList.currentIndex = -1; taskList.positionViewAtBeginning() } }
    function focusFirstTask() {
        if (taskList.count === 0) return
        taskList.currentIndex = 0
        taskList.positionViewAtBeginning()
        Qt.callLater(function() {
            const first = taskList.itemAtIndex(0)
            if (first) first.focusTask()
        })
    }
    function clearTaskFilters() {
        searchInput.clear()
        workbench.resetFilters()
        resetTaskPosition()
        searchInput.forceActiveFocus()
    }

    Shortcut { sequences: [StandardKey.Find]; onActivated: { window.currentPage = 0; searchInput.forceActiveFocus(); searchInput.selectAll() } }
    Shortcut { sequence: StandardKey.Quit; onActivated: window.close() }

    RowLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.preferredWidth: window.width < 1100 ? 224 : 244
            Layout.minimumWidth: Layout.preferredWidth; Layout.maximumWidth: Layout.preferredWidth
            Layout.fillHeight: true; Layout.minimumHeight: 0
            color: Theme.sidebar
            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: Theme.line }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 16; spacing: 4
                RowLayout {
                    Layout.fillWidth: true; Layout.leftMargin: 4; Layout.topMargin: window.compactHeight ? 12 : 20
                    Layout.bottomMargin: window.compactHeight ? 24 : 32; spacing: 8
                    Image { source: "icons/app.svg"; sourceSize.width: 80; sourceSize.height: 80; Layout.preferredWidth: 32; Layout.preferredHeight: 32 }
                    UiText { objectName: "projectName"; text: "UnipusAI-Helper"; font.pixelSize: window.width < 1100 ? 16 : 17; font.weight: Font.DemiBold; Layout.fillWidth: true }
                }
                NavItem { objectName: "tasksPage"; Layout.fillWidth: true; text: "课程任务"; iconName: "list"; selected: window.currentPage === 0; onClicked: window.currentPage = 0 }
                NavItem { objectName: "logsPage"; Layout.fillWidth: true; text: "运行日志"; iconName: "logs"; selected: window.currentPage === 1; onClicked: window.currentPage = 1 }
                Rectangle { Layout.fillWidth: true; height: 1; color: Theme.line; Layout.topMargin: 16; Layout.bottomMargin: 12 }
                RowLayout {
                    Layout.fillWidth: true; Layout.leftMargin: 8; Layout.rightMargin: 8; Layout.bottomMargin: 8
                    UiText { text: "课程目录"; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                    UiText { text: workbench.units.length ? workbench.units.length + " 个单元" : "待扫描"; color: Theme.muted; font.pixelSize: 13 }
                }
                NavItem {
                    objectName: "allUnits"; Layout.fillWidth: true; text: "全部单元"; iconName: "layers"
                    selected: window.currentPage === 0 && workbench.activeUnit === "all"
                    count: summary.total > 0 ? summary.total : ""
                    onClicked: { workbench.chooseUnit("all"); window.currentPage = 0; window.resetTaskPosition() }
                }
                ListView {
                    id: unitList; objectName: "unitList"
                    Layout.fillHeight: true; Layout.fillWidth: true; Layout.minimumHeight: 0
                    Layout.topMargin: 8; clip: true; spacing: 4; model: workbench.units
                    delegate: Button {
                        id: unitButton
                        required property var modelData
                        readonly property bool selected: workbench.activeUnit === modelData.name && window.currentPage === 0
                        objectName: "unit_" + modelData.name
                        width: unitList.width; height: 60
                        leftPadding: 12; rightPadding: 12; hoverEnabled: true
                        Accessible.name: modelData.name + "，已完成 " + modelData.done + " / " + modelData.total
                        onClicked: { workbench.chooseUnit(modelData.name); window.currentPage = 0; window.resetTaskPosition() }
                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                        background: Rectangle {
                            radius: 8; color: unitButton.selected ? Theme.navigation : unitButton.hovered ? Theme.hover : "transparent"
                            border.width: unitButton.activeFocus ? 1 : 0; border.color: Theme.accent
                        }
                        contentItem: ColumnLayout {
                            spacing: 6
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText { text: unitButton.modelData.name; font.pixelSize: 14; font.weight: Font.Medium; Layout.fillWidth: true }
                                UiText { text: unitButton.modelData.done + "/" + unitButton.modelData.total; font.pixelSize: 13; color: Theme.secondary; font.features: { "tnum": 1 } }
                            }
                            RowLayout {
                                Layout.fillWidth: true; spacing: 8
                                UiText { text: (unitButton.modelData.total - unitButton.modelData.done) + " 项未完成"; font.pixelSize: 13; color: Theme.muted; Layout.fillWidth: true }
                                ProgressTrack { Layout.preferredWidth: 68; implicitHeight: 4; value: unitButton.modelData.done / Math.max(1, unitButton.modelData.total); Accessible.ignored: true }
                            }
                        }
                        ToolTip.visible: hovered && modelData.title !== ""
                        ToolTip.text: modelData.title
                        ToolTip.delay: 650
                    }
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                }
                Rectangle {
                    Layout.fillWidth: true; Layout.preferredHeight: 1
                    Layout.topMargin: 12; Layout.bottomMargin: 8; color: Theme.line
                }
                ColumnLayout {
                    objectName: "githubInfo"
                    Layout.fillWidth: true; Layout.minimumWidth: 0
                    Layout.leftMargin: 8; Layout.rightMargin: 8; spacing: 8
                    RowLayout {
                        Layout.fillWidth: true; spacing: 8
                        Icon { name: "github"; Layout.preferredWidth: 18; Layout.preferredHeight: 18 }
                        UiText { objectName: "githubLabel"; text: "GitHub 地址"; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                    }
                    TextEdit {
                        objectName: "githubAddress"
                        Layout.fillWidth: true; Layout.minimumWidth: 0
                        text: "github.com/YSJohnson/UnipusAI-Helper"
                        readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText
                        wrapMode: TextEdit.Wrap
                        font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.Normal
                        font.hintingPreference: Font.PreferFullHinting; renderType: TextEdit.NativeRendering
                        color: Theme.muted; selectedTextColor: "white"; selectionColor: Theme.accentFill
                        Accessible.name: "GitHub 地址：" + text
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
            Layout.leftMargin: window.pageMargin; Layout.rightMargin: window.pageMargin; spacing: 0
            RowLayout {
                Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 64 : 72
                Layout.topMargin: window.compactHeight ? 16 : 24; Layout.bottomMargin: window.compactHeight ? 16 : 20; spacing: 24
                ColumnLayout {
                    Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 4
                    UiText {
                        text: window.currentPage === 1 ? "运行日志" : workbench.activeUnit === "all" ? "全部课程任务" : workbench.activeUnit
                        font.pixelSize: window.compactHeight ? 28 : 30; font.weight: Font.DemiBold; Layout.fillWidth: true
                    }
                    UiText { visible: window.currentPage === 0 && summary.unitTitle !== ""; text: summary.unitTitle; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                }
                AccountSummary {
                    objectName: "accountDetails"; Layout.fillWidth: false; Layout.preferredWidth: window.width < 1100 ? 296 : 352
                    Layout.minimumWidth: 296; Layout.maximumWidth: Layout.preferredWidth
                    account: window.appState.account; model: window.appState.model; status: window.appState.status
                    busy: window.busy; ready: !!window.appState.ready; hasError: !!window.appState.error
                }
            }
            CourseOverview {
                objectName: "courseOverview"; visible: window.currentPage === 0
                Layout.fillWidth: true; Layout.bottomMargin: window.compactHeight ? 12 : 16
                counts: summary.counts; compact: window.width < 1100 || window.compactHeight
            }
            RowLayout {
                Layout.fillWidth: true; Layout.bottomMargin: window.compactHeight ? 12 : 16; spacing: 12
                ActionButton {
                    objectName: "scanAll"; visible: window.currentPage === 0; text: "扫描全部单元"; iconName: "refresh"; implicitHeight: window.compactHeight ? 36 : 40
                    primary: true; leftPadding: 20; rightPadding: 20
                    hint: "读取当前教材全部单元的任务"; enabled: window.appState.ready && !window.busy; onClicked: workbench.start("scan")
                }
                ActionButton {
                    objectName: "scanUnit"; visible: window.currentPage === 0; text: "扫描当前单元"; iconName: "layers"; implicitHeight: window.compactHeight ? 36 : 40
                    hint: "读取课程浏览器当前所在的单元"; enabled: window.appState.ready && !window.busy; onClicked: workbench.start("scan_unit")
                }
                ActionButton {
                    objectName: "processPage"; visible: window.currentPage === 0; text: "处理当前页"; iconName: "page"; implicitHeight: window.compactHeight ? 36 : 40
                    hint: "直接处理课程浏览器中已打开的题目"; enabled: window.appState.ready && !window.busy; onClicked: quickDialog.open()
                }
                UiText { visible: window.currentPage === 1; text: (window.appState.logs || []).length + " 条日志"; font.pixelSize: 13; color: Theme.muted }
                Item { Layout.fillWidth: true }
                ActionButton { objectName: "copyLogs"; visible: window.currentPage === 1; enabled: (window.appState.logs || []).length > 0; text: "复制日志"; iconName: "copy"; implicitHeight: 40; onClicked: workbench.copyLogs() }
            }
            Rectangle {
                visible: !!window.appState.error || workbench.notice !== ""
                Layout.fillWidth: true; implicitHeight: noticeText.implicitHeight + 24; Layout.bottomMargin: 12
                color: Theme.surface; radius: 8; border.color: Theme.line
                RowLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 12
                    UiText { id: noticeText; text: workbench.notice || window.appState.error || ""; color: window.appState.error ? Theme.error : Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
                    ActionButton { visible: workbench.notice !== ""; iconName: "close"; quiet: true; Accessible.name: "关闭提示"; onClicked: workbench.dismissNotice() }
                }
            }
            Rectangle {
                objectName: "taskSurface"; visible: window.currentPage === 0
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0; Layout.minimumWidth: 0
                color: Theme.surface; radius: 12; border.color: Theme.line
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 1; spacing: 0
                    Item {
                        Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 48 : 56
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16; spacing: 12
                            RowLayout {
                                spacing: 8
                                Repeater {
                                    model: [{key:"all",title:"全部"},{key:"pending",title:"未完成"},{key:"required",title:"必修"},{key:"done",title:"已完成"}]
                                    Button {
                                        id: filterButton
                                        required property var modelData
                                        readonly property bool selected: workbench.activeFilter === modelData.key
                                        objectName: "filter_" + modelData.key
                                        implicitWidth: filterText.implicitWidth + 20; implicitHeight: 32; hoverEnabled: true
                                        onClicked: { workbench.chooseFilter(modelData.key); window.resetTaskPosition() }
                                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                                        Accessible.name: modelData.title
                                        background: Rectangle { radius: 7; color: filterButton.selected ? Theme.navigation : filterButton.hovered ? Theme.hover : "transparent"; border.width: filterButton.selected || filterButton.activeFocus ? 1 : 0; border.color: filterButton.activeFocus ? Theme.accent : Theme.selectionLine }
                                        contentItem: UiText { id: filterText; text: filterButton.modelData.title + "  " + summary.counts[filterButton.modelData.key]; font.pixelSize: 13; font.weight: filterButton.selected ? Font.DemiBold : Font.Medium; color: filterButton.modelData.key === "required" ? Theme.compulsory : filterButton.selected ? Theme.ink : Theme.secondary; horizontalAlignment: Text.AlignHCenter }
                                    }
                                }
                            }
                            Item { Layout.fillWidth: true }
                            TextField {
                                id: searchInput; objectName: "taskSearch"
                                Layout.preferredWidth: window.width < 1100 ? 152 : 240; Layout.minimumWidth: 120
                                implicitHeight: 32; placeholderText: "搜索任务"; color: Theme.ink; placeholderTextColor: Theme.muted
                                font.family: Theme.fontFamily; font.weight: Font.Medium; font.pixelSize: 13
                                font.hintingPreference: Font.PreferFullHinting; renderType: Text.NativeRendering
                                leftPadding: 32; rightPadding: 28; selectByMouse: true
                                Accessible.name: "搜索任务"; onTextChanged: { workbench.search(text); window.resetTaskPosition() }
                                Keys.onEscapePressed: { if (text !== "") clear(); else window.focusFirstTask() }
                                Keys.onReturnPressed: window.focusFirstTask()
                                Keys.onEnterPressed: window.focusFirstTask()
                                Keys.onDownPressed: window.focusFirstTask()
                                background: Rectangle { radius: 7; color: Theme.raised; border.width: 1; border.color: searchInput.activeFocus ? Theme.accent : Theme.line }
                                Icon { x: 8; anchors.verticalCenter: parent.verticalCenter; name: "search"; width: 16; height: 16 }
                                ActionButton { objectName: "clearSearch"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: 28; height: 28; leftPadding: 6; rightPadding: 6; visible: searchInput.text !== ""; quiet: true; iconName: "close"; Accessible.name: "清空搜索"; hint: "清空搜索（Esc）"; onClicked: { searchInput.clear(); searchInput.forceActiveFocus() } }
                            }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 36; color: Theme.raised
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 24; anchors.rightMargin: 16; spacing: 12
                            UiCheck {
                                objectName: "selectAllTasks"; text: "全选 · " + summary.selectableVisible + " 项"
                                hint: "仅选择当前列表中的可选任务，不影响其他单元或被筛选隐藏的选择"
                                checkState: summary.allChecked ? Qt.Checked : summary.someChecked ? Qt.PartiallyChecked : Qt.Unchecked
                                enabled: !window.busy && summary.selectableVisible > 0; onClicked: workbench.toggleVisible()
                            }
                            Item { Layout.fillWidth: true }
                            ActionButton { objectName: "selectPending"; text: "选择未完成必修"; accented: true; quiet: true; implicitHeight: 32; hint: "只调整当前列表，不影响隐藏任务的选择"; enabled: !window.busy && summary.selectableVisible > 0; onClicked: workbench.selectVisible("pending") }
                            Rectangle { width: 1; height: 16; color: Theme.line }
                            ActionButton { objectName: "clearSelection"; text: "清空全部选择"; hint: "清空所有单元的选择，包括筛选隐藏的任务"; quiet: true; implicitHeight: 32; enabled: !window.busy && summary.selected > 0; onClicked: workbench.clearSelection() }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 32; color: Theme.surface
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.line }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 60; anchors.rightMargin: 24; spacing: 16
                            UiText { text: "任务名称"; color: Theme.muted; font.pixelSize: 13; Layout.fillWidth: true }
                            UiText { text: "要求"; color: Theme.muted; font.pixelSize: 13; Layout.preferredWidth: 48; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: "计分"; color: Theme.muted; font.pixelSize: 13; Layout.preferredWidth: 64; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: "状态"; color: Theme.muted; font.pixelSize: 13; Layout.preferredWidth: 72; horizontalAlignment: Text.AlignHCenter }
                        }
                    }
                    ListView {
                        id: taskList; objectName: "taskList"
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0; Layout.bottomMargin: 8
                        clip: true; model: taskModel; currentIndex: -1
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: TaskRow { width: taskList.width; editable: !window.busy }
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        ColumnLayout {
                            anchors.centerIn: parent; width: Math.min(400, parent.width - 64); spacing: 12
                            visible: taskList.count === 0
                            UiText { text: summary.total ? "没有匹配的任务" : window.appState.ready ? "还没有课程任务" : "等待 U 校园登录"; font.pixelSize: 18; font.weight: Font.Medium; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter }
                            UiText { text: summary.total ? "试试其他关键词，或清除搜索与筛选" : window.appState.ready ? "在课程浏览器中打开目录，然后扫描课程" : "请在课程浏览器中完成登录"; color: Theme.muted; font.pixelSize: 13; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap }
                            ActionButton {
                                objectName: "resetTaskFilters"; visible: summary.total > 0
                                Layout.alignment: Qt.AlignHCenter; text: "清除搜索与筛选"; hint: "保留当前单元和已选任务"
                                onClicked: window.clearTaskFilters()
                            }
                        }
                    }
                }
            }
            Rectangle {
                visible: window.currentPage === 1; Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0
                color: Theme.surface; radius: 12; border.color: Theme.line
                LogView { id: activity; objectName: "allActivity"; anchors.fill: parent; anchors.margins: 12; entries: window.appState.logs || [] }
                ActionButton {
                    objectName: "latestLogs"; visible: !activity.followTail && !activity.atYEnd && activity.count > 0
                    anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 24
                    text: "回到最新"; onClicked: { activity.followTail = true; activity.followLatest() }
                }
            }
            Rectangle {
                objectName: "taskActions"; visible: window.currentPage === 0 || window.busy
                Layout.fillWidth: true; Layout.preferredHeight: window.compactHeight ? 64 : 76
                Layout.topMargin: window.compactHeight ? 12 : 16; Layout.bottomMargin: 16
                radius: 12; color: Theme.surface; border.color: Theme.subtleLine
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 16; spacing: window.width < 1100 ? 16 : 20
                    Rectangle {
                        visible: window.width >= 1100; width: 28; height: 28; radius: 6
                        color: window.busy ? Theme.navigation : summary.selected > 0 ? Theme.accentFill : Theme.raised
                        Icon { anchors.centerIn: parent; name: window.busy ? "play" : "check"; width: 20; height: 20; white: true; visible: window.busy || summary.selected > 0 }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 4
                        UiText {
                            text: window.busy ? (window.appState.progress.total > 0 ? "已完成 " + window.appState.progress.completed + " / " + window.appState.progress.total + " 项" : window.appState.status) : summary.selected ? "已选择 " + summary.selected + " 项任务" : "选择需要处理的任务"
                            font.pixelSize: 16; font.weight: Font.DemiBold; Layout.fillWidth: true
                        }
                        UiText {
                            objectName: "selectionDetails"
                            visible: !window.busy
                            text: summary.selectedHidden > 0 ? "当前列表 " + summary.selectedVisible + " 项，另有 " + summary.selectedHidden + " 项已选"
                                  : summary.selectedUnits > 1 ? "来自 " + summary.selectedUnits + " 个单元" : "点击整行勾选 · 空格切换"
                            font.pixelSize: 13; color: summary.selectedHidden > 0 ? Theme.secondary : Theme.muted; Layout.fillWidth: true
                            HoverHandler { id: selectionHint }
                            ToolTip.visible: selectionHint.hovered
                            ToolTip.text: summary.selectedHidden > 0 ? "另有 " + summary.selectedHidden + " 项已选任务位于其他单元或被当前筛选隐藏，开始处理时也会包含这些任务" : "点击整行勾选；↑↓ 移动；空格或回车切换；Ctrl+A 全选当前列表"
                            ToolTip.delay: 650
                        }
                        ProgressTrack {
                            visible: window.busy && window.appState.progress.total > 0
                            Layout.fillWidth: true; Layout.maximumWidth: 240; implicitHeight: 4
                            value: window.appState.progress.completed / Math.max(1, window.appState.progress.total)
                        }
                    }
                    UiCheck { objectName: "excludeUngraded"; visible: window.currentPage === 0; text: "排除不计分题目"; hint: "开启后取消并禁止选择明确不计分的任务，计分待确认的任务仍可选择"; checked: !!window.appState.excludeUngraded; enabled: !window.busy; onClicked: workbench.excludeUngraded(checked) }
                    Rectangle { visible: window.currentPage === 0; width: 1; height: 24; color: Theme.line }
                    ActionButton {
                        objectName: "startTasks"; Layout.preferredWidth: window.width < 1100 ? 136 : 164; implicitHeight: window.compactHeight ? 40 : 44
                        text: window.appState.status === "正在停止" ? "正在停止…" : window.busy ? "停止处理" : "开始处理"
                        iconName: window.busy ? "stop" : "play"; primary: !window.busy; destructive: window.busy
                        enabled: window.appState.status !== "正在停止" && (window.busy || (window.appState.ready && summary.selected > 0))
                        onClicked: window.busy ? workbench.stop() : window.startProcessing("run")
                    }
                }
            }
            Item { visible: window.currentPage === 1 && !window.busy; Layout.preferredHeight: 24 }
        }
    }
    Dialog {
        id: quickDialog; objectName: "quickDialog"; modal: true
        closePolicy: Popup.CloseOnEscape
        onOpened: cancelQuick.forceActiveFocus()
        anchors.centerIn: parent; width: 456; padding: 24
        header: UiText { text: "处理当前页"; font.pixelSize: 20; font.weight: Font.Medium; padding: 24; bottomPadding: 8 }
        onAccepted: window.startProcessing("quick")
        background: Rectangle { color: Theme.surface; radius: 16; border.color: Theme.line }
        ColumnLayout {
            width: parent.width; spacing: 24
            UiText { Layout.fillWidth: true; text: "确认处理浏览器当前打开的题目？"; wrapMode: Text.Wrap; lineHeight: 1.5; color: Theme.secondary }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                ActionButton { id: cancelQuick; objectName: "cancelQuick"; text: "取消"; onClicked: quickDialog.reject() }
                ActionButton { objectName: "confirmQuick"; text: "处理当前页"; primary: true; enabled: window.appState.ready && !window.busy; onClicked: quickDialog.accept() }
            }
        }
    }
}
