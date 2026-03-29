import argparse

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.helper import get_program_version
from p3lib.launcher import Launcher

from tapo_p110_ev_charger.tapo_p110_ev_charger import gui_main

def main() -> None:
    """@brief Program entry point"""
    uio = UIO()
    options = None
    try:
        parser = argparse.ArgumentParser(description="An app to allow a Tapo P110 smart plug (connected to 13A mains EV charger) to charge your EV.",
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("-d", "--debug",  action='store_true', help="Enable debugging.")
        parser.add_argument("-p", "--port",    type=int, help="The TCP port to start the nicegui server on (default=8080).", default=8080)
        parser.add_argument("-n", "--no_web_launch",  action='store_true', help="Do not open web browser.")
        launcher = Launcher("icon.png", app_name="tapo_p11_ev_charger")
        launcher.addLauncherArgs(parser)

        options = parser.parse_args()

        uio.enableDebug(options.debug)
        uio.logAll(True)
        uio.enableSyslog(True, programName="tapo_p110_ev_charger")

        prog_version = get_program_version('tapo_p110_ev_charger')
        uio.info(f"tapo_p110_ev_charger: V{prog_version}")

        handled = launcher.handleLauncherArgs(options, uio=uio)
        if not handled:

            gui_main(not options.no_web_launch, options.port)

    # If the program throws a system exit exception
    except SystemExit:
        pass
    # Don't print error information if CTRL C pressed
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)

        if options and options.debug:
            raise
        else:
            uio.error(str(ex))

if __name__ in {"__main__", "__mp_main__"}:
    main()
