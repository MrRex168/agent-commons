"""Bounded libcurl HTTPS transfers pinned to an admitted numeric destination."""

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import BoundedSemaphore
from time import monotonic

import certifi
import httpx
import pycurl

TOTAL_SECONDS = 10
DNS_SECONDS = 3
CONNECT_MS = 3000
MAX_REQUEST_BYTES = 16_384
MAX_HEADER_BYTES = 32_768
SLOTS = BoundedSemaphore(8)
_DNS_SLOTS = BoundedSemaphore(8)
_DNS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="outbound-dns")
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
_EXCLUDED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "192.0.0.0/24",
        "192.88.99.0/24",
        "2001::/23",
        "2002::/16",
        "3fff::/20",
    )
)


class DestinationError(ValueError):
    """Destination is outside the outbound URL or address policy."""


class TransferError(RuntimeError):
    """Sanitized transport failure."""


class ResponseTooLarge(TransferError):
    pass


class UnexpectedStatus(TransferError):
    def __init__(self, status: int):
        super().__init__("Outbound server returned an unexpected HTTP status")
        self.status = status


def _url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if (
            str(url) != value
            or not value.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or url.scheme != "https"
            or not url.host
            or url.userinfo
            or "@" in url.netloc.decode("ascii")
            or "#" in value
            or "\\" in value
            or "%" in url.host
            or url.host.rstrip(".").endswith((".localhost", ".local"))
            or url.host.rstrip(".") in {"localhost", "local"}
            or url.port == 0
        ):
            raise ValueError
        return url
    except (ValueError, httpx.InvalidURL):
        raise DestinationError("Use a canonical public HTTPS URL without credentials") from None


def _public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address not in _GLOBAL_UNICAST_V6:
        return False
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not any(address in network for network in _EXCLUDED)
    )


def _resolve(url: httpx.URL, timeout: float) -> str:
    try:
        literal = ipaddress.ip_address(url.host)
    except ValueError:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise TransferError("Outbound DNS capacity is busy") from None
        try:
            future = _DNS.submit(
                socket.getaddrinfo, url.host, url.port or 443, 0, socket.SOCK_STREAM
            )
        except BaseException:
            _DNS_SLOTS.release()
            raise
        # A timed-out system resolver can still be running. Keep its slot until it exits.
        future.add_done_callback(lambda _: _DNS_SLOTS.release())
        try:
            records = future.result(timeout=timeout)
            addresses = list(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in records))
        except TimeoutError:
            raise TransferError("Outbound DNS lookup timed out") from None
        except (OSError, ValueError):
            raise DestinationError("Outbound hostname could not be resolved") from None
    else:
        addresses = [literal]
    if not addresses or any(not _public(address) for address in addresses):
        raise DestinationError("Outbound hostname must resolve only to public IP addresses")
    return str(addresses[0])


def validate_destination(value: str) -> None:
    _resolve(_url(value), DNS_SECONDS)


def _transfer(
    url: httpx.URL,
    address: str,
    *,
    timeout: float,
    response_limit: int,
    ca_file: str,
    body: bytes | None = None,
    bearer: str | None = None,
) -> bytes:
    if timeout <= 0:
        raise TransferError("Outbound HTTPS request timed out")
    buffer = bytearray()
    header_bytes = 0

    def read_header(data: bytes) -> int:
        nonlocal header_bytes
        header_bytes += len(data)
        return len(data) if header_bytes <= MAX_HEADER_BYTES else 0

    def write(data: bytes) -> int:
        if len(buffer) + len(data) > response_limit:
            return 0
        buffer.extend(data)
        return len(data)

    headers = ["Accept: application/json", "Accept-Encoding: identity"]
    if body is not None:
        headers.append("Content-Type: application/json")
    if bearer is not None:
        headers.append("Authorization: Bearer " + bearer)

    # CONNECT_TO overrides the socket destination, while libcurl uses the original
    # URL for TLS verification, SNI and HTTP Host. The wildcard matches every
    # host/port spelling. Each request has a fresh handle and no redirect path.
    target = f"[{address}]" if ":" in address else address
    with pycurl.Curl() as curl:
        curl.setopt(pycurl.URL, str(url))
        curl.setopt(pycurl.CONNECT_TO, [f"::{target}:{url.port or 443}"])
        curl.setopt(pycurl.PROTOCOLS, pycurl.PROTO_HTTPS)
        curl.setopt(pycurl.PROXY, "")
        curl.setopt(pycurl.FOLLOWLOCATION, False)
        curl.setopt(pycurl.PATH_AS_IS, True)
        curl.setopt(pycurl.SSL_VERIFYPEER, True)
        curl.setopt(pycurl.SSL_VERIFYHOST, 2)
        curl.setopt(pycurl.CAINFO, ca_file)
        curl.setopt(pycurl.NOSIGNAL, True)
        curl.setopt(pycurl.CONNECTTIMEOUT_MS, CONNECT_MS)
        curl.setopt(pycurl.TIMEOUT_MS, max(1, int(timeout * 1000)))
        # A per-transfer low-speed cutoff complements the absolute deadline.
        curl.setopt(pycurl.LOW_SPEED_LIMIT, 1)
        curl.setopt(pycurl.LOW_SPEED_TIME, 5)
        curl.setopt(pycurl.MAXFILESIZE_LARGE, response_limit)
        curl.setopt(pycurl.WRITEFUNCTION, write)
        curl.setopt(pycurl.HEADERFUNCTION, read_header)
        curl.setopt(pycurl.HTTPHEADER, headers)
        if body is not None:
            curl.setopt(pycurl.POSTFIELDS, body)
        try:
            curl.perform()
        except pycurl.error as exc:
            if exc.args[0] in (pycurl.E_WRITE_ERROR, pycurl.E_FILESIZE_EXCEEDED):
                raise ResponseTooLarge("Outbound response exceeds the size limit") from None
            if exc.args[0] == pycurl.E_OPERATION_TIMEDOUT:
                raise TransferError("Outbound HTTPS request timed out") from None
            raise TransferError("Outbound HTTPS connection failed") from None
        status = curl.getinfo(pycurl.RESPONSE_CODE)
        if not 200 <= status < 300:
            raise UnexpectedStatus(status)
    return bytes(buffer)


def request(
    value: str,
    *,
    response_limit: int,
    body: bytes | None = None,
    bearer: str | None = None,
    valid_until: datetime | None = None,
) -> bytes:
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise TransferError("Outbound request exceeds the size limit")
    if bearer is not None and (body is None or not bearer.isascii() or not bearer.isprintable()):
        raise TransferError("Invalid outbound credential")
    if not SLOTS.acquire(blocking=False):
        raise TransferError("Outbound HTTPS capacity is busy")
    try:
        end = monotonic() + TOTAL_SECONDS
        url = _url(value)
        dns_budget = DNS_SECONDS
        if valid_until is not None:
            dns_budget = min(dns_budget, (valid_until - datetime.now(UTC)).total_seconds())
            if dns_budget <= 0:
                raise TransferError("Peer credential or endpoint authorization expired")
        address = _resolve(url, dns_budget)
        timeout = end - monotonic()
        if valid_until is not None:
            remaining = (valid_until - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise TransferError("Peer credential or endpoint authorization expired")
            timeout = min(timeout, remaining)
        return _transfer(
            url,
            address,
            timeout=timeout,
            response_limit=response_limit,
            ca_file=certifi.where(),
            body=body,
            bearer=bearer,
        )
    finally:
        SLOTS.release()
