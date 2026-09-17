"""Bounded provider transports, with no redirects or ambient proxy configuration."""

from .profiles import GatewayError


def bounded_transport(http, *, asynchronous=False, max_bytes=8 * 1024 * 1024):
    """Support the pinned httpx and Anthropic httpx2 APIs without buffering streaming turns."""

    def validate(response):
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in {"", "identity"}:
            raise GatewayError("compressed_response_denied", "Provider ignored bounded identity encoding")
        length = response.headers.get("content-length")
        if length and int(length) > max_bytes:
            raise GatewayError("provider_response_too_large", "Provider response exceeds the bounded limit")

    class SyncStream(http.SyncByteStream):
        def __init__(self, inner):
            self.inner = inner

        def __iter__(self):
            size = 0
            for chunk in self.inner:
                size += len(chunk)
                if size > max_bytes:
                    raise GatewayError(
                        "provider_response_too_large", "Provider response exceeds the bounded limit"
                    )
                yield chunk

        def close(self):
            self.inner.close()

    class AsyncStream(http.AsyncByteStream):
        def __init__(self, inner):
            self.inner = inner

        async def __aiter__(self):
            size = 0
            async for chunk in self.inner:
                size += len(chunk)
                if size > max_bytes:
                    raise GatewayError(
                        "provider_response_too_large", "Provider response exceeds the bounded limit"
                    )
                yield chunk

        async def aclose(self):
            await self.inner.aclose()

    class SyncTransport(http.BaseTransport):
        def __init__(self):
            self.inner = http.HTTPTransport(retries=0, trust_env=False)

        def handle_request(self, request):
            request.headers["Accept-Encoding"] = "identity"
            response = self.inner.handle_request(request)
            try:
                validate(response)
            except Exception:
                response.close()
                raise
            response.stream = SyncStream(response.stream)
            return response

        def close(self):
            self.inner.close()

    class AsyncTransport(http.AsyncBaseTransport):
        def __init__(self):
            self.inner = http.AsyncHTTPTransport(retries=0, trust_env=False)

        async def handle_async_request(self, request):
            request.headers["Accept-Encoding"] = "identity"
            response = await self.inner.handle_async_request(request)
            try:
                validate(response)
            except Exception:
                await response.aclose()
                raise
            response.stream = AsyncStream(response.stream)
            return response

        async def aclose(self):
            await self.inner.aclose()

    return AsyncTransport() if asynchronous else SyncTransport()
