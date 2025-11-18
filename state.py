
import asyncio
from dataclasses import dataclass, field

@dataclass
class AppState:
    """Holds the shared state of the application."""
    download_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    upload_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    new_download_event: asyncio.Event = field(default_factory=asyncio.Event)
    
    active_torrents: dict = field(default_factory=dict)
    torrent_metadata_cache: dict = field(default_factory=dict)
    channel_file_index: set = field(default_factory=set)
    torrent_locks: dict = field(default_factory=dict)