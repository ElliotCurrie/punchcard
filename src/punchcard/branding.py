"""Windows application identity and the shared tray/taskbar icon."""
from pathlib import Path
import sys


APP_USER_MODEL_ID = 'PunchCard.Desktop'


def icon_path() -> Path:
    """Find the bundled icon in both source and PyInstaller onefile runs."""
    root = (Path(sys._MEIPASS) if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[2])
    return root / 'assets' / 'punchcard.ico'


def set_windows_app_identity() -> None:
    """Give native windows a stable taskbar application identity."""
    if sys.platform == 'win32':
        import ctypes
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        if result != 0:
            raise OSError(f'Failed to configure PunchCard Windows AppUserModelID: {result}')
