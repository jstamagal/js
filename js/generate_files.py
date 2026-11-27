"""Local media inputs, output naming and atomic publication."""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import mimetypes
import os
import socket
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies

import httpx


@dataclass(frozen=True)
class Asset:
    source: str

    def __post_init__(self):
        if not self.remote and not Path(self.source).is_file():
            raise ValueError(f"input file does not exist: {self.source}")

    @property
    def remote(self):
        return self.source.startswith(("http://", "https://"))

    def upload(self, client):
        name = Path(urlsplit(self.source).path if self.remote else self.source).name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if self.remote:
            r = client.get(self.source, follow_redirects=True)
            r.raise_for_status()
            return name, r.content, r.headers.get("content-type", mime)
        return name, Path(self.source).read_bytes(), mime

    def url(self, client):
        if self.remote:
            return self.source
        _, data, mime = self.upload(client)
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class InlineImage(Asset):
    """A Gemini inlineData part."""


def destinations(a):
    text = a.op == "stt" or a.list_voices
    default = {"image": "png", "video": "mp4", "tts": "mp3", "music": "mp3", "sfx": "mp3",
               "stt": "json" if a.timestamps or a.diarize else "txt"}[a.kind]
    if a.list_voices:
        default = "json"
    suffix = Path(a.output or "").suffix.lstrip(".").lower()
    fmt = (a.format or suffix or default).lower()
    ext = fmt.split("_")[0] if a.kind in {"music", "sfx", "tts"} else fmt
    if not ext.isalnum():
        raise ValueError(f"invalid output format {fmt!r}")
    alias = {"jpg": "jpeg", "wave": "wav", "text": "txt"}
    if suffix and alias.get(suffix, suffix) != alias.get(ext, ext):
        raise ValueError("--format and output filename extension disagree")
    if text and fmt not in ({"json"} if a.list_voices or a.timestamps or a.diarize else {"txt", "text", "json"}):
        raise ValueError("use JSON to retain transcript metadata or voice listings, or txt for plain transcription")
    if a.kind == "video" and fmt != "mp4":
        raise ValueError("video output requires mp4")
    path = Path(a.output or ("-" if text else "out." + ext))
    count = a.count or 1
    if count > 1 and path == Path("-"):
        raise ValueError("-o - takes one artifact")
    outputs = ([path] if count == 1 else
               [path.with_name(f"{path.stem}-{i:03}{path.suffix}") for i in range(1, count + 1)])
    if a.submit_only:
        return fmt, []
    for dest in outputs:
        if dest == Path("-"):
            continue
        if not dest.parent.is_dir():
            raise ValueError(f"output directory does not exist: {dest.parent}")
        if dest.is_dir() or (os.path.lexists(dest) and not a.force):
            raise ValueError(f"output exists: {dest} (use --force to replace a file)")
    return fmt, outputs


def image_bytes(data, fmt):
    fmt = {"jpg": "JPEG"}.get(fmt, fmt.upper())
    # Native encodings pass through, including on installations without Pillow.
    magic = {"PNG": b"\x89PNG\r\n\x1a\n", "JPEG": b"\xff\xd8\xff", "GIF": b"GIF8"}
    if (fmt in magic and data.startswith(magic[fmt])) or (
            fmt == "WEBP" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return data
    from PIL import Image
    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as img:
        if img.format == fmt:
            return data
        img = img.convert("RGB") if fmt == "JPEG" else img
        img.save(out, format=fmt)
    return out.getvalue()


def mp4_boxes(data):
    while len(data) >= 8:
        size, kind = struct.unpack_from(">I4s", data)
        header = 8
        if size == 1 and len(data) >= 16:
            size, = struct.unpack_from(">Q", data, 8)
            header = 16
        elif size == 0:
            size = len(data)
        if not header <= size <= len(data):
            raise ValueError("invalid MP4 box size")
        yield kind, data[header:size]
        data = data[size:]
    if data:
        raise ValueError("truncated MP4 box")


def mp4_has_video(movie):
    for kind, track in mp4_boxes(movie):
        if kind != b"trak":
            continue
        for kind, media in mp4_boxes(track):
            if kind == b"mdia" and any(
                field == b"hdlr" and value[8:12] == b"vide" for field, value in mp4_boxes(media)
            ):
                return True
    return False


def check_video(data):
    boxes = {"format": False, "video": False, "samples": False}
    try:
        for kind, payload in mp4_boxes(memoryview(data)):
            if kind == b"ftyp":
                boxes["format"] |= len(payload) >= 8
            elif kind == b"moov":
                boxes["video"] |= mp4_has_video(payload)
            elif kind == b"mdat":
                boxes["samples"] |= bool(payload)
    except ValueError as exc:
        raise ValueError("expected an MP4 video artifact") from exc
    if not all(boxes.values()):
        raise ValueError("expected an MP4 video artifact")


def save(outputs, results, fmt, a):
    if len(results) != len(outputs):
        raise ValueError(f"endpoint returned {len(results)} artifacts for {len(outputs)} outputs")
    staged = []
    linked = []
    try:
        for path, data in zip(outputs, results, strict=True):
            if not data:
                raise ValueError("endpoint returned an empty artifact")
            if a.kind == "image":
                data = image_bytes(data, fmt)
            elif a.kind == "video":
                check_video(data)
            if path == Path("-"):
                sys.stdout.buffer.write(data)
                return
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                staged.append((Path(f.name), path))
                f.write(data)
        # A concurrent run cannot replace a file when --force was absent.
        for tmp, path in staged:
            if a.force:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)
                linked.append((tmp, path))
    except OSError:
        for tmp, path in linked:
            if path.exists() and path.samefile(tmp):
                path.unlink()
        raise
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    for path in outputs:
        print(path)


