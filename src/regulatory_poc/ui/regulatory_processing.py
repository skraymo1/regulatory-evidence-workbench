from __future__ import annotations

import asyncio
import time

import streamlit as st


BATCH_SECONDS = 900
DEFAULT_PARALLEL_ROWS = 3
PARALLEL_ROW_CHOICES = [1, 2, 3, 4]
MAX_CONSECUTIVE_FAILURES = 3


def process_requirements(
    service, report: dict, count: int | None = None, parallel: int = DEFAULT_PARALLEL_ROWS,
) -> dict:
    pending = [row for row in report["rows"] if row["status"] in {"pending", "error"}]
    if count is not None:
        pending = pending[:count]
    if not pending:
        st.info("This table has no pending requirements. Saved results were not regenerated.")
        return report
    parallel = max(1, min(max(PARALLEL_ROW_CHOICES), int(parallel)))
    progress = st.progress(0.0, text=f"Starting {len(pending)} requirements, {parallel} at a time")
    started = time.monotonic()

    async def run() -> tuple[list[str], bool, bool]:
        queue = list(pending)
        active: dict[asyncio.Task, tuple[str, float]] = {}
        errors: list[str] = []
        paused = stopped = False
        done = consecutive = 0
        try:
            while queue or active:
                while queue and len(active) < parallel and not stopped and not paused:
                    if time.monotonic() - started >= BATCH_SECONDS:
                        paused = True
                        break
                    row = queue.pop(0)
                    task = asyncio.create_task(service.process_row(
                        report["report_id"], row["baseline"]["key"],
                    ))
                    active[task] = (row["baseline"]["identifier"], time.monotonic())
                if not active:
                    break
                now = time.monotonic()
                running = ", ".join(f"{name} ({int(now - since)}s)" for name, since in active.values())
                progress.progress(
                    done / len(pending),
                    text=f"{done}/{len(pending)} saved; processing {running}",
                )
                finished, _ = await asyncio.wait(
                    set(active), timeout=1, return_when=asyncio.FIRST_COMPLETED,
                )
                for task in finished:
                    identifier, _ = active.pop(task)
                    done += 1
                    try:
                        task.result()
                        consecutive = 0
                    except Exception as exc:  # noqa: BLE001 - show a clean message, not a traceback
                        errors.append(f"{identifier}: {exc}")
                        consecutive += 1
                        stopped = stopped or consecutive >= MAX_CONSECUTIVE_FAILURES
                progress.progress(done / len(pending), text=f"{done}/{len(pending)} saved")
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        return errors, paused, stopped

    try:
        errors, paused, stopped = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - show a clean message, not a traceback
        st.error(f"Processing stopped: {exc} Completed rows are saved; resume to retry.")
        return service.get(report["report_id"])
    result = service.get(report["report_id"])
    if errors:
        failed = "\n".join(f"- {error}" for error in errors)
        if stopped:
            st.error(
                f"Processing stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures, which usually "
                f"indicates a service or sign-in problem. Completed rows are saved; resume to retry.\n\n{failed}"
            )
            return result
        st.warning(
            f"{len(errors)} row(s) failed and were saved as errors; other rows continued. "
            f"Resume to retry the failed rows.\n\n{failed}"
        )
    if paused:
        st.warning(
            "Processing paused at the 15-minute batch limit. Completed rows are saved. "
            "Click Create / resume or Process pending requirements to continue."
        )
    remaining = sum(row["status"] in {"pending", "error"} for row in result["rows"])
    st.info(
        f"Processing saved. {len(result['rows']) - remaining}/{len(result['rows'])} rows processed; "
        f"{remaining} pending/retry. Results remain unreviewed."
    )
    return result
