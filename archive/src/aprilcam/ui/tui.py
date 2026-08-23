from __future__ import annotations

from typing import Any, Optional


_PIPE_MODE_LABELS = {
    0: "0: Color",
    1: "1: Grayscale",
    2: "2: Flattened",
    3: "3: CLAHE",
    4: "4: Flat+CLAHE",
    5: "5: Threshold",
}


class TagTableTUI:
    """Rich-based TUI dashboard for tag display.

    Completely independent of AprilCam — only needs tag data dicts.
    """

    def __init__(self) -> None:
        self._ema: dict[int, dict[str, float]] = {}
        self._ema_alpha: float = 0.05
        self._live: Optional[Any] = None  # Rich Live instance

    def _ema_smooth(self, tag_id: int, key: str, value: float) -> float:
        """Apply EMA smoothing for display purposes only."""
        if tag_id not in self._ema:
            self._ema[tag_id] = {}
        state = self._ema[tag_id]
        if key not in state:
            state[key] = value
        else:
            alpha = self._ema_alpha
            state[key] = alpha * value + (1.0 - alpha) * state[key]
        return state[key]

    def _build_layout(
        self,
        tags: list[dict],
        *,
        fps: float,
        camera_name: str,
        has_world: bool,
        pipe_mode: int,
    ) -> Any:
        """Build a Rich Columns layout with tag table and status panel."""
        from rich.table import Table
        from rich.columns import Columns

        current_ids: set[int] = set()
        rows: dict[int, dict] = {}

        for tag in tags:
            tag_id = int(tag["id"])
            current_ids.add(tag_id)

            cx = self._ema_smooth(tag_id, "cx", float(tag["cx"]))
            cy = self._ema_smooth(tag_id, "cy", float(tag["cy"]))
            ori = self._ema_smooth(tag_id, "ori", float(tag.get("orientation_yaw", 0.0)))
            spd = self._ema_smooth(tag_id, "spd", float(tag.get("speed_px", 0.0)))

            row: dict = {
                "id": tag_id,
                "cx": cx,
                "cy": cy,
                "ori": ori,
                "spd": spd,
                "visible": True,
            }

            if has_world and "wx" in tag:
                row["wx"] = self._ema_smooth(tag_id, "wx", float(tag["wx"]))
                row["wy"] = self._ema_smooth(tag_id, "wy", float(tag.get("wy", 0.0)))

            rows[tag_id] = row

        # Include stale tags from EMA state
        for old_id, ema_data in self._ema.items():
            if old_id not in current_ids:
                row = {
                    "id": old_id,
                    "cx": ema_data.get("cx", 0.0),
                    "cy": ema_data.get("cy", 0.0),
                    "ori": ema_data.get("ori", 0.0),
                    "spd": 0.0,
                    "visible": False,
                }
                if "wx" in ema_data:
                    row["wx"] = ema_data["wx"]
                if "wy" in ema_data:
                    row["wy"] = ema_data["wy"]
                rows[old_id] = row

        sorted_rows = sorted(rows.values(), key=lambda r: r["id"])

        # --- Tag table ---
        table = Table(title="Tags", border_style="blue", title_style="bold cyan")
        table.add_column("ID", justify="right", style="cyan", width=4)
        table.add_column("CX", justify="right", width=7)
        table.add_column("CY", justify="right", width=7)
        table.add_column("ORI", justify="right", width=8)
        table.add_column("SPD", justify="right", width=8)
        if has_world:
            table.add_column("WX", justify="right", width=8)
            table.add_column("WY", justify="right", width=8)

        for r in sorted_rows:
            style = "dim" if not r["visible"] else ""
            cols = [
                str(r["id"]),
                f"{r['cx']:.1f}",
                f"{r['cy']:.1f}",
                f"{r['ori']:+.1f}\u00b0",
                f"{r['spd']:.1f}",
            ]
            if has_world:
                cols.append(f"{r['wx']:.1f}" if "wx" in r else "\u2014")
                cols.append(f"{r['wy']:.1f}" if "wy" in r else "\u2014")
            table.add_row(*cols, style=style)

        # --- Status table ---
        st = Table(
            title="Status",
            border_style="blue",
            title_style="bold cyan",
            show_header=False,
            min_width=40,
        )
        st.add_column("Key", style="white", width=14)
        st.add_column("Value", width=24)

        st.add_row("Camera", camera_name)
        st.add_row("FPS", f"{fps:.1f}")

        view_label = _PIPE_MODE_LABELS.get(pipe_mode, str(pipe_mode))
        st.add_row("View", f"[cyan]{view_label}[/cyan]")

        if has_world:
            st.add_row("Homography", "[green]OK[/green]")
        else:
            st.add_row("Homography", "[red]NO \u2014 no calibration[/red]")

        visible = sorted(r["id"] for r in sorted_rows if r["visible"])
        missing = sorted(r["id"] for r in sorted_rows if not r["visible"])
        st.add_row("Visible", f"[green]{visible}[/green]")
        if missing:
            st.add_row("Missing", f"[red]{missing}[/red]")

        return Columns([table, st])

    def update(
        self,
        tags: list[dict],
        *,
        fps: float = 0.0,
        camera_name: str = "",
        has_world: bool = False,
        pipe_mode: int = 0,
    ) -> None:
        """Update the TUI display with current tag data.

        Args:
            tags: List of dicts with keys: id, cx, cy, orientation_yaw,
                  speed_px, wx, wy (optional), age (optional).
            fps: Current frames per second.
            camera_name: Camera device name for status display.
            has_world: Whether world coordinates are available.
            pipe_mode: Current pipeline view mode (0-5).
        """
        from rich.live import Live
        from rich.console import Console

        layout = self._build_layout(
            tags,
            fps=fps,
            camera_name=camera_name,
            has_world=has_world,
            pipe_mode=pipe_mode,
        )

        if self._live is None:
            console = Console()
            self._live = Live(
                layout,
                console=console,
                refresh_per_second=15,
                screen=True,
            )
            self._live.start()
        else:
            self._live.update(layout)

    def stop(self) -> None:
        """Stop the TUI display."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def __enter__(self) -> "TagTableTUI":
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
