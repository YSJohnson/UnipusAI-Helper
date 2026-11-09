import QtQuick

Item {
    id: icon
    property string name: "list"
    property bool white: false
    implicitWidth: 18
    implicitHeight: 18
    Accessible.ignored: true
    Image {
        anchors.fill: parent
        sourceSize.width: 36
        sourceSize.height: 36
        source: "icons/" + icon.name + ((icon.white || Theme.dark) ? "-light" : "") + ".svg"
        fillMode: Image.PreserveAspectFit
    }
}
