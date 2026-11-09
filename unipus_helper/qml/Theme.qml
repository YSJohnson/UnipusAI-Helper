pragma Singleton
import QtQuick

QtObject {
    // The supplied reference uses a permanent charcoal appearance.
    readonly property bool dark: true
    readonly property string fontFamily: Qt.application.font.family
    readonly property color canvas: "#151517"
    readonly property color surface: "#242426"
    readonly property color sidebar: "#222224"
    readonly property color raised: "#2c2c2f"
    readonly property color hover: "#343437"
    readonly property color line: "#3d3d41"
    readonly property color subtleLine: "#303034"
    readonly property color ink: "#f4f4f6"
    readonly property color secondary: "#d1d1d6"
    readonly property color muted: "#b0b0b8"
    readonly property color accent: "#77b4ff"
    readonly property color accentFill: "#0071e8"
    readonly property color accentPressed: "#005ec6"
    readonly property color navigation: "#203b5c"
    readonly property color selection: "#263346"
    readonly property color selectionHover: "#30415a"
    readonly property color selectionLine: "#3d506a"
    readonly property color track: "#4a4a50"
    readonly property color success: "#b4e2c2"
    readonly property color successSurface: "#22442d"
    readonly property color error: "#f0aaa5"
    readonly property color warning: "#e2bf86"
    readonly property color compulsory: "#FF8000"
    readonly property color requiredInk: "#24180c"
}
