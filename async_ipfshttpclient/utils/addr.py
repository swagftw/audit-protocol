from multiaddr.protocols import (
    P_DNS, P_DNS4, P_DNS6,  # type: ignore[import]
    P_HTTP, P_HTTPS, P_IP4, P_IP6, P_TCP, P_UNIX
)
import multiaddr
import multiaddr.exceptions
import exceptions
import typing as ty
import socket
import urllib


AF_UNIX = getattr(socket, "AF_UNIX", NotImplemented)


def multiaddr_to_url_data(
        addr, base: str  # type: ignore[no-any-unimported]
):
    try:
        multi_addr = multiaddr.Multiaddr(addr)
    except multiaddr.exceptions.ParseError as error:
        raise exceptions.AddressError(addr) from error

    addr_iter = iter(multi_addr.items())

    try:
        # Read host value
        proto, host = next(addr_iter)
        host_numeric = proto.code in (P_IP4, P_IP6)

        # Read port value for IP-based transports
        proto, port = next(addr_iter)
        if proto.code != P_TCP:
            raise exceptions.AddressError(addr)

        # Pre-format network location URL part based on host+port
        if ":" in host and not host.startswith("["):
            netloc = "[{0}]:{1}".format(host, port)
        else:
            netloc = "{0}:{1}".format(host, port)

        # Read application-level protocol name
        secure = False
        try:
            proto, value = next(addr_iter)
        except StopIteration:
            pass
        else:
            if proto.code == P_HTTPS:
                secure = True
            elif proto.code != P_HTTP:
                raise exceptions.AddressError(addr)

        # No further values may follow; this also exhausts the iterator
        was_final = all(False for _ in addr_iter)
        if not was_final:
            raise exceptions.AddressError(addr)
    except StopIteration:
        raise exceptions.AddressError(addr) from None

    if not base.endswith("/"):
        base += "/"

    # Convert the parsed `addr` values to a URL base and parameters for the
    # HTTP library
    base_url = urllib.parse.SplitResult(
        scheme="http" if not secure else "https",
        netloc=netloc,
        path=base,
        query="",
        fragment=""
    ).geturl()

    return base_url, host_numeric
