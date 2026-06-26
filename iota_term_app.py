from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static


class IotaTermApp(App):
    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("IOTA terminal app is running.\nPress q to quit.")
        yield Footer()


if __name__ == "__main__":
    app = IotaTermApp()
    app.run()
