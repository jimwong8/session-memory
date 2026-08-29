#!/bin/bash
# Deprecated compatibility wrapper: never starts extra KG workers.
exec /usr/bin/python3 /home/jimwong/session-memory-backend/scripts/kg_stale_lock_watchdog.py
