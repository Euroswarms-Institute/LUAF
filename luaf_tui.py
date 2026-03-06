from __future__ import annotations
import queue
from pathlib import Path
from typing import Any, Callable
from loguru import logger
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.widgets import Footer, Header, RichLog, Static
    from textual import on
    from textual.worker import Worker
    _TEXTUAL_AVAILABLE = True
except ImportError:
    _TEXTUAL_AVAILABLE = False

_TUI_CSS_PATH = Path(__file__).resolve().parent / 'tui.css'
_log_sink_id: int | None = None

def add_log_sink(log_queue: queue.Queue[str]) -> None:
    global _log_sink_id
    if _log_sink_id is not None:
        return
    def _sink(msg: Any) -> None:
        try:
            r = msg.record
            t = r['time'].strftime('%H:%M:%S')
            lvl = (r['level'].name or 'LOG').ljust(8)
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

def create_luaf_app(run_persistent_fn: Callable[[], None], config: dict[str, Any]) -> type:
    get_creator_pubkey = config['get_creator_pubkey']
    get_solana_balance = config['get_solana_balance']
    load_agents_registry = config['load_agents_registry']
    target_sol = config['target_sol']
    registry_path = config['registry_path']
    rpc_url = config['rpc_url']
    set_stop_requested = config['set_stop_requested']
    get_tui_state = config['get_tui_state']
    log_queue = config['log_queue']
    css = _TUI_CSS_PATH.read_text(encoding='utf-8') if _TUI_CSS_PATH.exists() else ''

    class LUAFApp(App[None]):
        TITLE = 'LUAF'
        SUB_TITLE = 'tokenomics dashboard'
        CSS = css
        BINDINGS = [Binding('q', 'quit', 'Quit', show=True), Binding('s', 'request_stop', 'Stop', show=True)]

        def compose(self) -> ComposeResult:
            with Vertical(id='app-body'):
                yield Header(show_clock=True)
                with Container(id='hero'):
                    yield Static('[bold #e6b422]LUAF[/] · brief → research → build → validate → launch', id='hero-tagline')
                with Container(id='dashboard'):
                    with Horizontal(id='dashboard-strip'):
                        with Vertical(classes='metric-card'):
                            yield Static('BALANCE (SOL)', classes='metric-label')
                            yield Static('—', id='metric-balance', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('TARGET (SOL)', classes='metric-label')
                            yield Static('—', id='metric-target', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAUNCHED UNITS', classes='metric-label')
                            yield Static('0', id='metric-agents', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('STATUS', classes='metric-label')
                            yield Static('Ready', id='metric-status', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('CURRENT BRIEF', classes='metric-label')
                            yield Static('—', id='metric-topic', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAUNCHED THIS RUN', classes='metric-label')
                            yield Static('0', id='metric-session-published', classes='metric-value')
                        with Vertical(classes='metric-card'):
                            yield Static('LAST', classes='metric-label')
                            yield Static('—', id='metric-last', classes='metric-value')
                with Vertical(id='log-section'):
                    yield Static('[bold #7ec8e3]LIVE FEED[/]  [dim] s Stop   q Quit[/]', id='log-title')
                    with ScrollableContainer(id='log-scroll'):
                        yield RichLog(highlight=True, markup=True, id='log')
                yield Footer()

        def on_mount(self) -> None:
            self._refresh_dashboard()
            log_widget = self.query_one('#log', RichLog)
            log_widget.write('[bold]Autonomous mode.[/] Persistent loop started. Log below.')
            add_log_sink(log_queue)
            self._log_drain_timer = self.set_interval(0.15, self._drain_log_queue)
            self._persistent_worker = self.run_worker(run_persistent_fn, thread=True, exclusive=True, name='persistent')

        def _refresh_dashboard(self) -> None:
            topic, sess_pub, last_name, stopped_reason = get_tui_state()
            try:
                self.query_one('#metric-target', Static).update(f'{target_sol:.1f}')
                agents = load_agents_registry()
                self.query_one('#metric-agents', Static).update(str(len(agents)))
                pubkey = get_creator_pubkey()
                balance = get_solana_balance(pubkey, rpc_url) if pubkey else None
            except Exception:
                balance = None
            bal_w = self.query_one('#metric-balance', Static)
            if balance is not None:
                bal_w.update(f'{balance:.4f}')
                bal_w.remove_class('success')
                bal_w.remove_class('warn')
                if target_sol > 0 and balance >= target_sol:
                    bal_w.add_class('success')
                elif target_sol > 0:
                    bal_w.add_class('warn')
            else:
                bal_w.update('—')
            status_w = self.query_one('#metric-status', Static)
            persistent_worker = getattr(self, '_persistent_worker', None)
            if persistent_worker and persistent_worker.is_running:
                status_w.update('Running…')
            elif stopped_reason == 'target':
                status_w.update('Target reached')
            elif stopped_reason == 'stop':
                status_w.update('Stopped')
            else:
                status_w.update('Ready')
            self.query_one('#metric-topic', Static).update(topic or '—')
            self.query_one('#metric-session-published', Static).update(str(sess_pub))
            self.query_one('#metric-last', Static).update(last_name or '—')

        def _drain_log_queue(self) -> None:
            log_widget = self.query_one('#log', RichLog)
            while True:
                try:
                    line = log_queue.get_nowait()
                except queue.Empty:
                    break
                log_widget.write(line)
                log_widget.scroll_end()
            self._refresh_dashboard()

        def action_request_stop(self) -> None:
            set_stop_requested()
            self.query_one('#log', RichLog).write('[dim]Stop requested; loop will exit after current step.[/]')

        @on(Worker.StateChanged)
        def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
            persistent_worker = getattr(self, '_persistent_worker', None)
            if event.worker is not persistent_worker:
                return
            if not event.worker.is_finished:
                return
            timer = getattr(self, '_log_drain_timer', None)
            if timer is not None:
                timer.stop()
                del self._log_drain_timer
            remove_log_sink()
            self._drain_log_queue()
            self._refresh_dashboard()
            log_widget = self.query_one('#log', RichLog)
            if event.worker.error:
                log_widget.write(f'[red]Error: {event.worker.error}[/]')
            else:
                log_widget.write('[green]Persistent finished.[/]')
            self._persistent_worker = None

        def action_quit(self) -> None:
            self.exit()

    return LUAFApp
