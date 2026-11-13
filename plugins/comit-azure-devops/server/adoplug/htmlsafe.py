"""HTML de comentário: montagem segura e comparação na releitura.

- `sanitize` reconstrói o HTML com uma lista fechada de tags e atributos e recusa o resto.
- `text_to_html` escapa texto puro, troca quebra de linha por `<br>` e cria link http(s).
- `equivalent` compara o HTML enviado com o HTML que o ADO devolve. Tolera só as
  normalizações medidas: `target=_blank` sem aspas, entidades (`&quot;`), `%28`/`%29` no
  `href` e o link de anexo devolvido com o GUID do projeto depois da org.
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
GUID_RE = re.compile("^" + GUID + "$")
MENTION_RE = re.compile(r"^version:2\.0," + GUID + "$")
URL_RE = re.compile(r"https?://[^\s<>\"']+")

ALLOWED_TAGS = frozenset({"a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4",
                          "i", "li", "ol", "p", "pre", "span", "strong", "u", "ul"})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"})
BLOCK_TAGS = frozenset({"p", "div", "li", "ul", "ol"})


class HtmlError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def escape(text: str) -> str:
    return _html.escape("" if text is None else str(text), quote=True)


def is_http_url(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme in ("http", "https") and bool(u.netloc) and not u.username and not u.password


def link(url: str, label: Optional[str] = None) -> str:
    """`<a href=... target="_blank">rótulo</a>` só para http(s)."""
    if not is_http_url(url):
        raise HtmlError("HTML_URL_NOT_ALLOWED", "O link precisa ser http ou https.")
    return f'<a href="{escape(url)}" target="_blank">{escape(label if label else url)}</a>'


def mention(identity_id: str, display_name: str) -> str:
    """Menção do ADO pelo id da identidade (GUID)."""
    if not isinstance(identity_id, str) or not GUID_RE.match(identity_id):
        raise HtmlError("MENTION_INVALID", "A menção precisa do id (GUID) da identidade.")
    name = (display_name or "").strip()
    if not name:
        raise HtmlError("MENTION_INVALID", "A menção precisa do nome de exibição.")
    return f'<a href="#" data-vss-mention="version:2.0,{identity_id.lower()}">@{escape(name)}</a>'


def text_to_html(text: str) -> str:
    """Texto puro para HTML: escapa, cria link http(s) e troca quebra de linha por `<br>`."""
    out: List[str] = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        parts, pos = [], 0
        for m in URL_RE.finditer(line):
            url = m.group(0).rstrip(".,;:")
            # `)` final é pontuação só quando sobra sem `(` correspondente na URL.
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1].rstrip(".,;:")
            end = m.start() + len(url)
            parts.append(escape(line[pos:m.start()]))
            parts.append(link(url))
            pos = end
        parts.append(escape(line[pos:]))
        out.append("".join(parts))
    return "<br>".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[str] = []
        self.refused: List[str] = []

    def _attrs_a(self, attrs: Sequence[Tuple[str, Optional[str]]]) -> str:
        d = {}
        for k, v in attrs:
            if k in d:
                self.refused.append(f"atributo repetido {k}")
                return ""
            d[k] = v or ""
        unknown = set(d) - {"href", "target", "data-vss-mention", "rel"}
        if unknown:
            self.refused.append("atributo " + ", ".join(sorted(unknown)) + " em <a>")
            return ""
        href = d.get("href", "")
        if "data-vss-mention" in d:
            if href != "#" or not MENTION_RE.match(d["data-vss-mention"]):
                self.refused.append("menção fora do formato version:2.0,<GUID>")
                return ""
            return f' href="#" data-vss-mention="{escape(d["data-vss-mention"].lower())}"'
        if href == "#":
            return ' href="#"'
        if not is_http_url(href):
            self.refused.append("link que não é http(s)")
            return ""
        return f' href="{escape(href)}" target="_blank"'

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.refused.append(f"<{tag}>")
            return
        if tag == "a":
            a = self._attrs_a(attrs)
        else:
            if attrs:
                self.refused.append("atributo " + ", ".join(sorted(k for k, _ in attrs)) + f" em <{tag}>")
                return
            a = ""
        self.out.append(f"<{tag}{a}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.refused.append(f"<{tag}/>")
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.refused.append(f"</{tag}> sem abertura correspondente")
            return
        self.stack.pop()
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(escape(data))

    def handle_comment(self, data):
        self.refused.append("comentário HTML")

    def handle_decl(self, decl):
        self.refused.append("declaração")

    def handle_pi(self, data):
        self.refused.append("instrução de processamento")

    def unknown_decl(self, data):
        self.refused.append("declaração")


def sanitize(html: str) -> str:
    """Reconstrói o HTML com a lista fechada. Recusa (não remove) o que estiver fora dela."""
    p = _Sanitizer()
    p.feed(str(html or ""))
    p.close()
    if p.stack:
        p.refused.append("tag sem fechamento: " + ", ".join(f"<{t}>" for t in p.stack))
    if p.refused:
        raise HtmlError("HTML_NOT_ALLOWED", "HTML recusado: " + "; ".join(dict.fromkeys(p.refused)))
    return "".join(p.out)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.href: List[Optional[str]] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("p", "div") and self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")
        if tag == "a":
            h = d.get("href")
            self.href.append(h if h and h != "#" else None)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            h = self.href.pop()
            if h:
                self.out.append(f" ({h})")
        elif tag in BLOCK_TAGS and self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def to_text(html: str) -> str:
    """Texto legível do HTML para a prévia: quebra de linha, item de lista e URL do link."""
    p = _Text()
    p.feed(str(html or ""))
    p.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


# --- comparação na releitura ----------------------------------------------------

def _paren_escapes(value: str) -> str:
    """O ADO devolve `(` e `)` do href como `%28`/`%29`; só esses dois escapes são equivalentes."""
    return re.sub(r"%(28|29)", lambda m: "(" if m.group(1) == "28" else ")", value)


def org_attachment_links(text: str, org: Optional[str], project: Optional[str]) -> str:
    """Troca o link de anexo na forma do projeto pela forma da org.

    O ADO devolve `https://dev.azure.com/<org>/_apis/wit/attachments/...` como
    `https://dev.azure.com/<org>/<projeto>/_apis/wit/attachments/...`, no href e no texto.
    Só esse segmento, só esse projeto e só onde o link começa.
    """
    if not isinstance(text, str) or not org or not project:
        return text
    host = "https://dev.azure.com/" + org
    return re.sub(r"(?<![\w./:?=&%#~+-])(?i:" + re.escape(host) + ")/(?i:" + re.escape(project) + r")(?=/_apis/wit/attachments/)",
                  host, text)


class _Tokens(HTMLParser):
    def __init__(self, org, project):
        super().__init__(convert_charrefs=True)
        self.org, self.project = org, project
        self.tokens: list = []
        self.stack: List[str] = []
        self.valid = True

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = [(k, _paren_escapes(org_attachment_links(v or "", self.org, self.project)) if k == "href" else v)
                     for k, v in attrs]
        if len({k for k, _ in attrs}) != len(attrs):
            self.valid = False
        if tag in ("style", "link") or any(k in ("style", "class", "id") for k, _ in attrs):
            self.valid = False  # sem cascata CSS, só igualdade literal
        self.tokens.append(("start", tag, tuple(sorted(attrs, key=lambda a: a[0]))))
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.valid = False  # em HTML, /> só fecha elemento vazio
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        if tag in BLOCK_TAGS and not any(t in ("pre", "code") for t in self.stack):
            if self.tokens and self.tokens[-1][0] == "data":
                trimmed = self.tokens[-1][1].rstrip(" \t\r\n")
                if trimmed:
                    self.tokens[-1] = ("data", trimmed)
                else:
                    self.tokens.pop()
        self.stack.pop()
        self.tokens.append(("end", tag))

    def handle_data(self, data):
        if self.tokens and self.tokens[-1][0] == "data":
            self.tokens[-1] = ("data", self.tokens[-1][1] + data)
        else:
            self.tokens.append(("data", data))

    def handle_comment(self, data):
        self.tokens.append(("comment", data))

    def handle_decl(self, data):
        self.tokens.append(("decl", data))

    def handle_pi(self, data):
        self.tokens.append(("pi", data))

    def unknown_decl(self, data):
        self.valid = False


def equivalent(actual: str, expected: str, org: Optional[str] = None, project: Optional[str] = None) -> bool:
    """O HTML devolvido pelo ADO equivale ao enviado? `org`+`project` ligam a tolerância do anexo."""
    if actual == expected:
        return True
    parsed = []
    for value in (actual, expected):
        p = _Tokens(org, project)
        try:
            p.feed(value or "")
            p.close()
        except (ValueError, AssertionError):
            return False
        if not p.valid or p.stack:
            return False
        parsed.append([("data", org_attachment_links(t[1], org, project)) if t[0] == "data" else t for t in p.tokens])
    return parsed[0] == parsed[1]
