"""Start the server: python run.py"""
import uvicorn

from freeclipper import config

if __name__ == "__main__":
    behind_proxy = config.HOST != "127.0.0.1"
    uvicorn.run(
        "freeclipper.main:app",
        host=config.HOST,
        port=config.PORT,
        # Behind a platform proxy (Railway), trust X-Forwarded-Proto so HTTPS is detected.
        proxy_headers=True,
        forwarded_allow_ips="*" if behind_proxy else None,
        # On SIGTERM: stop accepting requests, then the app returns running jobs to the queue.
        timeout_graceful_shutdown=15,
        log_level="info",
    )
