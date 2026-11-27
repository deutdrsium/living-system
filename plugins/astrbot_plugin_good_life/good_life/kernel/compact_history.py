"""Offline entry point for the same migration used when enabling the kernel."""
import argparse
from contextlib import closing
from pathlib import Path

from ..storage import LifeStorage
from .repository import KernelRepository, VERSION_KEY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_directory", type=Path, help="directory containing good-life.sqlite3 (stop the service first)")
    directory = parser.parse_args().data_directory
    if not (directory / "good-life.sqlite3").is_file():
        parser.error("good-life.sqlite3 was not found in the data directory")
    with closing(LifeStorage.open(directory)) as storage:
        repository = KernelRepository(storage)
        if repository.reader.one("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)) is None:
            parser.error("no installed kernel schema")
        repository.install()
        page_size, pages, free_pages = (repository.reader.one(f"PRAGMA {name}", ())[0]
                                       for name in ("page_size", "page_count", "freelist_count"))
    print(f"Kernel history compacted. Allocated bytes: {pages * page_size}. "
          f"In-use page bytes: {(pages - free_pages) * page_size}.")


if __name__ == "__main__":
    main()
