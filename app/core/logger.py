import logging
from pathlib import Path


def configure_logging(
    log_level: str,
    log_directory: Path,
) -> None:
    """
    Настраивает логирование в консоль и файл.
    """

    log_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_file = log_directory / "chappie.log"

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=(
            "%(asctime)s | "
            "%(levelname)-8s | "
            "%(name)s | "
            "%(message)s"
        ),
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                log_file,
                encoding="utf-8",
            ),
        ],
        force=True,
    )