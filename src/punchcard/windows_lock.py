"""Pause tracking when Windows locks. No microphone, camera or screen capture."""
import ctypes
import logging
import threading

LOG = logging.getLogger(__name__)
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0


class LockMonitor:
    def __init__(self, on_lock, on_unlock=None):
        self.on_lock = on_lock
        self.on_unlock = on_unlock
        self.thread = None
        self.hwnd = None

    def start(self):
        self.thread = threading.Thread(target=self._message_loop, daemon=True,
                                       name='PunchCardLockMonitor')
        self.thread.start()

    def _message_loop(self):
        import win32api
        import win32con
        import win32gui

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK:
                    self.on_lock()
                elif wparam == WTS_SESSION_UNLOCK and self.on_unlock:
                    self.on_unlock()
                # Deliberately don't resume on unlock: user selects Resume.
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        cls = win32gui.WNDCLASS()
        cls.hInstance = win32api.GetModuleHandle(None)
        cls.lpszClassName = 'PunchCardSessionEvents'
        cls.lpfnWndProc = wndproc
        atom = win32gui.RegisterClass(cls)
        self.hwnd = win32gui.CreateWindow(
            atom, 'PunchCard Session Events', 0, 0, 0, 0, 0, 0, 0,
            cls.hInstance, None,
        )
        wtsapi32 = ctypes.WinDLL('wtsapi32', use_last_error=True)
        wtsapi32.WTSRegisterSessionNotification.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        wtsapi32.WTSRegisterSessionNotification.restype = ctypes.c_int
        wtsapi32.WTSUnRegisterSessionNotification.argtypes = [ctypes.c_void_p]
        wtsapi32.WTSUnRegisterSessionNotification.restype = ctypes.c_int

        registered = bool(wtsapi32.WTSRegisterSessionNotification(self.hwnd, NOTIFY_FOR_THIS_SESSION))
        if not registered:
            LOG.warning('Could not register Windows lock notifications (error %s)', ctypes.get_last_error())
        try:
            win32gui.PumpMessages()
        finally:
            if registered:
                wtsapi32.WTSUnRegisterSessionNotification(self.hwnd)
            self.hwnd = None
