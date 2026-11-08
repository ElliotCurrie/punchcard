"""Dedicated pywebview process, adapted from Conductor's worker."""
import argparse
import json
import logging
import threading
import time
import urllib.request
from collections import deque
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import webview
import zmq

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument('--web-port', type=int, required=True)
parser.add_argument('--command-port', type=int, required=True)
args = parser.parse_args()
base = f'http://127.0.0.1:{args.web_port}'

# NiceGUI's startup hook may run before the HTTP listener is ready.
for attempt in range(100):
    try:
        with urllib.request.urlopen(base + '/health', timeout=0.2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(0.1)
else:
    raise RuntimeError('PunchCard UI did not become ready')

command_queue = deque()
queue_lock = threading.Lock()
windows = {}


def listen():
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.PULL)
    sock.bind(f'tcp://127.0.0.1:{args.command_port}')
    try:
        while True:
            msg = sock.recv_json()
            with queue_lock:
                command_queue.append(msg)
    finally:
        sock.close()


def make_close_handler(name):
    def handler():
        windows.pop(name, None)
    return handler


def create_window(name, *, reload: bool = False):
    if name in windows:
        windows[name].show()
        # show() only unhides; explicitly restore minimized windows as well.
        windows[name].restore()
        if reload:
            windows[name].load_url(base + ('/' if name == 'widget' else f'/{name}'))
        return
    # All PunchCard windows use the same native-window styling. Dragging is
    # limited to the custom titlebar (not the form inputs or ticket tree).
    frameless = dict(frameless=True, easy_drag=False, shadow=True,
                     background_color='#f6f7fb')
    configs = {
        'widget': dict(url=base + '/', width=385, height=445, on_top=True,
                       resizable=False, **frameless),
        'timesheet': dict(url=base + '/timesheet', width=940, height=710,
                          resizable=True, min_size=(600, 450), **frameless),
        'tickets': dict(url=base + '/tickets', width=630, height=750,
                        resizable=True, min_size=(450, 470), **frameless),
        'guide': dict(url=base + '/guide', width=760, height=720,
                       resizable=True, min_size=(480, 430), **frameless),
        'settings': dict(url=base + '/settings', width=610, height=770,
                         resizable=True, min_size=(440, 410), **frameless),
    }
    window = webview.create_window(f'PunchCard — {name.title()}', **configs[name])
    window.events.closed += make_close_handler(name)
    windows[name] = window


def export_csv(start_date: str | None = None, end_date: str | None = None):
    """Save the selected calendar range through the native file dialog."""
    window = windows.get('timesheet') or windows.get('widget')
    if window is None:
        raise RuntimeError('No PunchCard window is available for saving')
    if bool(start_date) != bool(end_date):
        raise ValueError('Both export dates must be provided')
    if start_date and end_date:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end:
            raise ValueError('End date must be on or after start date')
        url = base + '/exports/sessions.csv?' + urlencode({'start': start.isoformat(),
                                                             'end': end.isoformat()})
        filename = f'punchcard-{start.isoformat()}-to-{end.isoformat()}.csv'
    else:
        url = base + '/exports/sessions.csv'
        filename = f'punchcard-{date.today().isoformat()}.csv'
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = response.read()
    result = window.create_file_dialog(
        webview.FileDialog.SAVE,
        save_filename=filename,
        file_types=('CSV files (*.csv)',),
    )
    if not result:  # User cancelled the dialog.
        return
    destination = Path(result[0] if isinstance(result, (tuple, list)) else result)
    if destination.suffix.lower() != '.csv':
        destination = destination.with_suffix('.csv')
    destination.write_bytes(payload)
    log.info('PunchCard timesheet exported to %s (%s bytes)', destination, len(payload))


def process_commands():
    while True:
        with queue_lock:
            messages = list(command_queue)
            command_queue.clear()
        for message in messages:
            name = message.get('window')
            cmd = message.get('cmd')
            if name not in ('widget', 'timesheet', 'settings', 'tickets', 'guide'):
                continue
            try:
                if cmd == 'show':
                    create_window(name, reload=bool(message.get('reload', False)))
                elif cmd == 'hide' and name in windows:
                    windows[name].hide()
                elif cmd == 'minimize' and name in windows:
                    windows[name].minimize()
                elif cmd == 'export_csv':
                    export_csv(message.get('start_date'), message.get('end_date'))
            except Exception as exc:
                log.exception('Window command failed: %s', message)
                # Surface export failures to the user; don't silently lose data.
                if cmd == 'export_csv':
                    try:
                        window = windows.get('timesheet') or windows.get('widget')
                        if window:
                            window.evaluate_js(f'alert({json.dumps("CSV export failed: " + str(exc))})')
                    except Exception:
                        log.exception('Failed to show export error')
        time.sleep(0.06)


threading.Thread(target=listen, daemon=True).start()
threading.Thread(target=process_commands, daemon=True).start()

# Hidden root keeps pywebview running when users close all visible windows.
root = webview.create_window('PunchCard Host', html='<html></html>', hidden=True)
windows['__root__'] = root
create_window('widget')
webview.settings['ALLOW_DOWNLOADS'] = True
webview.start(debug=False)
