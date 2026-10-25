"""Logging setup. Never log passwords, tokens or keys."""
import logging.config


def configure_logging(level: str = "INFO") -> None:
    """Configure structured-ish console logging for the whole process."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                "sqlalchemy.engine": {"level": "WARNING"},
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "google": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
            },
        }
    )
