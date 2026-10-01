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
    from exceptions import CaptchaRequired
    from utils import lock_file, resource_path, set_root_icon
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH

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
        run_parser.add_argument("--open-browser", action="store_true")
        args = parser.parse_args(namespace=ParsedArgs())
        if args.mode == "cli" and args.command is None:
            cli_parser.print_help()
            parser.exit(2)
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
        if sys.platform == "linux":
            loop.add_signal_handler(signal.SIGINT, lambda *_: client.gui.close())
            loop.add_signal_handler(signal.SIGTERM, lambda *_: client.gui.close())
        try:
            await client.run()
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            client.print(_("error", "captcha"))
        except Exception:
            exit_status = 1
            client.prevent_close()
            client.print("Fatal error encountered:\n")
            client.print(traceback.format_exc())
        finally:
            if sys.platform == "linux":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            client.print(_("gui", "status", "exiting"))
            if cli_mode:
                try:
                    await asyncio.wait_for(client.shutdown(), timeout=10)
                except asyncio.TimeoutError:
                    client.print("Shutdown timed out.")
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
