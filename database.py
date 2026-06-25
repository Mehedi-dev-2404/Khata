from typing import Optional

from supabase import Client, create_client

import config

_client: Optional[Client] = None


def get_client() -> Optional[Client]:
    global _client
    if _client is not None:
        return _client
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return None
    _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    return _client
