import QtQuick

Rectangle {
    id: track
    property real value: 0
    implicitHeight: 6
    radius: height / 2
    color: Theme.track
    Accessible.role: Accessible.ProgressBar
    Accessible.name: "完成进度 " + Math.round(value * 100) + "%"
    Rectangle {
        height: track.height
        width: track.width * Math.max(0, Math.min(1, track.value))
        radius: track.radius
        color: Theme.accent
    }
}
