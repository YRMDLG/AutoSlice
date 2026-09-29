"""运行可停留的 Qt 视觉预览：python -m autoslice.desktop.qt_preview。"""

import sys

from PySide6.QtWidgets import QApplication

from . import motion
from .theme import stylesheet
from .window import PreviewWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoSlice Qt 视觉预览")
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())
    motion.install_app(app)
    window = PreviewWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
