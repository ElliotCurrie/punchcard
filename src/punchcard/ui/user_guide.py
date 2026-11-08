"""Read-only user guide window."""
from nicegui import ui
from .layout import window_layout

class UserGuideView:
    def __init__(self, desktop, document_path):
        self.desktop = desktop
        self.document_path = document_path

    def register_page(self) -> None:
        # Each NiceGUI route builds controls for its own browser connection.
        @ui.page('/guide')
        def render_user_guide():
            with window_layout(self.desktop, 'guide', 'PUNCHCARD / USER GUIDE', padding=22):
                document = self.document_path
                ui.markdown(document.read_text(encoding='utf-8')).classes('w-full')
