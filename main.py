import os
import sys
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Settings, translations, the download database and cookies all live under
# resources/ and are opened with relative paths, so run from the app folder
# whatever directory the app was launched from.
os.chdir(_app_dir())

from app.views.pyside.main_window import PySideMainWindow  # noqa: E402


def main():
    app = QApplication(sys.argv)
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)

    app_icon = QIcon("resources/img/icono.ico")
    app.setWindowIcon(app_icon)
    
    # The window shows itself after the startup community dialog resolves
    window = PySideMainWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
