"""Shared NiceGUI desktop window layout, navigation and time labels."""
import os
from contextlib import contextmanager
from nicegui import ui

def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds//3600:02d}:{(seconds//60)%60:02d}:{seconds%60:02d}'


def format_timesheet_total(seconds: float) -> str:
    """A compact daily total without second-level precision."""
    minutes = max(0, int(seconds) // 60)
    hours, remainder = divmod(minutes, 60)
    return f'{hours}h {remainder}m' if hours else f'{remainder}m'


def open_view(desktop, name: str, *, reload: bool = False):
    if os.name == 'nt':
        desktop.command('show', window=name, reload=reload)
    else:
        ui.navigate.to('/' + ('' if name == 'widget' else name), new_tab=True)


def add_window_styles(*, padding: int = 16):
    """Fixed desktop title bar, with only the content pane allowed to scroll."""
    ui.add_css(f"""
        html, body {{ height: 100%; overflow: hidden; }}
        body {{ background: #f6f7fb; margin: 0; }}
        .nicegui-content {{
            padding: 0 !important; height: 100vh; max-height: 100vh;
            min-height: 0; overflow: hidden !important;
        }}
        .pc-window {{
            display: flex !important; flex-direction: column; gap: 0 !important;
            width: 100%; height: 100%; max-height: 100vh;
            min-height: 0; overflow: hidden;
        }}
        .pc-titlebar {{
            flex: 0 0 auto; width: 100%; min-height: 48px;
            padding: 5px {padding}px; margin: 0;
            border-bottom: 1px solid #dce3ed;
            background: #f6f7fb; z-index: 10;
        }}
        .pc-window-body {{
            flex: 1 1 0; min-height: 0; width: 100%;
            overflow-y: auto; overflow-x: hidden;
            overscroll-behavior: contain;
            padding: {padding}px;
        }}
        /* QScrollArea gives long Settings forms a real, independently scrollable
           viewport while keeping the custom title bar fixed. */
        .pc-settings-scroll {{
            flex: 1 1 0 !important; min-height: 0 !important;
            height: 0 !important; width: 100%;
            overflow: hidden !important;
        }}
        .pc-settings-scroll .q-scrollarea__content {{
            width: 100%; min-width: 0;
        }}
        /* Card-like Settings sections and genuinely compact tooltips. */
        .pc-settings-expansion {{
            background: white;
            border: 1px solid #e5e9f0;
            border-radius: 10px !important;
            box-shadow: 0 3px 12px rgba(32, 52, 75, 0.10);
            overflow: hidden;
        }}
        .pc-settings-expansion .q-item {{ min-height: 54px; }}
        .q-tooltip {{
            font-size: 10px !important; line-height: 1.25 !important;
            padding: 3px 6px !important;
        }}
        .pc-tempo-toggle .q-toggle__label {{
            font-size: 12px; color: #526173;
        }}
        .pywebview-drag-region {{
            cursor: move; user-select: none; -webkit-user-select: none;
            min-height: 34px; display: flex; align-items: center;
        }}
    """)


def window_header(desktop, name: str, title: str, *, status: bool = False):
    """Shared drag-enabled titlebar; buttons must stay outside the drag region."""
    with ui.row().classes('pc-titlebar items-center justify-between no-wrap'):
        ui.label(title).classes(
            'pywebview-drag-region flex-grow font-semibold tracking-widest '
            'text-blue-grey-9'
        ).tooltip('Drag to move window')
        badge = ui.badge('Paused', color='grey') if status else None
        if name != 'widget':
            ui.button(icon='remove', on_click=lambda: desktop.command('minimize', window=name)).props(
                'flat dense round size=sm'
            ).tooltip('Minimize')
        ui.button(icon='close', on_click=lambda: desktop.command('hide', window=name)).props(
            'flat dense round size=sm'
        ).tooltip('Close')
    return badge


@contextmanager
def window_layout(desktop, name: str, title: str, *, padding: int = 16,
                  status: bool = False, scroll_area: bool = False):
    """Keep native window controls visible while the page body scrolls."""
    add_window_styles(padding=padding)
    with ui.column().classes('pc-window'):
        badge = window_header(desktop, name, title, status=status)
        if scroll_area:
            # Settings expansions can become taller than their window.
            # A dedicated QScrollArea maintains a fixed viewport while
            # keeping the custom drag-enabled header visible.
            with ui.scroll_area().classes('pc-window-body pc-settings-scroll'):
                with ui.column().classes('w-full gap-3'):
                    yield badge
        else:
            with ui.column().classes('pc-window-body gap-3'):
                yield badge


