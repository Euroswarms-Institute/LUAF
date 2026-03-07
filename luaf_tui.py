"""LUAF TUI: profile selection then dashboard with metrics and live log. Uses Rich (no Textual)."""
from __future__ import annotations
import queue
import sys
import threading
import time
from typing import Any, Callable
from loguru import logger

_log_sink_id: int | None = None


def add_log_sink(log_queue: queue.Queue[str]) -> None:
    global _log_sink_id
    if _log_sink_id is not None:
        return

    def _sink(msg: Any) -> None:
        try:
            r = msg.record
            t = r["time"].strftime("%H:%M:%S")
            lvl = (r["level"].name or "LOG").ljust(8)
            log_queue.put(f"{t} | {lvl} | {r['message']}")
        except Exception:
            log_queue.put(str(msg))

    _log_sink_id = logger.add(_sink, level=0)


def remove_log_sink() -> None:
    global _log_sink_id
    if _log_sink_id is not None:
        try:
            logger.remove(_log_sink_id)
        except Exception:
            pass
        _log_sink_id = None


def _profile_select(profile_options: list[dict[str, Any]], on_profile_selected: Callable[[int], None]) -> None:
    """Show ls-style profile selection; call on_profile_selected with chosen index."""
    if not profile_options:
        return
    try:
        import questionary
        choices = [p.get("display_name", p.get("id", "")) for p in profile_options]
        ans = questionary.select("Select profile:", choices=choices).ask()
        if ans is None:
            on_profile_selected(0)
            return
        for i, p in enumerate(profile_options):
            if p.get("display_name", p.get("id", "")) == ans:
                on_profile_selected(i)
                return
        on_profile_selected(0)
    except ImportError:
        print("  Select profile:")
        for i, p in enumerate(profile_options):
            print(f"    {i}  {p.get('display_name', p.get('id', ''))}")
        try:
            raw = input("  Choice [0]: ").strip() or "0"
            idx = max(0, min(len(profile_options) - 1, int(raw)))
        except (ValueError, EOFError, KeyboardInterrupt):
            idx = 0
        on_profile_selected(idx)


def _read_key_nonblocking() -> str | None:
    """Read a single key if available. Returns None if no key. Windows: msvcrt; Unix: select+read (may need raw mode)."""
    try:
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                return ch.decode("utf-8", errors="replace") if isinstance(ch, bytes) else ch
            return None
        else:
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.read(1)
            return None
    except Exception:
        return None


