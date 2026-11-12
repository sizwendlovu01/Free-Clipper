"""Start the server: python run.py"""
import uvicorn

from freeclipper import config

if __name__ == "__main__":
    uvicorn.run("freeclipper.main:app", host=config.HOST, port=config.PORT, proxy_headers=True,
                forwarded_allow_ips="*" if config.HOST != "127.0.0.1" else None)
