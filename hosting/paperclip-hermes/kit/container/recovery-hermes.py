#!/usr/bin/env python3
"""Run the pinned Hermes CLI without import's best-effort service revival.

Used only in a one-shot container against a stopped home. v0.23.4 import
otherwise calls ensure_gateway_service(), which is inappropriate during a
host restore. Compose starts the services after all recovery checks pass.
"""
from hermes_cli import backup
from hermes_cli.main import main


def leave_gateway_stopped(home):
    pass


if __name__ == '__main__':
    # Fail on an upstream contract change rather than silently permitting a
    # scheduler start from inside the import command.
    if not callable(getattr(backup, '_revive_gateway_after_import', None)):
        raise SystemExit('Unsupported Hermes import implementation')
    backup._revive_gateway_after_import = leave_gateway_stopped
    main()
