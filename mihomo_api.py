"""Transport for the mihomo RESTful API.

Clash Verge Rev launches the core with a Windows named pipe as the only
control channel (-ext-ctl-pipe) and leaves the TCP external-controller off,
so the pipe transport is the default here. PipeClient speaks HTTP/1.0 over
the pipe: the response then carries a plain Content-Length body instead of
chunked encoding, which keeps parsing trivial.

HttpClient covers the other case, where a TCP external-controller has been
enabled in the client settings.
"""

import http.client
import json
import re
import time

DEFAULT_PIPE = r"\\.\pipe\verge-mihomo"


class ApiError(RuntimeError):
    pass


class _BaseClient:
    def get_json(self, path):
        status, body = self.request("GET", path)
        if status != 200:
            raise ApiError("GET %s -> %s %s" % (path, status, body[:200]))
        return json.loads(body)

    def request(self, method, path, body=None):
        raise NotImplementedError


class PipeClient(_BaseClient):
    def __init__(self, pipe=DEFAULT_PIPE, connect_retries=5):
        self.pipe = pipe
        self.connect_retries = connect_retries

    def _open(self):
        last = None
        for attempt in range(self.connect_retries):
            try:
                return open(self.pipe, "r+b", buffering=0)
            except OSError as exc:
                # ERROR_PIPE_BUSY: every instance is serving another client.
                last = exc
                time.sleep(0.05 * (attempt + 1))
        raise ApiError("cannot open %s: %s" % (self.pipe, last))

    def request(self, method, path, body=None):
        payload = body.encode("utf-8") if body else b""
        head = "%s %s HTTP/1.0\r\nHost: localhost\r\n" % (method, path)
        if payload:
            head += "Content-Type: application/json\r\n"
            head += "Content-Length: %d\r\n" % len(payload)
        head += "\r\n"

        handle = self._open()
        try:
            handle.write(head.encode("ascii") + payload)
            handle.flush()
            chunks = []
            while True:
                block = handle.read(65536)
                if not block:
                    break
                chunks.append(block)
        finally:
            try:
                handle.close()
            except OSError:
                pass

        raw = b"".join(chunks)
        sep = raw.find(b"\r\n\r\n")
        if sep < 0:
            raise ApiError("malformed response from %s" % self.pipe)
        header = raw[:sep].decode("latin-1")
        status = int(header.split(" ", 2)[1])
        return status, raw[sep + 4:].decode("utf-8", "replace")


class HttpClient(_BaseClient):
    def __init__(self, host="127.0.0.1", port=9097, secret=None, timeout=10):
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout

    def request(self, method, path, body=None):
        headers = {}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        if body:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()


def build_client(args):
    if getattr(args, "api", None):
        target = args.api.split("://", 1)[-1]
        host, _, port = target.partition(":")
        return HttpClient(host or "127.0.0.1", int(port or 9097), args.secret)
    return PipeClient(args.pipe)


_FRACTION = re.compile(r"\.(\d{1,9})")


def parse_ts(value):
    """Parse mihomo's RFC3339 timestamps.

    Go emits up to 9 fractional digits; datetime.fromisoformat accepts at
    most 6, so the fraction is truncated before parsing.
    """
    if not value:
        return None
    from datetime import datetime

    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], value, count=1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
