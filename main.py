from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import io
    import os
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import traceback
    from contextlib import suppress
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired, LoginException
    from utils import lock_file, resource_path, set_root_icon
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH, DATA_DIR

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    # Suppress X11 Input Method registration on Linux to prevent
    # XWayland/Mutter lockups during heavy Tkinter layout updates.
    if sys.platform.startswith("linux") and "XMODIFIERS" not in os.environ:
        os.environ["XMODIFIERS"] = "@im=none"

    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._message: io.StringIO = io.StringIO()

        def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
            self._message.write(message)
            # print(message, file=self._message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            try:
                super().exit(status, message)  # sys.exit(2)
            finally:
                messagebox.showerror("Argument Parser Error", self._message.getvalue())

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        tray: bool
        dump: bool
        open_browser: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    # handle input parameters
    # This check must happen before importing tkinter, so the CLI has no GUI dependency.
    cli_mode = len(sys.argv) > 1 and "cli" in sys.argv[1:]
    if cli_mode:
        from cli import CLIManager

        parser = argparse.ArgumentParser(
            SELF_PATH.name,
            description="A program that allows you to mine timed drops on Twitch.",
        )
        parser.add_argument("--version", action="version", version=f"v{__version__}")
        parser.add_argument("-v", dest="_verbose", action="count", default=0)
        parser.add_argument("--tray", action="store_true")
        parser.add_argument("--log", action="store_true")
        parser.add_argument("--dump", action="store_true")
        # undocumented debug args
        parser.add_argument(
            "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
        )
        parser.add_argument(
            "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
        )
        subparsers = parser.add_subparsers(dest="mode")
        cli_parser = subparsers.add_parser("cli")
        cli_subparsers = cli_parser.add_subparsers(dest="command")
        run_parser = cli_subparsers.add_parser("run")
        run_parser.add_argument("--no-control", action="store_true")
        run_parser.add_argument("--open-browser", action="store_true")
        run_parser.add_argument("--dashboard", action="store_true")
        run_parser.add_argument("--dashboard-host")
        run_parser.add_argument("--dashboard-port", metavar="N|A-B",
                                help="exact port or first free port in range (default: 23450-23500)")
        run_parser.add_argument("--dashboard-token-file", nargs="?", const="", metavar="PATH",
                                help="enable token authentication (default file: DATA_DIR/dashboard.token)")
        run_parser.add_argument("--dashboard-readonly", action="store_true")
        settings_parser = cli_subparsers.add_parser("settings")
        settings_subparsers = settings_parser.add_subparsers(dest="settings_command", required=True)
        settings_subparsers.add_parser("show")
        settings_get_parser = settings_subparsers.add_parser("get")
        settings_get_parser.add_argument("key")
        settings_set_parser = settings_subparsers.add_parser("set")
        settings_set_parser.add_argument("key")
        settings_set_parser.add_argument("value")
        priority_parser = cli_subparsers.add_parser("priority")
        priority_subparsers = priority_parser.add_subparsers(dest="priority_command", required=True)
        priority_subparsers.add_parser("list")
        for action in ("add", "remove"):
            action_parser = priority_subparsers.add_parser(action)
            action_parser.add_argument("game", nargs="+")
        priority_move_parser = priority_subparsers.add_parser("move")
        priority_move_parser.add_argument("game")
        priority_move_parser.add_argument("position")
        exclude_parser = cli_subparsers.add_parser("exclude")
        exclude_subparsers = exclude_parser.add_subparsers(dest="exclude_command", required=True)
        exclude_subparsers.add_parser("list")
        for action in ("add", "remove"):
            action_parser = exclude_subparsers.add_parser(action)
            action_parser.add_argument("game", nargs="+")
        logout_parser = cli_subparsers.add_parser("logout")
        logout_parser.add_argument("--yes", action="store_true")
        auth_parser = cli_subparsers.add_parser("auth")
        auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
        for action in ("status", "backup", "restore"):
            auth_subparsers.add_parser(action)
        import_parser = auth_subparsers.add_parser("import")
        import_parser.add_argument("--from-jar", dest="from_jar")
        ctl_parser = cli_subparsers.add_parser("ctl")
        ctl_parser.add_argument("-y", action="store_true")
        ctl_parser.add_argument("words", nargs=argparse.REMAINDER)
        args = parser.parse_args(namespace=ParsedArgs())
        if isinstance(getattr(args, "game", None), list):
            # multi-word game names work without shell quoting
            args.game = " ".join(args.game)
        if args.mode == "cli" and args.command is None:
            cli_parser.print_help()
            parser.exit(2)
        if args.command in ("settings", "priority", "exclude", "logout", "auth"):
            from cli_commands import run_offline

            sys.exit(run_offline(args))
        if args.command == "ctl":
            from cli_commands import run_control_client

            sys.exit(run_control_client(args))
        if args.command == "run":
            from dashboard import Dashboard, DashboardError, resolve_dashboard_config

            try:
                dashboard_config = resolve_dashboard_config(args)
            except argparse.ArgumentError as exc:
                parser.error(str(exc))
            except DashboardError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
    else:
        import tkinter as tk
        from tkinter import messagebox

        # NOTE: parser output is shown via message box
        # we also need a dummy invisible window for the parser
        root = tk.Tk()
        root.overrideredirect(True)
        root.withdraw()
        set_root_icon(root, resource_path("icons/pickaxe.ico"))
        root.update()
        parser = Parser(
            SELF_PATH.name,
            description="A program that allows you to mine timed drops on Twitch.",
        )
        parser.add_argument("--version", action="version", version=f"v{__version__}")
        parser.add_argument("-v", dest="_verbose", action="count", default=0)
        parser.add_argument("--tray", action="store_true")
        parser.add_argument("--log", action="store_true")
        parser.add_argument("--dump", action="store_true")
        # undocumented debug args
        parser.add_argument(
            "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
        )
        parser.add_argument(
            "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
        )
        args = parser.parse_args(namespace=ParsedArgs())
        args.open_browser = False

    # load settings
    try:
        settings = Settings(args)
    except Exception:
        if cli_mode:
            traceback.print_exc()
        else:
            messagebox.showerror(
                "Settings error",
                f"There was an error while loading the settings file:\n\n{traceback.format_exc()}",
            )
        sys.exit(4)
    if not cli_mode:
        # dummy window isn't needed anymore
        root.destroy()
        # get rid of unneeded objects
        del root, parser

    # client run
    async def main():
        # set language
        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass

        # handle logging stuff
        if settings.logging_level > logging.DEBUG:
            # redirect the root logger into a NullHandler, effectively ignoring all logging calls
            # that aren't ours. This always runs, unless the main logging level is DEBUG or lower.
            logging.getLogger().addHandler(logging.NullHandler())
        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)
        if settings.log:
            handler = logging.FileHandler(LOG_PATH)
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)
        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        exit_status = 0
        if cli_mode:
            client = Twitch(
                settings,
                ui_factory=lambda twitch: CLIManager(twitch, open_browser=args.open_browser),
            )
            logger.info("CLI mode started")
        else:
            client = Twitch(settings)
        loop = asyncio.get_running_loop()
        if sys.platform == "linux" or (cli_mode and sys.platform != "win32"):
            loop.add_signal_handler(signal.SIGINT, lambda *_: client.gui.close())
            loop.add_signal_handler(signal.SIGTERM, lambda *_: client.gui.close())
        elif cli_mode and sys.platform == "win32":
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(
                signal.SIGINT,
                lambda *_: loop.call_soon_threadsafe(client.gui.close),
            )
        dashboard = None
        control = None
        dashboard_start_failed = False
        if cli_mode and not args.no_control:
            from control import ControlServer

            control = ControlServer(client.gui, DATA_DIR)
            try:
                await control.start()
            except (OSError, RuntimeError) as exc:
                # the control channel is optional: keep mining without "cli ctl"
                control = None
                client.print(f"control channel disabled ({exc}); \"cli ctl\" is unavailable")
        if cli_mode and dashboard_config.enabled:
            if not dashboard_start_failed:
                dashboard = Dashboard(client.gui, client, dashboard_config)
                try:
                    await dashboard.start()
                except DashboardError as exc:
                    dashboard_start_failed = True
                    exit_status = 1
                    client.print(str(exc))
        try:
            if not dashboard_start_failed:
                await client.run()
            if cli_mode and not client.gui.close_requested:
                exit_status = 1
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            client.print(_("error", "captcha"))
        except Exception as exc:
            exit_status = 1
            client.prevent_close()
            if cli_mode and isinstance(exc, LoginException):
                client.print(str(exc))
            else:
                client.print("Fatal error encountered:\n")
                client.print(traceback.format_exc())
        finally:
            if sys.platform == "linux" or (cli_mode and sys.platform != "win32"):
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            elif cli_mode and sys.platform == "win32":
                signal.signal(signal.SIGINT, previous_sigint)
            client.print(_("gui", "status", "exiting"))
            if cli_mode:
                try:
                    async def shutdown_cli():
                        if control is not None and control.server is not None:
                            try:
                                await asyncio.wait_for(control.stop(), timeout=2)
                            except asyncio.TimeoutError:
                                logger.warning("Control shutdown timed out")
                        if dashboard is not None:
                            stop_task = asyncio.create_task(dashboard.stop())
                            done, pending = await asyncio.wait({stop_task}, timeout=2)
                            if pending:
                                stop_task.cancel()
                                logger.warning("Dashboard shutdown timed out")
                            else:
                                try:
                                    stop_task.result()
                                except Exception:
                                    logger.exception("Dashboard shutdown failed")
                        await client.shutdown()

                    await asyncio.wait_for(shutdown_cli(), timeout=10)
                except asyncio.TimeoutError:
                    client.print("Shutdown timed out.")
                    try:
                        if client._session is not None and not client._jar_load_failed:
                            from constants import COOKIES_PATH

                            from auth_session import save_jar

                            save_jar(client._session.cookie_jar, COOKIES_PATH)
                    except Exception:
                        logger.exception("Failed to save cookies after shutdown timeout")
            else:
                await client.shutdown()
        if not client.gui.close_requested:
            # user didn't request the closure
            client.gui.tray.change_icon("error")
            if cli_mode:
                client.print(_("status", "terminated").splitlines()[0])
            else:
                client.print(_("status", "terminated"))
            client.gui.status.update(_("gui", "status", "terminated"))
            # notify the user about the closure
            client.gui.grab_attention(sound=True)
        await client.gui.wait_until_closed()
        # save the application state
        # NOTE: we have to do it after wait_until_closed,
        # because the user can alter some settings between app termination and closing the window
        client.save(force=True)
        client.gui.stop()
        client.gui.close_window()
        sys.exit(exit_status)

    file = None
    cli_exit_status = 0
    try:
        # use lock_file to check if we're not already running
        success, file = lock_file(LOCK_PATH)
        if not success:
            # already running - exit
            if cli_mode:
                cli_exit_status = 3
            else:
                sys.exit(3)

        if cli_mode:
            if cli_exit_status == 0:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                task = loop.create_task(main())
                try:
                    loop.run_until_complete(task)
                except KeyboardInterrupt:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        loop.run_until_complete(task)
                    cli_exit_status = 0
                except SystemExit as exc:
                    cli_exit_status = int(exc.code or 0)
                finally:
                    pending = asyncio.all_tasks(loop)
                    for pending_task in pending:
                        pending_task.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()
        else:
            asyncio.run(main())
    finally:
        if file is not None:
            file.close()
    if cli_mode:
        # DNS resolution can outlive an interrupted aiohttp request. Its executor would make
        # asyncio.run wait during interpreter shutdown even after the client has closed.
        with suppress(Exception):
            logging.shutdown()
        with suppress(Exception):
            sys.stdout.flush()
        with suppress(Exception):
            sys.stderr.flush()
        os._exit(cli_exit_status)
