"""Run the whole application: ``python -m app``."""

import asyncio

from app.main import main

if __name__ == "__main__":
    asyncio.run(main())