def create_luaf_app(run_persistent_fn: Callable[[], None], config: dict[str, Any]) -> type:
    get_creator_pubkey = config["get_creator_pubkey"]
    get_solana_balance = config["get_solana_balance"]
    load_agents_registry = config["load_agents_registry"]
    target_sol = config["target_sol"]
    rpc_url = config["rpc_url"]
    set_stop_requested = config["set_stop_requested"]
    get_tui_state = config["get_tui_state"]
    log_queue = config["log_queue"]
    profile_options: list[dict[str, Any]] = config.get("profile_options") or []
    on_profile_selected: Callable[[int], None] = config.get("on_profile_selected") or (lambda _: None)

    class LUAFApp:
        """Rich-based TUI: profile select then Live dashboard. Same contract as Textual app (instantiate and .run())."""

        def __init__(self) -> None:
            self._run_fn = run_persistent_fn
            self._quit = False
            self._worker: threading.Thread | None = None
            self._worker_error: BaseException | None = None
            self._log_lines: list[str] = []
            self._max_log_lines = 500

        def run(self) -> None:
            if profile_options:
                _profile_select(profile_options, on_profile_selected)
            self._start_persistent_worker()
            add_log_sink(log_queue)
            self._log_lines.append("[bold]Autonomous mode.[/] Persistent loop started. Log below.")
            try:
                self._run_live_dashboard()
            finally:
                remove_log_sink()
                self._quit = True

        def _start_persistent_worker(self) -> None:
            def _run() -> None:
                try:
                    run_persistent_fn()
                except BaseException as e:
                    self._worker_error = e

            self._worker = threading.Thread(target=_run, daemon=True)
            self._worker.start()

        def _drain_log(self) -> None:
            while True:
                try:
                    line = log_queue.get_nowait()
                    self._log_lines.append(line)
                    if len(self._log_lines) > self._max_log_lines:
                        self._log_lines.pop(0)
                except queue.Empty:
                    break

        def _build_renderable(self) -> Any:
            from rich.console import Group
            from rich.panel import Panel
            from rich.table import Table
            from rich.text import Text

            self._drain_log()
            topic, sess_pub, last_name, stopped_reason = get_tui_state()

            try:
                agents = load_agents_registry()
                n_agents = str(len(agents))
            except Exception:
                n_agents = "0"
            try:
                pubkey = get_creator_pubkey()
                balance = get_solana_balance(pubkey, rpc_url) if pubkey else None
            except Exception:
                balance = None
            if balance is not None:
                bal_str = f"{balance:.4f}"
            else:
                bal_str = "—"
            status = "Ready"
            if self._worker and self._worker.is_alive():
                status = "Running…"
            elif stopped_reason == "target":
                status = "Target reached"
            elif stopped_reason == "stop":
                status = "Stopped"

            table = Table(expand=True, show_header=False)
            for _ in range(14):
                table.add_column(style="dim" if _ % 2 == 0 else "bold cyan", min_width=4)
            brief_short = (topic or "—")[:20] + ("…" if (topic or "") and len(topic or "") > 20 else "")
            table.add_row(
                "BALANCE", bal_str,
                "TARGET", f"{target_sol:.1f}",
                "LAUNCHED", n_agents,
                "STATUS", status,
                "BRIEF", brief_short,
                "RUN", str(sess_pub),
                "LAST", last_name or "—",
            )

            metrics_panel = Panel(table, title="LUAF  brief → research → build → validate → launch", padding=(0, 1))
            try:
                log_text = Text.from_markup("\n".join(self._log_lines[-80:]))
            except Exception:
                log_text = Text.from_plain_text("\n".join(self._log_lines[-80:]), style="dim")
            log_panel = Panel(log_text, title="LIVE FEED  [dim]s Stop  q Quit[/]", padding=(0, 1))
            return Group(metrics_panel, "", log_panel)

        def _run_live_dashboard(self) -> None:
            from rich.console import Console
            from rich.live import Live

            console = Console()
            renderable_holder: list[Any] = [None]
            saved_tty: Any = None
            if sys.platform != "win32" and sys.stdin.isatty():
                try:
                    import termios
                    import tty
                    fd = sys.stdin.fileno()
                    saved_tty = termios.tcgetattr(fd)
                    tty.setcbreak(fd)
                except Exception:
                    saved_tty = None

            def get_renderable() -> Any:
                renderable_holder[0] = self._build_renderable()
                return renderable_holder[0]

            try:
                with Live(get_renderable(), refresh_per_second=8, screen=True, console=console) as live:
                    while not self._quit:
                        time.sleep(0.15)
                        if not self._worker or not self._worker.is_alive():
                            if self._worker_error:
                                self._log_lines.append(f"[red]Error: {self._worker_error}[/]")
                            else:
                                self._log_lines.append("[green]Persistent finished.[/]")
                            self._quit = True
                            break
                        live.update(get_renderable())
                        key = _read_key_nonblocking()
                        if key and key.lower() == "q":
                            self._quit = True
                            break
                        if key and key.lower() == "s":
                            set_stop_requested()
                            self._log_lines.append("[dim]Stop requested; loop will exit after current step.[/]")
                        live.update(get_renderable())
            finally:
                if saved_tty is not None and sys.stdin.isatty():
                    try:
                        import termios
                        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_tty)
                    except Exception:
                        pass

    return LUAFApp
