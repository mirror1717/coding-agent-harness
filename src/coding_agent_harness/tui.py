"""Minimal Textual TUI for the governed coding agent harness."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static


class HarnessTUI(App):
    """Minimal TUI that displays run status and collects approvals."""

    BINDINGS = [("q", "quit", "Quit"), ("a", "approve", "Approve"), ("r", "reject", "Reject")]

    def __init__(self) -> None:
        super().__init__()
        self._status: str = "Ready"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#status", Static).update(f"Status: {self._status}")

    def action_approve(self) -> None:
        self.query_one("#status", Static).update("Status: Approved")

    def action_reject(self) -> None:
        self.query_one("#status", Static).update("Status: Rejected")
