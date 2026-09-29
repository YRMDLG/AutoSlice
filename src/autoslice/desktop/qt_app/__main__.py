"""运行正式 Qt vNext 开发入口：python -m autoslice.desktop.qt_app。"""

import ctypes
import hashlib
import os
import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from autoslice.desktop.qt_preview.theme import stylesheet

from .window import DesktopWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoSlice")
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())

    identity = (os.environ.get("LOCALAPPDATA", "") + os.environ.get("USERNAME", "")).casefold()
    name = "autoslice-vnext-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    mutex = None
    kernel = None
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\" + name)
        if not mutex:
            QMessageBox.warning(None, "AutoSlice", "无法创建桌面端单实例锁。")
            return 2
        if ctypes.get_last_error() == 183:
            socket = QLocalSocket()
            socket.connectToServer(name)
            if socket.waitForConnected(800):
                socket.write(b"activate")
                socket.waitForBytesWritten(800)
                kernel.CloseHandle(ctypes.c_void_p(mutex))
                return 0
            kernel.CloseHandle(ctypes.c_void_p(mutex))
            QMessageBox.warning(None, "AutoSlice", "已有桌面端正在运行，但无法激活窗口。")
            return 2
    server = QLocalServer(app)
    if not server.listen(name):
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(400):
            socket.write(b"activate")
            socket.waitForBytesWritten(400)
            if kernel and mutex:
                kernel.CloseHandle(ctypes.c_void_p(mutex))
            return 0
        QLocalServer.removeServer(name)
        if not server.listen(name):
            QMessageBox.warning(None, "AutoSlice", "无法确认桌面端单实例状态，请检查已有窗口。")
            if kernel and mutex:
                kernel.CloseHandle(ctypes.c_void_p(mutex))
            return 2

    window = DesktopWindow()
    def activate():
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.readAll()
            connection.disconnectFromServer()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    window.show()
    result = app.exec()
    server.close()
    if kernel and mutex:
        kernel.CloseHandle(ctypes.c_void_p(mutex))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
