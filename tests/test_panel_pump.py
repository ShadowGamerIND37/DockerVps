"""Runtime regression tests for the provisioning progress panel."""

from __future__ import annotations

import asyncio


def test_panel_pump_start_accepts_its_task_name():
    """Starting creation must not fail while starting the progress pump.

    ``asyncio.ensure_future(coro, name=...)`` raises ``TypeError`` on every
    supported Python version.  The pump must use ``asyncio.create_task``,
    whose API accepts a task name.

    Use a private event loop rather than pytest-asyncio's global loop: older
    synchronous tests in this suite create their own implicit default loop.
    """
    from bot import PanelPump

    class FakePanel:
        def __init__(self):
            self.steps = []

        async def step(self, index, state, detail=""):
            self.steps.append((index, state, detail))

    async def exercise():
        panel = FakePanel()
        pump = PanelPump(panel, 2)
        pump.start()
        try:
            assert pump._task is not None
            assert pump._task.get_name() == "ui-panel-pump"

            pump.emit(0, "running", "Creating container")
            pump.emit(1, "done", "VPS ready")
            await pump.stop(complete=False)

            assert pump.events == [
                (0, "running", "Creating container"),
                (1, "done", "VPS ready"),
            ]
            assert panel.steps[-1] == (1, "done", "VPS ready")
        finally:
            if pump._task is not None:
                pump._task.cancel()
                await asyncio.gather(pump._task, return_exceptions=True)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(exercise())
    finally:
        loop.close()