def pretty(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def checked(response):
    if response.is_error:
        raise ValueError(f"endpoint returned HTTP {response.status_code}: {response.text[:2000]}")
    response.raise_for_status()
    return response


def resolved_addresses(url):
    try:
        answers = socket.getaddrinfo(url.raw_host.decode("ascii"), url.port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(ipaddress.ip_address(answer[4][0]) for answer in answers))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot resolve artifact host: {url}") from exc
    if not addresses:
        raise ValueError(f"cannot resolve artifact host: {url}")
    return addresses


def public_address(address):
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.is_site_local or address in ipaddress.IPv6Network("64:ff9b::/96"):
            return False
    return address.is_global and not (address.is_multicast or address.is_reserved)


def record_peer(response):
    # Capture the peer while the response still holds its connection.
    stream = response.extensions.get("network_stream")
    if stream is not None:
        try:
            peer = stream.get_extra_info("server_addr")
        except OSError:
            return
        if peer:
            response.extensions["generate_peer"] = (ipaddress.ip_address(peer[0]), peer[1])


def local_endpoint(response):
    address, port = response.extensions.get("generate_peer", (None, None))
    origin = response.url
    if address is None or public_address(address) or port != endpoint_port(origin):
        return False
    # A proxy's TCP peer can also be on the local network.
    proxies = getproxies()
    try:
        for key in (origin.scheme, "all"):
            if proxy := proxies.get(key):
                proxy_url = httpx.URL(proxy if "://" in proxy else "http://" + proxy)
                if port == endpoint_port(proxy_url) and address in resolved_addresses(proxy_url):
                    return False
        return address in resolved_addresses(origin)
    except ValueError:
        return False


def endpoint_port(url):
    return url.port or {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}.get(url.scheme)


def public_get(client, url, headers):
    addresses = resolved_addresses(url)
    if not all(public_address(address) for address in addresses):
        raise ValueError(f"artifact URL resolves to a non-public address: {url}")

    # Use the checked IP for TCP and the URL hostname for HTTP and TLS.
    request = client.build_request("GET", url, headers=headers,
                                   extensions={"sni_hostname": url.raw_host.decode("ascii")})
    request.headers["Host"] = url.netloc.decode("ascii")
    verify = httpx.create_ssl_context() if url.scheme == "https" else True
    with httpx.Client(timeout=client.timeout, trust_env=False, verify=verify) as direct:
        for address in addresses:
            request.url = url.copy_with(host=str(address))
            try:
                response = direct.send(request, follow_redirects=False)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if address == addresses[-1]:
                    raise
        request.url = url
        client.cookies.extract_cookies(response)
        return response


def download(client, url, source, headers):
    origin = source.url
    dest = origin.join(url)
    local = local_endpoint(source)
    # Compare each hop to the endpoint so its custom headers stay on its origin.
    for _ in range(10):
        if dest.scheme not in {"http", "https"} or not dest.host or dest.userinfo:
            raise ValueError(f"invalid artifact URL: {dest}")
        same_origin = (dest.scheme, dest.host, dest.port) == (origin.scheme, origin.host, origin.port)
        request_headers = headers if same_origin else {}
        if local:
            response = client.get(dest, headers=request_headers, follow_redirects=False)
        else:
            response = public_get(client, dest, request_headers)
        if not response.is_redirect:
            return checked(response).content
        dest = dest.join(response.headers["location"])
    raise ValueError(f"too many artifact redirects: {dest}")
