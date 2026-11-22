import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window
    objectName: "configWindow"
    title: "UnipusAI-Helper 配置"
    width: 820; height: 840
    minimumWidth: 700; minimumHeight: 560
    visible: true
    color: Theme.canvas
    palette.window: Theme.surface
    palette.windowText: Theme.ink
    palette.base: Theme.canvas
    palette.text: Theme.ink
    palette.highlight: Theme.accentFill
    palette.highlightedText: "white"

    // 关键：delegate 里不能直接引用 context property / 调根函数读它，
    // 这里把 editor 挂成窗口属性，delegate 只读 window.* 与 modelData。
    property var ed: configEditor

    property string notice: ""
    property bool noticeOk: true
    property bool allEditing: false
    property string query: ""
    property var revealed: ({})
    property var rowsModel: []

    readonly property var rowSpecs: [
        { key: "username", label: "账号", kind: "text", required: true },
        { key: "password", label: "密码", kind: "password", required: true },
        { key: "api_key", label: "API Key", kind: "secret", required: true },
        { key: "base_url", label: "API 地址", kind: "text", required: true },
        { key: "model", label: "模型名称", kind: "text", required: true },
        { key: "url", label: "登录地址", kind: "text" },
        { key: "target_accuracy", label: "目标正确率", kind: "int" },
        { key: "homework_submit", label: "自动提交", kind: "bool" },
        { key: "homework.allow_force_submit", label: "允许强制提交", kind: "bool" },
        { key: "max_tokens", label: "最大 Token 数", kind: "int" },
        { key: "request_batch_size", label: "每批题数", kind: "int" },
        { key: "temperature", label: "温度", kind: "float" },
        { key: "debug_mode", label: "调试模式", kind: "bool" },
        { key: "screenshot", label: "识图模式", kind: "bool" },
        { key: "tts_engine", label: "TTS 引擎", kind: "text" },
        { key: "tts_voice", label: "TTS 音色", kind: "text" },
        { key: "tts_proxy", label: "TTS 代理", kind: "text" },
        { key: "browser", label: "浏览器", kind: "text" },
        { key: "token_full", label: "Token（可选）", kind: "secret" }
    ]

    readonly property var accountKeys: ["username", "password", "api_key", "token_full",
                                        "base_url", "model", "url"]

    function safeKey(key) { return String(key).replace(/\./g, "_") }

    function pick(source, key) {
        let parts = String(key).split(".")
        let value = source
        for (let i = 0; i < parts.length; ++i) {
            if (value === null || value === undefined) return undefined
            value = value[parts[i]]
        }
        return value
    }

    function isEmpty(value) {
        return value === undefined || value === null || String(value).trim() === ""
    }

    function mask(text) {
        if (text.length <= 4) return "****"
        return text.slice(0, 2) + "****" + text.slice(-2)
    }

    function rebuild() {
        let values = window.ed ? window.ed.values : ({})
        let defaults = window.ed ? window.ed.defaults : ({})
        let rows = []
        for (let i = 0; i < rowSpecs.length; ++i) {
            let spec = rowSpecs[i]
            let raw = pick(values, spec.key)
            let fallback = pick(defaults, spec.key)
            let isBool = spec.kind === "bool"
            let empty = !isBool && isEmpty(raw)
            let display = ""
            if (isBool) display = raw === true ? "开" : "关"
            else if (empty) display = "未设置"
            else display = String(raw)
            if (!isBool && (spec.kind === "secret" || spec.kind === "password")
                    && !window.revealed[spec.key] && !empty) display = mask(String(raw))
            let defaultText = isBool ? (fallback === true ? "开" : "关")
                                     : (isEmpty(fallback) ? "—" : String(fallback))
            rows.push({
                key: spec.key, label: spec.label, kind: spec.kind,
                required: !!spec.required, isBool: isBool,
                empty: empty, display: display, defaultText: defaultText,
                sameAsDefault: String(raw) === String(fallback),
                draftText: isBool ? (raw === true ? "开" : "关") : (empty ? "" : String(raw)),
                defaultRaw: isBool ? (fallback === true ? "true" : "false")
                                   : (isEmpty(fallback) ? "" : String(fallback))
            })
        }
        window.rowsModel = rows
    }

    function matches(row) {
        if (query === "") return true
        return (row.label + " " + row.key + " " + row.display).toLowerCase()
                .indexOf(query.toLowerCase()) >= 0
    }

    function collectValues() {
        let values = ({})
        for (let i = 0; i < window.rowsModel.length; ++i) {
            let row = window.rowsModel[i]
            values[row.key] = row.isBool ? (row.display === "开") : row.draftText
        }
        return values
    }

    function flash(message, ok) {
        window.notice = message
        window.noticeOk = ok
        noticeTimer.restart()
    }

    Timer { id: noticeTimer; interval: 4000; onTriggered: window.notice = "" }

    Component.onCompleted: rebuild()

    Connections {
        target: window.ed
        function onChanged() { window.rebuild() }
        function onSaved(message, ok) {
            window.flash(message, ok)
            if (ok && window.ed.firstRun) window.close()
        }
    }

    Shortcut { sequence: StandardKey.Save; onActivated: window.ed.save(window.collectValues()) }
    Shortcut { sequence: StandardKey.Close; onActivated: window.close() }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 28
        spacing: 14

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 4
            UiText { text: "配置"; font.pixelSize: 26; font.weight: Font.DemiBold }
            UiText {
                text: window.ed && window.ed.firstRun
                      ? "请先填写账号和模型必填项"
                      : "每行直接显示当前值与默认值；「编辑」单改即时保存，「全部编辑」批量改"
                color: Theme.muted; font.pixelSize: 13
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            TextField {
                id: searchInput
                objectName: "configSearch"
                Layout.fillWidth: true; implicitHeight: 36
                placeholderText: "搜索字段名或当前值"
                placeholderTextColor: Theme.muted
                color: Theme.ink; selectionColor: Theme.accentFill
                font.family: Theme.fontFamily; font.pixelSize: 13
                onTextChanged: window.query = text
                background: Rectangle {
                    radius: 7; color: Theme.canvas
                    border.color: searchInput.activeFocus ? Theme.accent : Theme.line
                }
            }
            ActionButton {
                objectName: "editAll"; text: "全部编辑"; quiet: true
                onClicked: window.allEditing = true
            }
            ActionButton {
                objectName: "restoreAll"; text: "全部恢复默认"; quiet: true
                onClicked: restoreDialog.open()
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 26
            color: "transparent"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10
                UiText { Layout.preferredWidth: 168; text: "名称"; color: Theme.muted; font.pixelSize: 12 }
                UiText { Layout.fillWidth: true; text: "当前值"; color: Theme.muted; font.pixelSize: 12 }
                UiText { Layout.preferredWidth: 86; text: "默认值"; color: Theme.muted; font.pixelSize: 12 }
                UiText { Layout.preferredWidth: 148; text: "操作"; color: Theme.muted; font.pixelSize: 12 }
            }
        }

        ScrollView {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

            ColumnLayout {
                width: scroll.availableWidth
                spacing: 2

                Repeater {
                    id: fieldRows
                    model: window.rowsModel

                    delegate: Rectangle {
                        id: rowItem
                        required property var modelData
                        readonly property string fieldKey: modelData.key
                        property bool editing: false
                        property string draft: ""

                        objectName: "row_" + window.safeKey(modelData.key)
                        Layout.fillWidth: true
                        visible: window.matches(modelData)
                        implicitHeight: editing ? 48 : Math.max(42, valueText.implicitHeight + 14)
                        radius: 8
                        color: editing ? Theme.canvas : "transparent"
                        border.color: editing ? Theme.accent : Theme.subtleLine

                        Connections {
                            target: window
                            function onAllEditingChanged() {
                                if (window.allEditing) {
                                    rowItem.draft = rowItem.modelData.draftText
                                    rowItem.editing = true
                                } else {
                                    rowItem.editing = false
                                }
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10; anchors.rightMargin: 10
                            spacing: 10

                            RowLayout {
                                Layout.preferredWidth: 168
                                spacing: 6
                                Rectangle {
                                    width: 6; height: 6; radius: 3
                                    visible: rowItem.modelData.required && rowItem.modelData.empty
                                    color: Theme.error
                                }
                                UiText {
                                    Layout.fillWidth: true
                                    text: rowItem.modelData.label
                                    color: Theme.secondary; font.pixelSize: 13
                                    elide: Text.ElideRight
                                }
                            }

                            UiText {
                                id: valueText
                                objectName: "value_" + window.safeKey(rowItem.modelData.key)
                                Layout.fillWidth: true
                                visible: !rowItem.editing
                                text: rowItem.modelData.display
                                color: rowItem.modelData.empty ? Theme.muted : Theme.ink
                                font.pixelSize: 13
                                elide: Text.ElideRight
                            }

                            TextField {
                                id: editInput
                                objectName: "field_" + window.safeKey(rowItem.modelData.key)
                                Layout.fillWidth: true
                                visible: rowItem.editing
                                text: rowItem.editing ? rowItem.draft : ""
                                implicitHeight: 34
                                color: Theme.ink; selectionColor: Theme.accentFill
                                font.family: Theme.fontFamily; font.pixelSize: 13
                                onTextEdited: rowItem.draft = text
                                Keys.onEscapePressed: {
                                    rowItem.editing = false
                                    window.allEditing = false
                                }
                                onActiveFocusChanged: {
                                    if (!activeFocus && rowItem.editing && !window.allEditing) {
                                        if (window.ed.saveOne(rowItem.fieldKey, rowItem.draft))
                                            window.flash("已保存", true)
                                        rowItem.editing = false
                                    }
                                }
                                background: Rectangle {
                                    radius: 7; color: Theme.surface
                                    border.color: editInput.activeFocus ? Theme.accent : Theme.line
                                }
                            }

                            UiText {
                                objectName: "default_" + window.safeKey(rowItem.modelData.key)
                                Layout.preferredWidth: 86
                                visible: !rowItem.editing
                                text: rowItem.modelData.defaultText
                                color: rowItem.modelData.sameAsDefault ? Theme.muted : Theme.secondary
                                font.pixelSize: 12
                                elide: Text.ElideRight
                            }

                            RowLayout {
                                Layout.preferredWidth: 148
                                spacing: 6

                                ActionButton {
                                    objectName: "edit_" + window.safeKey(rowItem.modelData.key)
                                    visible: !rowItem.editing
                                    text: rowItem.modelData.isBool ? "切换" : "编辑"
                                    quiet: true; implicitHeight: 28
                                    onClicked: {
                                        if (rowItem.modelData.isBool) {
                                            let next = rowItem.modelData.display === "开" ? "false" : "true"
                                            window.ed.saveOne(rowItem.fieldKey, next)
                                        } else {
                                            rowItem.draft = rowItem.modelData.draftText
                                            rowItem.editing = true
                                            editInput.forceActiveFocus()
                                            editInput.selectAll()
                                        }
                                    }
                                }
                                ActionButton {
                                    visible: !rowItem.editing
                                    text: (rowItem.modelData.kind === "secret"
                                           || rowItem.modelData.kind === "password")
                                          ? (window.revealed[rowItem.modelData.key] ? "隐藏" : "显示")
                                          : "恢复默认"
                                    quiet: true; implicitHeight: 28
                                    onClicked: {
                                        let key = rowItem.modelData.key
                                        if (rowItem.modelData.kind === "secret"
                                                || rowItem.modelData.kind === "password") {
                                            let copy = window.revealed
                                            copy[key] = !copy[key]
                                            window.revealed = copy
                                            window.rebuild()
                                        } else {
                                            window.ed.saveOne(key, rowItem.modelData.defaultRaw)
                                        }
                                    }
                                }
                                ActionButton {
                                    visible: rowItem.editing
                                    text: "保存"; primary: true; implicitHeight: 28
                                    onClicked: {
                                        if (window.allEditing) {
                                            rowItem.editing = false
                                        } else if (window.ed.saveOne(rowItem.fieldKey, rowItem.draft)) {
                                            window.flash("已保存", true)
                                            rowItem.editing = false
                                        }
                                    }
                                }
                                ActionButton {
                                    visible: rowItem.editing
                                    text: "取消"; quiet: true; implicitHeight: 28
                                    onClicked: rowItem.editing = false
                                }
                            }
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            UiText {
                Layout.fillWidth: true
                text: window.notice
                color: window.noticeOk ? Theme.success : Theme.error
                font.pixelSize: 13
                elide: Text.ElideRight
            }
            ActionButton {
                objectName: "cancelAll"; visible: window.allEditing; text: "取消"
                quiet: true
                onClicked: window.allEditing = false
            }
            ActionButton {
                objectName: "saveButton"
                text: window.allEditing ? "保存全部" : (window.ed && window.ed.firstRun ? "保存配置" : "保存全部")
                primary: true
                onClicked: {
                    window.ed.save(window.collectValues())
                    window.allEditing = false
                }
            }
        }
    }

    Dialog {
        id: restoreDialog
        objectName: "restoreDialog"
        modal: true
        anchors.centerIn: parent
        width: 460; padding: 24
        header: UiText { text: "全部恢复默认"; font.pixelSize: 20; font.weight: Font.Medium; padding: 24; bottomPadding: 8 }
        background: Rectangle { color: Theme.surface; radius: 16; border.color: Theme.line }
        ColumnLayout {
            width: parent.width; spacing: 18
            UiText {
                Layout.fillWidth: true
                text: "除账号、密码、API Key、Token、API 地址和模型名称外，其余字段将重置为默认值。"
                wrapMode: Text.Wrap; color: Theme.secondary; lineHeight: 1.5
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                Item { Layout.fillWidth: true }
                ActionButton { text: "取消"; onClicked: restoreDialog.reject() }
                ActionButton {
                    text: "恢复默认"; primary: true
                    onClicked: {
                        restoreDialog.accept()
                        let values = window.collectValues()
                        for (let i = 0; i < window.rowsModel.length; ++i) {
                            let row = window.rowsModel[i]
                            if (window.accountKeys.indexOf(row.key) >= 0) continue
                            values[row.key] = row.defaultRaw
                        }
                        window.ed.save(values)
                    }
                }
            }
        }
    }
}