import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window
    objectName: "configWindow"
    title: "UnipusAI-Helper 配置"
    width: 820; height: 840
    minimumWidth: 600; minimumHeight: 520
    visible: true
    color: Theme.canvas
    palette.window: Theme.surface
    palette.windowText: Theme.ink
    palette.base: Theme.canvas
    palette.text: Theme.ink
    palette.highlight: Theme.accentFill
    palette.highlightedText: "white"
    property string notice: ""
    property bool noticeOk: true

    function saveSettings() {
        let values = {"token_full": tokenInput.text, "debug_mode": debugCheck.checked,
                      "screenshot": screenshotCheck.checked,
                      "answer_api_enabled": answerApiCheck.checked,
                      "skip_record_mode": skipRecordCombo.currentValue || "off"}
        for (let i = 0; i < fields.count; ++i) {
            let row = fields.itemAt(i)
            values[row.fieldKey] = row.value
        }
        configEditor.save(values)
    }
    Connections {
        target: configEditor
        function onSaved(message, ok) { window.notice = message; window.noticeOk = ok; if (ok && configEditor.firstRun) window.close() }
    }
    Shortcut { sequence: StandardKey.Save; onActivated: window.saveSettings() }
    Shortcut { sequence: "Escape"; onActivated: window.close() }
    Shortcut { sequence: StandardKey.Close; onActivated: window.close() }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 28
        spacing: 18
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            UiText { text: "配置"; font.pixelSize: 26; font.weight: Font.DemiBold }
            UiText { text: configEditor.firstRun ? "请先填写账号和密码（大模型接口为可选项）" : "设置账号、模型和浏览器，保存后重新启动生效"; color: Theme.muted; font.pixelSize: 13 }
        }
        ScrollView {
            id: scroll
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: scroll.availableWidth
                spacing: 16
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: form.implicitHeight + 40
                    radius: 10; color: Theme.surface
                    border.color: Theme.subtleLine
                    ColumnLayout {
                        id: form
                        anchors.fill: parent; anchors.margins: 20
                        spacing: 14
                        UiText { text: "连接设置"; font.pixelSize: 16; font.weight: Font.DemiBold }
                        Repeater {
                            id: fields
                            model: [
                                { key: "username", label: "账号 *", hint: "输入 U校园AI版账号" },
                                { key: "password", label: "密码 *", hint: "输入 U校园AI版密码" },
                                { key: "url", label: "登录地址", hint: "U校园登录地址" },
                                { key: "api_key", label: "API Key", hint: "可选，留空时仅用官方答案（需 AI 的题将跳过）" },
                                { key: "base_url", label: "API 地址", hint: "可选，兼容 OpenAI 的接口地址" },
                                { key: "model", label: "模型名称", hint: "可选，对应接口的模型名称" },
                                { key: "max_tokens", label: "最大 Token 数", hint: "32768" },
                                { key: "temperature", label: "温度（0–2）", hint: "0.3" },
                                { key: "record_score_threshold", label: "录音、朗读达标分", hint: "70（0–100）" },
                                { key: "score_threshold", label: "其余题型达标分", hint: "80（0–100）" },
                                { key: "target_accuracy", label: "目标正确率（实验性）", hint: "100（关闭），或填 85 / 80-90" },
                                { key: "tts_engine", label: "录音题 TTS 引擎", hint: "auto / edge / sapi / off" },
                                { key: "tts_voice", label: "TTS 音色", hint: "留空使用默认英语音色" },
                                { key: "tts_proxy", label: "TTS 代理", hint: "可选，例如 http://127.0.0.1:7890" },
                                { key: "browser", label: "浏览器", hint: "edge（默认）/ chrome" }
                            ]
                            delegate: RowLayout {
                                required property var modelData
                                property string fieldKey: modelData.key
                                property alias value: input.text
                                Layout.fillWidth: true
                                spacing: 16
                                UiText {
                                    Layout.preferredWidth: 150
                                    text: modelData.label; color: Theme.secondary
                                    font.pixelSize: 13
                                }
                                TextField {
                                    id: input
                                    objectName: "field_" + modelData.key
                                    Layout.fillWidth: true
                                    implicitHeight: 38
                                    text: configEditor.values[modelData.key] == null ? "" : String(configEditor.values[modelData.key])
                                    echoMode: modelData.key === "password" || modelData.key === "api_key" ? TextInput.Password : TextInput.Normal
                                    placeholderText: modelData.hint
                                    placeholderTextColor: Theme.muted
                                    color: Theme.ink; selectionColor: Theme.accentFill
                                    font.family: Theme.fontFamily; font.pixelSize: 14
                                    Accessible.name: modelData.label
                                    background: Rectangle {
                                        radius: 7; color: Theme.canvas
                                        border.color: input.activeFocus ? Theme.accent : Theme.line
                                    }
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; Layout.alignment: Qt.AlignTop
                            spacing: 16
                            UiText { Layout.preferredWidth: 150; Layout.alignment: Qt.AlignTop; Layout.topMargin: 8; text: "跟读跳过（实验性）"; color: Theme.secondary; font.pixelSize: 13 }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 4
                                ComboBox {
                                    id: skipRecordCombo
                                    objectName: "field_skip_record_mode"
                                    Layout.fillWidth: true
                                    implicitHeight: 38
                                    textRole: "label"
                                    valueRole: "value"
                                    model: [
                                        { value: "off", label: "不跳过（正常作答）" },
                                        { value: "follow_aloud", label: "仅跳过跟读与朗读（保留自由口语）" },
                                        { value: "all", label: "跳过全部录音题（含自由口语）" }
                                    ]
                                    currentIndex: Math.max(0, indexOfValue(String(configEditor.values.skip_record_mode || "off")))
                                    font.family: Theme.fontFamily; font.pixelSize: 14
                                    Accessible.name: "跟读/录音跳过"
                                    contentItem: UiText {
                                        leftPadding: 12; rightPadding: skipRecordCombo.indicator.width + 12
                                        text: skipRecordCombo.displayText
                                        font: skipRecordCombo.font
                                        color: Theme.ink
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    indicator: UiText {
                                        x: skipRecordCombo.width - width - 12
                                        y: skipRecordCombo.topPadding + (skipRecordCombo.availableHeight - height) / 2
                                        text: "▾"
                                        color: Theme.muted
                                        font.pixelSize: 13
                                    }
                                    background: Rectangle {
                                        radius: 7; color: Theme.canvas
                                        border.color: skipRecordCombo.activeFocus || skipRecordCombo.popup.visible ? Theme.accent : Theme.line
                                    }
                                    delegate: ItemDelegate {
                                        width: skipRecordCombo.width
                                        highlighted: skipRecordCombo.highlightedIndex === index
                                        contentItem: UiText {
                                            text: modelData.label
                                            color: Theme.ink
                                            font.pixelSize: 14
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                        background: Rectangle {
                                            color: highlighted ? Theme.hover : Theme.surface
                                        }
                                    }
                                    popup: Popup {
                                        y: skipRecordCombo.height + 4
                                        width: skipRecordCombo.width
                                        padding: 4
                                        contentItem: ListView {
                                            clip: true
                                            implicitHeight: contentHeight
                                            model: skipRecordCombo.popup.visible ? skipRecordCombo.delegateModel : null
                                            currentIndex: skipRecordCombo.highlightedIndex
                                        }
                                        background: Rectangle {
                                            radius: 8; color: Theme.surface; border.color: Theme.line
                                        }
                                    }
                                }
                                UiText { text: "实验性功能（默认关闭，尚未覆盖所有教材实测）：目标正确率仅作用于客观题，开启后自动联动放宽重做下限并守住新版 60% 最低提交线；开启跟读跳过后纯跟读任务将保留未完成状态。"; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; Layout.alignment: Qt.AlignTop
                            spacing: 16
                            UiText { Layout.preferredWidth: 150; Layout.alignment: Qt.AlignTop; Layout.topMargin: 8; text: "Token（可选）"; color: Theme.secondary; font.pixelSize: 13 }
                            ScrollView {
                                Layout.fillWidth: true; Layout.preferredHeight: 100
                                clip: true
                                background: Rectangle { radius: 7; color: Theme.canvas; border.color: tokenInput.activeFocus ? Theme.accent : Theme.line }
                                TextArea {
                                    id: tokenInput
                                    objectName: "field_token_full"
                                    text: configEditor.values.token_full || ""
                                    placeholderText: "留空使用登录会话；也可粘贴 __token 或 5.0 Authorization"
                                    placeholderTextColor: Theme.muted
                                    color: Theme.ink; selectionColor: Theme.accentFill
                                    font.family: Theme.fontFamily; font.pixelSize: 14
                                    wrapMode: TextEdit.Wrap
                                    Accessible.name: "Token（可选）"
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; spacing: 16
                            UiText { Layout.preferredWidth: 150; text: "调试模式"; color: Theme.secondary; font.pixelSize: 13 }
                            UiCheck { id: debugCheck; objectName: "field_debug_mode"; text: "开启调试输出"; checked: !!configEditor.values.debug_mode; Layout.fillWidth: true }
                        }
                        RowLayout {
                            Layout.fillWidth: true; spacing: 16
                            UiText { Layout.preferredWidth: 150; text: "识图模式"; color: Theme.secondary; font.pixelSize: 13 }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 4
                                UiCheck { id: screenshotCheck; objectName: "field_screenshot"; text: "题目截图发给模型（需支持识图）"; checked: !!configEditor.values.screenshot; Layout.fillWidth: true }
                                UiText { text: "开启后发送练习截图，消耗较多 token。依赖图片的口语题需要使用支持识图的模型。"; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; spacing: 16
                            UiText { Layout.preferredWidth: 150; text: "官方答案接口"; color: Theme.secondary; font.pixelSize: 13 }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 4
                                UiCheck { id: answerApiCheck; objectName: "field_answer_api_enabled"; text: "启用官方答案接口（实验性）"; checked: configEditor.values.answer_api_enabled !== false; Layout.fillWidth: true }
                                UiText { text: "默认开启。客观题优先使用平台官方答案，主观题仍由 AI 作答，未命中或异常时自动回落。可与「目标正确率」配合使用，控分更准确。"; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                            }
                        }
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            UiText { text: window.notice; color: window.noticeOk ? Theme.success : Theme.error; Layout.fillWidth: true; font.pixelSize: 13 }
            ActionButton { objectName: "saveButton"; text: "保存配置"; primary: true; onClicked: window.saveSettings() }
        }
    }
}
