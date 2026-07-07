"""Rich terminal UI for download progress and live logging."""

from __future__ import annotations

import shutil
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock

from rich.align import Align
from rich.box import SIMPLE
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text


class LoggerTable:
    """Scrolling event log rendered as a Rich table."""

    def __init__(self, max_rows: int = 5) -> None:
        self._rows: deque[tuple[str, str, str]] = deque(maxlen=max_rows)

    def log(self, event: str, details: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._rows.append((timestamp, event, details))

    def render(self, width: int) -> Panel:
        table = Table(box=SIMPLE, show_header=True, show_edge=True, expand=True)
        table.add_column("[cyan]Time", style="dim", width=max(8, width // 8))
        table.add_column("[cyan]Event", style="bold", width=max(12, width // 5))
        table.add_column("[cyan]Details", width=max(20, width - 24))

        for row in self._rows:
            table.add_row(*row)

        return Panel(
            table,
            title="[bold cyan]Log",
            border_style="bright_black",
            width=width,
        )


class ProgressTracker:
    """Overall file count and per-file byte progress."""

    def __init__(self, batch_label: str = "Album") -> None:
        self._batch_label = batch_label
        self._file_count = 0
        self._overall = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
        )
        self._files = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )
        self._overall_task: TaskID | None = None

    def begin_batch(self, description: str, file_count: int) -> None:
        self._file_count = file_count
        label = description if len(description) <= 24 else f"{description[:21]}..."
        self._overall_task = self._overall.add_task(
            f"[cyan]{label}",
            total=max(file_count, 1),
        )

    def add_file(self, filename: str, total_bytes: int | None) -> TaskID:
        name = filename if len(filename) <= 40 else f"...{filename[-37:]}"
        total = total_bytes if total_bytes and total_bytes > 0 else None
        return self._files.add_task(f"[green]{name}", total=total)

    def update_file(self, task_id: TaskID, completed: int) -> None:
        self._files.update(task_id, completed=completed)

    def finish_file(self, task_id: TaskID | None) -> None:
        if task_id is not None:
            self._files.update(task_id, visible=False)
        if self._overall_task is not None:
            self._overall.advance(self._overall_task)

    def render(self, width: int) -> Table:
        half = max(30, width // 2)
        grid = Table.grid(expand=True)
        grid.add_row(
            Panel(
                self._overall,
                title="[bold cyan]Overall",
                border_style="blue",
                width=half,
            ),
            Panel(
                self._files,
                title=f"[bold cyan]{self._batch_label}",
                border_style="green",
                width=half,
            ),
        )
        return grid


class TerminalUI:
    """Live Rich display combining progress bars and a scrolling log."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._console = Console()
        self._logger = LoggerTable()
        self._progress = ProgressTracker()
        self._live: Live | None = None
        self._start_time = 0.0
        self._paused = False

    def __enter__(self) -> TerminalUI:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    @property
    def live(self) -> Live:
        if self._live is None:
            raise RuntimeError("TerminalUI has not been started")
        return self._live

    def start(self) -> None:
        self._start_time = time.time()
        self._live = Live(self._render(), console=self._console, refresh_per_second=10)
        self._live.start()
        self.log("Started", "Preparing downloads...")

    def stop(self) -> None:
        if self._live is None:
            return
        elapsed = time.time() - self._start_time
        minutes, seconds = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)
        self.log(
            "Finished",
            f"Elapsed: {hours:02d}:{minutes:02d}:{seconds:02d}",
        )
        self._live.update(self._render())
        self._live.stop()
        self._live = None

    def pause(self) -> None:
        if self._live is not None and not self._paused:
            self._live.stop()
            self._paused = True

    def resume(self) -> None:
        if self._live is not None and self._paused:
            self._live.start()
            self._paused = False

    def log(self, event: str, details: str = "") -> None:
        with self._lock:
            self._logger.log(event, details)
            if self._live is not None and not self._paused:
                self._live.update(self._render())

    def begin_batch(self, description: str, file_count: int) -> None:
        with self._lock:
            self._progress.begin_batch(description, file_count)
            if self._live is not None and not self._paused:
                self._live.update(self._render())

    def add_file_task(self, filename: str, total_bytes: int | None) -> TaskID:
        with self._lock:
            task_id = self._progress.add_file(filename, total_bytes)
            if self._live is not None and not self._paused:
                self._live.update(self._render())
            return task_id

    def update_file_task(self, task_id: TaskID, completed: int) -> None:
        with self._lock:
            self._progress.update_file(task_id, completed)
            if self._live is not None and not self._paused:
                self._live.update(self._render())

    def finish_file_task(self, task_id: TaskID | None) -> None:
        with self._lock:
            self._progress.finish_file(task_id)
            if self._live is not None and not self._paused:
                self._live.update(self._render())

    def _panel_width(self) -> int:
        width, _ = shutil.get_terminal_size(fallback=(100, 24))
        return max(60, width - 2)

    def _render(self) -> Group:
        width = self._panel_width()
        header = Panel(
            Align.center(Text("GOFILE DOWNLOADER", style="bold cyan")),
            border_style="cyan",
        )
        footer = Align.left(Text("gofile-downloader", style="dim"))
        return Group(
            header,
            self._progress.render(width),
            self._logger.render(width),
            footer,
        )


def create_terminal_ui() -> TerminalUI | None:
    """Create a TerminalUI when stdout is an interactive terminal."""
    if not Console().is_terminal:
        return None
    return TerminalUI()
