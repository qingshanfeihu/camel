# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""运行农民独立流程。"""

from __future__ import annotations

import asyncio

from INAGENT.data_tools.farmer_ingest import main


if __name__ == "__main__":
    asyncio.run(main())
