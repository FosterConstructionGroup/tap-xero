import singer
from singer import bookmarks as bks_
from .client import XeroClient


class Context:
    def __init__(self, config, state, catalog):
        self.config = config
        self.state = state
        self.catalog = catalog
        self.client = XeroClient(config)

    def refresh_credentials(self):
        self.client.refresh_credentials(self.config)

    # If there isn't a bookmark, fall back to start date from config
    def get_bookmark(self, path):
        b = bks_.get_bookmark(self.state, *path)
        return b if b else self.config["start_date"]

    def set_bookmark(self, path, val):
        bks_.write_bookmark(self.state, path[0], path[1], val)

    def get_offset(self, path):
        off = bks_.get_offset(self.state, path[0])
        return (off or {}).get(path[1])

    def set_offset(self, path, val):
        bks_.set_offset(self.state, path[0], path[1], val)

    def clear_offsets(self, tap_stream_id):
        bks_.clear_offset(self.state, tap_stream_id)

    def write_state(self):
        singer.write_state(self.state)
