"""Library management APIs attached to :class:`VirtuosoClient`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from virtuoso_bridge.virtuoso.library.category import (
    CategoryPartialSuccessError,
    add_cell_to_category,
    create_category,
    delete_category,
    list_categories,
    list_category_cells,
    remove_cell_from_category,
    rename_category,
)
from virtuoso_bridge.virtuoso.library.management import (
    LibraryInfo,
    LibraryPartialSuccessError,
    create_library,
    delete_library,
    get_library,
    list_libraries,
    rename_library,
    set_technology_library,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class LibraryOps:
    """Attached to ``VirtuosoClient`` as ``client.library``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def list(self, *, timeout: int = 30) -> list[str]:
        """Return libraries visible in the current Virtuoso session."""
        return list_libraries(self._owner, timeout=timeout)

    def get(self, name: str, *, timeout: int = 30) -> LibraryInfo:
        """Return a library's path and technology binding."""
        return get_library(self._owner, name, timeout=timeout)

    def create(
        self,
        name: str,
        path: str,
        *,
        technology_library: str | None = None,
        timeout: int = 60,
    ) -> LibraryInfo:
        """Create a library and optionally bind existing technology."""
        return create_library(
            self._owner,
            name,
            path,
            technology_library=technology_library,
            timeout=timeout,
        )

    def delete(self, name: str, *, timeout: int = 60) -> None:
        """Delete a library through Cadence's supported API."""
        delete_library(self._owner, name, timeout=timeout)

    def rename(
        self,
        name: str,
        new_name: str,
        *,
        timeout: int = 120,
    ) -> LibraryInfo:
        """Rename a library without overwrite or force behavior."""
        return rename_library(self._owner, name, new_name, timeout=timeout)

    def get_technology_library(self, name: str, *, timeout: int = 30) -> str | None:
        """Return the technology library currently bound to a library."""
        return self.get(name, timeout=timeout).technology_library

    def set_technology_library(
        self,
        name: str,
        technology_library: str,
        *,
        timeout: int = 60,
    ) -> str:
        """Bind or change a library's technology library."""
        return set_technology_library(
            self._owner,
            name,
            technology_library,
            timeout=timeout,
        )

    def list_categories(self, library: str, *, timeout: int = 30) -> list[str]:
        """Return existing top-level categories in a library."""
        return list_categories(self._owner, library, timeout=timeout)

    def create_category(
        self,
        library: str,
        category: str,
        *,
        timeout: int = 30,
    ) -> str:
        """Create an empty persistent top-level category."""
        return create_category(self._owner, library, category, timeout=timeout)

    def rename_category(
        self,
        library: str,
        category: str,
        new_name: str,
        *,
        timeout: int = 60,
    ) -> str:
        """Rename a flat category without overwrite or merge behavior."""
        return rename_category(
            self._owner,
            library,
            category,
            new_name,
            timeout=timeout,
        )

    def delete_category(
        self,
        library: str,
        category: str,
        *,
        timeout: int = 30,
    ) -> None:
        """Delete a category and memberships without deleting cells."""
        delete_category(self._owner, library, category, timeout=timeout)

    def list_category_cells(
        self,
        library: str,
        category: str,
        *,
        timeout: int = 30,
    ) -> list[str]:
        """Return existing cell members of a category."""
        return list_category_cells(self._owner, library, category, timeout=timeout)

    def add_cell_to_category(
        self,
        library: str,
        category: str,
        cell: str,
        *,
        timeout: int = 30,
    ) -> None:
        """Add one existing cell to a category."""
        add_cell_to_category(
            self._owner,
            library,
            category,
            cell,
            timeout=timeout,
        )

    def remove_cell_from_category(
        self,
        library: str,
        category: str,
        cell: str,
        *,
        timeout: int = 30,
    ) -> None:
        """Remove one existing cell from a category."""
        remove_cell_from_category(
            self._owner,
            library,
            category,
            cell,
            timeout=timeout,
        )


__all__ = [
    "CategoryPartialSuccessError",
    "LibraryInfo",
    "LibraryOps",
    "LibraryPartialSuccessError",
]
