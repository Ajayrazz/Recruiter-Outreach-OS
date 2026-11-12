"""Background worker: run alongside the Streamlit app.  `python -m recruiter_os.worker`

APScheduler only provides the clock; the durable queue lives in SQLite, so a restart loses nothing and
duplicates nothing (claims are atomic, stale 'Sending' rows are reconciled with Gmail).
It only runs while your Mac is awake and this process is alive.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from recruiter_os.bootstrap import create_app
from recruiter_os.services import followups
from recruiter_os.services.settings import get_setting

log = logging.getLogger("recruiter_os.worker")


def main() -> None:
    app = create_app()
    disp = app.dispatcher()
    sched = BlockingScheduler(timezone="UTC")

    def dispatch() -> None:
        try:
            (app.cfg.data_dir / "worker.heartbeat").write_text(str(int(time.time())))
            r = disp.tick()
            if r["sent"] or r["failed"] or r["held"] or r["recovered"]:
                log.info("tick", extra={"ctx": r})
        except Exception:  # noqa: BLE001
            log.exception("dispatch tick crashed")

    def monitor() -> None:
        with app.db.scope() as s:
            enabled = get_setting(s, "gmail_monitoring_enabled")
        try:
            if enabled:
                log.info("monitor sync", extra={"ctx": app.monitor().sync()})
            with app.db.scope() as s:
                followups.cleanup_stale(s)
                followups.refresh_followups(s)
        except Exception:  # noqa: BLE001
            log.exception("monitor sync crashed")

    sched.add_job(dispatch, "interval", seconds=30, id="dispatch", max_instances=1, coalesce=True,
                  next_run_time=datetime.now(timezone.utc))
    with app.db.scope() as s:
        every = int(get_setting(s, "sync_interval_minutes"))
    sched.add_job(monitor, "interval", minutes=max(5, every), id="monitor", max_instances=1, coalesce=True)
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    log.info("worker started")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("worker stopped")


if __name__ == "__main__":
    main()
