"""Entry point for AI Guardian."""
import uvicorn
from guardian.models import settings
from guardian.proxy.server import app

if __name__ == "__main__":
    uvicorn.run(
        "guardian.proxy.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
