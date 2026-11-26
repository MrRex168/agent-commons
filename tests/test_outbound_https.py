import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest

from agent_commons import outbound_https as https
from scripts.agent_internet_live_support import make_tls


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.4",
        "224.0.0.9",
        "100.100.100.200",
        "::1",
        "::ffff:7f00:1",
        "64:ff9b::a9fe:a9fe",
        "ff02::1",
        "fe80::1",
        "fc00::1",
    ],
)
def test_mixed_dns_answers_fail_before_connect(monkeypatch, address):
    answers = [(None, None, None, None, (item, 443)) for item in ["8.8.8.8", address]]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: answers)
    monkeypatch.setattr(https, "_transfer", lambda *args, **kwargs: pytest.fail("connected"))
    with pytest.raises(https.DestinationError, match="public IP"):
        https.request("https://peer.example/card", response_limit=100)


def test_validated_numeric_destination_is_selected_once(monkeypatch):
    calls = []

    def resolve(*args):
        calls.append(args)
        address = "8.8.8.8" if len(calls) == 1 else "127.0.0.1"
        return [(None, None, None, None, (address, 443))]

    def transfer(url, address, **kwargs):
        assert url.host == "peer.example"
        assert address == "8.8.8.8"
        return b"{}"

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(https, "_transfer", transfer)
    assert https.request("https://peer.example/card", response_limit=100) == b"{}"
    assert len(calls) == 1


@pytest.fixture
def tls_peer(tmp_path):
    observed = []
    hostnames = []
    cert, key, _ = make_tls(tmp_path)
    slow = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, self.headers.get("Authorization"), self.headers["Host"]))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/secret")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/slow":
                slow.wait(2)
                return
            self.send_response(200)
            self.send_header("Content-Length", "13")
            self.end_headers()
            self.wfile.write(b"test response")

        def log_message(self, *args):
            pass

    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    tls.set_servername_callback(lambda _, hostname, __: hostnames.append(hostname))
    listener.socket = tls.wrap_socket(listener.socket, server_side=True)
    worker = Thread(target=listener.serve_forever, daemon=True)
    worker.start()
    try:
        yield listener.server_port, str(cert), observed, hostnames
    finally:
        slow.set()
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=2)


def test_real_pinned_tls_rejects_wrong_hostname_before_http(tls_peer, monkeypatch):
    port, cert, observed, hostnames = tls_peer
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with pytest.raises(https.TransferError, match="connection failed"):
        https._transfer(
            httpx.URL(f"https://unknown.invalid:{port}/secret"),
            "127.0.0.1",
            timeout=2,
            response_limit=50,
            ca_file=cert,
        )
    assert hostnames == ["unknown.invalid"]
    assert not observed
    assert (
        https._transfer(
            httpx.URL(f"https://127.0.0.1:{port}/card"),
            "127.0.0.1",
            timeout=2,
            response_limit=50,
            ca_file=cert,
        )
        == b"test response"
    )
    assert observed == [("/card", None, f"127.0.0.1:{port}")]


def test_real_transfer_rejects_redirect_and_large_response(tls_peer):
    port, cert, observed, _ = tls_peer
    args = {"timeout": 2, "ca_file": cert}
    with pytest.raises(https.UnexpectedStatus) as error:
        https._transfer(
            httpx.URL(f"https://127.0.0.1:{port}/redirect"),
            "127.0.0.1",
            response_limit=50,
            **args,
        )
    assert error.value.status == 302
    assert [item[0] for item in observed] == ["/redirect"]
    with pytest.raises(https.ResponseTooLarge):
        https._transfer(
            httpx.URL(f"https://127.0.0.1:{port}/card"),
            "127.0.0.1",
            response_limit=5,
            **args,
        )


def test_request_limit_and_dns_timeout(monkeypatch):
    with pytest.raises(https.TransferError, match="request exceeds"):
        https.request("https://peer.example/a2a", response_limit=50, body=b"x" * 16385)
    wait = Event()
    monkeypatch.setattr(https, "DNS_SECONDS", 0.02)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: wait.wait(2))
    try:
        with pytest.raises(https.TransferError, match="DNS lookup timed out"):
            https.request("https://peer.example/a2a", response_limit=50)
    finally:
        wait.set()


def test_outbound_connection_budget(monkeypatch):
    release = Event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = [Event() for _ in range(8)]
        flags = iter(admitted)

        def transfer(*args, **kwargs):
            next(flags).set()
            assert release.wait(5)
            return b"{}"

        monkeypatch.setattr(https, "_transfer", transfer)
        calls = [
            pool.submit(https.request, "https://8.8.8.8/a2a", response_limit=50) for _ in admitted
        ]
        try:
            assert all(flag.wait(2) for flag in admitted)
            with pytest.raises(https.TransferError, match="capacity is busy"):
                https.request("https://8.8.8.8/a2a", response_limit=50)
        finally:
            release.set()
        assert all(call.result() == b"{}" for call in calls)


def test_real_transfer_has_absolute_deadline(tls_peer):
    port, cert, _, _ = tls_peer
    with pytest.raises(https.TransferError, match="timed out"):
        https._transfer(
            httpx.URL(f"https://127.0.0.1:{port}/slow"),
            "127.0.0.1",
            timeout=0.1,
            response_limit=50,
            ca_file=cert,
        )
