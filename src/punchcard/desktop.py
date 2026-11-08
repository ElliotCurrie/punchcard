"""Native Windows shell: window process, tray and session lock observation."""
import asyncio
import logging
import os
import socket
import subprocess
import sys
import threading
from .settings import ProjectPaths
from .windows_lock import LockMonitor

import zmq
from nicegui import app
from PIL import Image, ImageDraw
import pystray

LOG = logging.getLogger(__name__)


def free_local_port() -> int:
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


class Desktop:
    def __init__(self, web_port: int | None = None, *, on_pause=None,
                 on_windows_locked=None, on_windows_unlocked=None, paths=None):
        self.paths = paths or ProjectPaths()
        self.on_pause = on_pause or (lambda: None)
        self.on_windows_locked = on_windows_locked or (lambda: None)
        self.on_windows_unlocked = on_windows_unlocked or (lambda: None)
        self.lock_monitor: LockMonitor | None = None
        self.web_port = free_local_port() if web_port is None else web_port
        self.command_port = free_local_port()
        self.worker: subprocess.Popen | None = None
        self.tray: pystray.Icon | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        if self.worker and self.worker.poll() is None:
            return
        self.worker = subprocess.Popen(
            [sys.executable, '-u', '-m', 'punchcard.desktop_worker',
             '--web-port', str(self.web_port), '--command-port', str(self.command_port)],
            cwd=str(self.paths.project_dir),
        )
        self.lock_monitor = LockMonitor(self.on_windows_locked, self.on_windows_unlocked)
        self.lock_monitor.start()
        threading.Thread(target=self._run_tray, daemon=True).start()

    def command(self, cmd: str, *, window: str = 'widget', reload: bool = False,
                start_date: str | None = None, end_date: str | None = None):
        """Send a desktop command; export dates travel to the native save dialog."""
        context = zmq.Context.instance()
        socket_ = context.socket(zmq.PUSH)
        socket_.set(zmq.LINGER, 250)
        try:
            socket_.connect(f'tcp://127.0.0.1:{self.command_port}')
            socket_.send_json({'cmd': cmd, 'window': window, 'reload': reload,
                               'start_date': start_date, 'end_date': end_date})
        finally:
            socket_.close()

    def show_widget(self):
        self.command('show', window='widget')

    def show_timesheet(self):
        self.command('show', window='timesheet')

    def show_settings(self):
        self.command('show', window='settings', reload=True)

    def _run_tray(self):
        icon_image = Image.new('RGBA', (64, 64), '#1c2744')
        d = ImageDraw.Draw(icon_image)
        d.rounded_rectangle((12, 9, 52, 55), radius=5, outline='white', width=3)
        for y in (22, 32, 42):
            d.rounded_rectangle((22, y, 29, y+5), radius=1, fill='#ffca75')
            d.line((34, y+3, 45, y+3), fill='white', width=2)

        def schedule(callback):
            if self.loop:
                self.loop.call_soon_threadsafe(callback)

        self.tray = pystray.Icon(
            'PunchCard', icon_image, 'PunchCard',
            menu=pystray.Menu(
                pystray.MenuItem('Open widget', lambda *_: self.show_widget()),
                pystray.MenuItem('Timesheet', lambda *_: self.show_timesheet()),
                pystray.MenuItem('Settings', lambda *_: self.show_settings()),
                pystray.MenuItem('User guide', lambda *_: self.command('show', window='guide')),
                pystray.MenuItem('Pause', lambda *_: schedule(
                    lambda: self.on_pause())),
                pystray.MenuItem('Quit', lambda *_: schedule(app.shutdown)),
            ),
        )
        self.tray.run()

    def stop(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                LOG.exception('Failed to stop tray')
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.worker.kill()
