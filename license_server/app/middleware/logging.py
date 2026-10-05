import logging
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path
        
        response = await call_next(request)
        
        duration_ms = int((time.time() - start_time) * 1000)
        status_code = response.status_code
        
        logger.info(f"{method} {path} {client_ip} {status_code} {duration_ms}ms")
        
        return response
