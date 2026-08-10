import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("BACKEND_PORT", "8000")),
        reload=os.getenv("BACKEND_RELOAD", "false").lower()
        in {"1", "true", "yes", "on"},
    )
