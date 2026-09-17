"""Only used by subprocess crash-window verification; never a production option."""

import asyncio
import os
import sys
from pathlib import Path

from harness.platform.config import Settings, Permissions
from harness.scheduler.worker import Worker, build_runtime
from harness.server.composition import Services


async def main():
    data = Path(sys.argv[1])
    services = Services(
        Settings(
            data_dir=data, instance_id="crash-fixture", permissions=Permissions(network=True, process=True)
        )
    )

    def crash(stage, operation):
        fault_tool = sys.argv[2] if len(sys.argv) > 2 else "write_file"
        if stage == "after_tool_before_ledger" and operation["tool_id"] == fault_tool:
            (data / "crash-marker").write_text(operation["id"], encoding="utf-8")
            os._exit(23)

    services.gateway.fault_hook = crash
    await services.open()
    async with build_runtime(services):
        await Worker(services).run()


if __name__ == "__main__":
    asyncio.run(main())
