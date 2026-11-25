"""HTTPS público, com resolução fixada por conexão e sem redirecionamentos."""
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import urllib3


class RemoteError(Exception):
    pass


def public_json(url, payload=None, headers=None, *, empty=False):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.port not in (443, None)
                or parts.username is not None or parts.password is not None or parts.fragment
                or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url):
            raise ValueError("destino")
        host = parts.hostname.encode("idna").decode("ascii")
        addresses = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM)]
        if not addresses or any(not a.is_global or a.is_multicast or
                (a.version == 6 and (a.ipv4_mapped or a.sixtofour or a.teredo or
                 a in ipaddress.ip_network("64:ff9b::/96"))) for a in addresses):
            raise ValueError("endereço")
        pool = urllib3.HTTPSConnectionPool(str(addresses[0]), 443, server_hostname=host,
            assert_hostname=host, ssl_context=ssl.create_default_context(),
            timeout=urllib3.Timeout(connect=3,read=4),retries=False)
        try:
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            with pool.urlopen("GET" if payload is None else "POST", path, body=payload,
                    headers={"Host":parts.netloc,"Accept":"application/json",**(headers or {})},
                    redirect=False, retries=False, preload_content=False) as response:
                if not 200 <= response.status < 300:
                    raise RemoteError(f"HTTP {response.status}")
                if empty:
                    return {}
                data = response.read(65537,decode_content=False)
                if len(data)>65536:
                    raise ValueError("resposta extensa")
                result = json.loads(data)
                if not isinstance(result,dict):
                    raise ValueError("resposta")
                return result
        finally:
            pool.close()
    except (ValueError, OSError, UnicodeError, urllib3.exceptions.HTTPError) as exc:
        raise RemoteError("Destino HTTPS indisponível ou resposta inválida.") from exc
