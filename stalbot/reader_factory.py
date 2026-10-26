"""Create one native reader with a device-specific compatible disk NAME cache."""
from .cache_storage import create_storage
from .name_cache import CachedNameReader
from .vision import Reader


def create_cached_reader(models, device, capacity, cache_directory):
    storage=create_storage(models,device=device,directory=cache_directory)
    native=Reader(models,device=device)
    try:
        return CachedNameReader(native,capacity,persistence=storage)
    except BaseException:
        native.close()
        raise
