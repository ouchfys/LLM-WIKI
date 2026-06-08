"""Storage backends and canonical object layout."""

from system.storage.object_storage import get_object_storage
from system.storage.layout import StorageLayout, get_storage_layout

__all__ = ["StorageLayout", "get_object_storage", "get_storage_layout"]
