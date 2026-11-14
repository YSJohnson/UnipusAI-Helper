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
        let values = {"token_full": tokenInput.text, "debug_mode": debugCheck.checked}
        for (let i = 0; i < fields.count; ++i) {
            let row = fields.itemAt(i)
            values[row.fieldKey] = row.value
        }
        configEditor.save(values)
    }
    Connections {
        target: configEditor
        function onSaved(message, ok) { window.notice = message; window.noticeOk = ok }
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
            UiText { text: "设置账号、模型和浏览器"; color: Theme.muted; font.pixelSize: 13 }
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
                                { key: "username", label: "账号", hint: "输入 U校园AI版账号" },
                                { key: "password", label: "密码", hint: "输入 U校园AI版密码" },
                                { key: "url", label: "登录地址", hint: "U校园登录地址" },
                                { key: "api_key", label: "API Key", hint: "输入 API Key" },
                                { key: "base_url", label: "API 地址", hint: "兼容 OpenAI 的接口地址" },
                                { key: "model", label: "模型名称", hint: "输入模型名称" },
                                { key: "max_tokens", label: "最大 Token 数", hint: "32768" },
                                { key: "temperature", label: "温度（0–2）", hint: "0.3" },
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