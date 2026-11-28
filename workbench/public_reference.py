"""Read bounded public HTTPS references without sending workspace credentials."""

from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit, urlunsplit


MAX_PAGE = 2_000_000


class ReferenceError(ValueError):
    pass


class Text(HTMLParser):
    excluded = {'script', 'style', 'template', 'noscript', 'svg', 'nav', 'footer', 'head'}
    blocks = {'p', 'li', 'pre', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'tr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.excluded:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.blocks:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        elif not self.hidden and tag in self.blocks:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def visible(self):
        lines = [' '.join(line.split()) for line in ''.join(self.parts).splitlines()]
        return '\n'.join(line for line in lines if line)


def _target(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or any(ord(ch) < 32 for ch in url) or len(url) > 2400):
        raise ReferenceError('references need public HTTPS URLs')
    host = parts.hostname.encode('idna').decode('ascii')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or '.' not in host:
        raise ReferenceError('select a named public host')
    target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
    return host, target


def _addresses(host):
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    found = list(dict.fromkeys(entry[4][0] for entry in answers))
    addresses = [ipaddress.ip_address(value) for value in found]
    if not addresses or any(not addr.is_global or addr.is_multicast or addr.is_reserved for addr in addresses):
        raise ReferenceError('reference host does not resolve exclusively to public addresses')
    return found


class Connection(http.client.HTTPSConnection):
    def __init__(self, host, addresses):
        super().__init__(host, port=443, timeout=12, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self):
        last_error = None
        for address in self.addresses[:4]:
            try:
                sock = socket.create_connection((address, 443), timeout=4)
                try:
                    self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                except BaseException:
                    sock.close()
                    raise
            except OSError as error:
                last_error = error
            else:
                self.sock.settimeout(self.timeout)
                return
        raise ReferenceError(f'could not connect to the public reference host: {self.host}') from last_error


def capture(url):
    requested = url
    for _ in range(4):
        host, path = _target(url)
        conn = Connection(host, _addresses(host))
        try:
            conn.request('GET', path, headers={'Accept': 'text/html, text/plain;q=0.9',
                                               'Accept-Encoding': 'identity', 'User-Agent': 'iBoard reference reader'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader('Location'):
                url = urljoin(url, response.getheader('Location'))
                continue
            if response.status != 200:
                raise ReferenceError(f'reference returned HTTP {response.status}')
            content_type = response.headers.get_content_type()
            if content_type not in ('text/html', 'text/plain') or response.getheader('Content-Encoding') not in (None, 'identity'):
                raise ReferenceError('reference needs a readable HTML or plain-text response')
            body = response.read(MAX_PAGE + 1)
            if len(body) > MAX_PAGE:
                raise ReferenceError('reference exceeds the response size bound')
            body = body.decode(response.headers.get_content_charset() or 'utf-8')
        finally:
            conn.close()
        if content_type == 'text/html':
            parser = Text()
            parser.feed(body)
            parser.close()
            body = parser.visible()
        if not body.strip():
            raise ReferenceError('reference contained no readable text')
        return {'requested_url': requested, 'url': url, 'text': body}
    raise ReferenceError('reference redirected too many times')
