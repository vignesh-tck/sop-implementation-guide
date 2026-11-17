from functools import lru_cache

import httpx
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from backend.config import settings


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """
    Return a singleton Supabase client using the service role key.

    The client talks HTTP/1.1, not supabase-py's default HTTP/2. FastAPI runs
    sync endpoints on a thread pool, and httpcore's sync HTTP/2 transport fails
    intermittently under concurrent requests with httpx.ReadError ("WinError
    10035"), even with one client per thread. It reproduced at about 1 in 25
    requests. The app sends several requests at once right after a funding
    approval (the plan and the plan list reload together), which showed up as
    "Could not load the plan" while the approval itself had saved. Over
    HTTP/1.1 each concurrent request gets its own pooled connection, and
    httpx.Client is safe to share across threads.

    PostgREST and Storage build absolute URLs and send the auth headers on
    every request, so a plain client with no base URL works for both.
    """
    http = httpx.Client(
        http2=False,
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=SyncClientOptions(httpx_client=http),
    )
