#!/bin/bash
# launchd entry point. Keeps cwd correct and logs the exit code.
cd "$(dirname "$0")" || exit 1
/usr/bin/env python3 ./ticketdrops.py "$@"
code=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ticketdrops exited $code" >> logs/launchd.log
exit $code
