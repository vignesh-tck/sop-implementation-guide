import json
from functools import lru_cache

import httpx
import postgrest.base_request_builder as _postgrest_responses
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from backend.config import settings


class _PlainJSON:
    """
    Parses PostgREST responses with json.loads instead of postgrest-py's
    pydantic check against a recursive JSON union type. That check runs in
    native code that tracemalloc does not see, and on a deeply nested row it is
    the costliest thing in the app: reading one pipeline's 4 MB state took
    ~300 MB at peak and 2.5 s, against ~25 MB and 0.4 s this way. Several at
    once (polls, progress writes) pushed the hosted server past its memory
    limit. Behaves as postgrest's own fallback does: an empty body is [], a
    body that is not JSON comes back as text.
    """

    @staticmethod
    def validate_json(content: bytes):
        if not content:
            return []
        try:
            return json.loads(content)
        except ValueError:
            return content.decode("utf-8", "replace")


_postgrest_responses.JSONAdapter = _PlainJSON


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
