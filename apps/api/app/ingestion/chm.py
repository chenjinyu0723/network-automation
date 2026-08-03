from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from bs4 import BeautifulSoup

# Huawei S-series naming has a stable sub-series prefix.  A product/SKU may add
# port specifications and software-feature suffixes after it, but those are not
# a new manual series.  Keep the map explicit instead of treating arbitrary
# numeric prefixes as compatible.
SERIES_BY_PREFIX = {
    "S17": "S1700",
    "S57": "S5700",
    "S67": "S6700",
    "S77": "S7700",
    "S127": "S12700",
}
MODEL_TOKEN_RE = re.compile(r"\bS(?:17|57|67|77|127)\d{2,}[A-Z0-9]*(?:-[A-Z0-9]+)*\b", re.IGNORECASE)
SUPPORT_SENTENCE_RE = re.compile(r"该命令.{0,80}(?:仅在|在).{0,800}?产品上支持", re.DOTALL)


@dataclass
class TocEntry:
    name: str
    local: str
    depth: int


class _TocParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.entries: list[TocEntry] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "ul":
            self.depth += 1
        elif tag == "object" and attributes.get("type", "").lower() == "text/sitemap":
            self._current = {"name": "", "local": "", "depth": str(self.depth)}
        elif tag == "param" and self._current is not None:
            name = attributes.get("name", "").lower()
            if name in {"name", "local"}:
                self._current[name] = unescape(attributes.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "object" and self._current is not None:
            local = self._current["local"].replace("\\", "/")
            if local:
                self.entries.append(
                    TocEntry(
                        name=self._current["name"],
                        local=local,
                        depth=int(self._current["depth"]),
                    )
                )
            self._current = None
        elif tag.lower() == "ul":
            self.depth = max(0, self.depth - 1)


def read_text_with_fallback(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    declared = re.search(rb"charset\s*=\s*([A-Za-z0-9_-]+)", raw[:4096], re.IGNORECASE)
    encodings: list[str] = []
    if declared:
        encodings.append(declared.group(1).decode("ascii", errors="ignore").lower())
    encodings.extend(["gbk", "utf-8", "gb18030", "latin-1"])
    visited: set[str] = set()
    for encoding in encodings:
        if not encoding or encoding in visited:
            continue
        visited.add(encoding)
        try:
            return raw.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def parse_toc(root: Path) -> tuple[dict[str, list[str]], list[TocEntry]]:
    hhc_files = list(root.glob("*.hhc"))
    if not hhc_files:
        return {}, []
    source, _encoding = read_text_with_fallback(hhc_files[0])
    parser = _TocParser()
    parser.feed(source)
    stack: list[TocEntry] = []
    paths: dict[str, list[str]] = {}
    for entry in parser.entries:
        while stack and stack[-1].depth >= entry.depth:
            stack.pop()
        stack.append(entry)
        paths[entry.local.lower()] = [item.name for item in stack]
    return paths, parser.entries


def _section_text(soup: BeautifulSoup, class_name: str) -> str:
    element = soup.select_one(f".{class_name}")
    if not element:
        return ""
    return "\n".join(line.strip() for line in element.get_text("\n", strip=True).splitlines() if line.strip())


def _metadata(soup: BeautifulSoup) -> dict[str, list[str]]:
    data: dict[str, list[str]] = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name")
        if name:
            data.setdefault(name, []).append(meta.get("content", ""))
    return data


def _unique_lines(value: str) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for line in value.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            lines.append(normalized)
    return lines


def _section_lines(value: str, heading: str) -> list[str]:
    lines = _unique_lines(value)
    return [line for line in lines if line != heading]


def _command_syntax_lines(soup: BeautifulSoup) -> list[str]:
    """Preserve one CLI grammar per HTML paragraph.

    Generic ``get_text("\\n")`` treats inline spans (parameters and keywords) as
    lines.  Huawei's CHM stores each grammar in a ``<p>`` inside ``.cliformatbody``;
    collect that DOM unit first, then use the old tolerant fallback for other HTML
    manuals.
    """

    section = soup.select_one(".cliformat")
    if not section:
        return []
    paragraphs = section.select(".cliformatbody > p")
    lines = [re.sub(r"\s+", " ", item.get_text(" ", strip=True)).strip() for item in paragraphs]
    # A few generated CHM pages put several grammar alternatives in one <p>
    # separated by an explicit line break. Keep them as separate alternatives.
    if len(lines) == 1 and "\n" in lines[0]:
        lines = [line.strip() for line in lines[0].splitlines() if line.strip()]
    normalized = _unique_lines("\n".join(lines))
    return normalized or _section_lines(_section_text(soup, "cliformat"), "命令格式")


def _extract_parameter_rows(soup: BeautifulSoup) -> list[dict[str, str]]:
    section = soup.select_one(".cliparam")
    if not section:
        return []
    rows: list[dict[str, str]] = []
    for table in section.find_all("table"):
        trs = table.find_all("tr")
        for tr in trs[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
            if len(cells) >= 2:
                rows.append(
                    {
                        "name": cells[0],
                        "description": cells[1],
                        "range": cells[2] if len(cells) > 2 else "",
                    }
                )
    return rows


def _extract_examples(soup: BeautifulSoup) -> list[str]:
    section = soup.select_one(".cliexample")
    if not section:
        return []
    examples = [pre.get_text("\n", strip=True) for pre in section.find_all("pre")]
    return [item for item in examples if item]


def infer_series(model_name: str) -> str | None:
    upper = model_name.upper()
    for prefix, series in sorted(SERIES_BY_PREFIX.items(), key=lambda item: -len(item[0])):
        if upper.startswith(prefix):
            return series
    return None


def classify_model_token(token: str) -> str:
    """Return ``family`` or ``sku`` without claiming vendor-specific certainty.

    The importer creates the inferred family as a candidate. A user must publish or correct
    it before it is selectable in a topology.
    """

    upper = token.upper()
    if "-" not in upper:
        return "family"
    parts = upper.split("-")
    middle = parts[1:-1] if re.fullmatch(r"V\d+", parts[-1]) else parts[1:]
    # An internal segment containing a digit is normally a port/SKU descriptor, unlike S/V2.
    return "sku" if any(any(char.isdigit() for char in segment) for segment in middle) else "family"


def infer_family_name(token: str) -> str | None:
    upper = token.upper()
    if classify_model_token(upper) != "sku" or "-" not in upper:
        return None
    parts = upper.split("-")
    prefix = parts[0]
    descriptor = parts[1]
    letter_match = re.match(r"([A-Z]+)", descriptor)
    if not letter_match:
        return None
    family = f"{prefix}-{letter_match.group(1)}"
    if re.fullmatch(r"V\d+", parts[-1]):
        family = f"{family}-{parts[-1]}"
    return family


@dataclass
class ParsedPage:
    source_path: str
    title: str
    toc_path: list[str]
    page_type: str
    encoding: str
    text_content: str
    metadata: dict[str, list[str]]
    command: dict[str, object] | None
    model_tokens: set[str] = field(default_factory=set)
    support_sentences: list[str] = field(default_factory=list)


def parse_html_page(path: Path, root: Path, toc_paths: dict[str, list[str]]) -> ParsedPage:
    html, encoding = read_text_with_fallback(path)
    soup = BeautifulSoup(html, "html.parser")
    relative = path.relative_to(root).as_posix()
    metadata = _metadata(soup)
    title = (metadata.get("DC.Title") or [""])[0].strip()
    if not title:
        title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    # Huawei chapter pages also declare DC.Type=cliref. A command is publishable only when
    # both command-function and command-format blocks exist; otherwise it remains a section/topic.
    has_command_blocks = soup.select_one(".clifunc") is not None and soup.select_one(".cliformat") is not None
    page_type = "command" if has_command_blocks else "topic"
    plain = soup.get_text("\n", strip=True)
    plain = "\n".join(_unique_lines(plain))
    model_tokens = {token.upper() for token in MODEL_TOKEN_RE.findall(plain)}
    support_sentences = [
        re.sub(r"\s+", " ", match.group(0)).strip() for match in SUPPORT_SENTENCE_RE.finditer(plain)
    ]
    command: dict[str, object] | None = None
    if page_type == "command":
        command = {
            "canonical_name": title,
            "feature": (metadata.get("featurename") or [None])[0],
            "syntax": _command_syntax_lines(soup),
            "views": _section_lines(_section_text(soup, "cliview"), "视图"),
            "parameters": _extract_parameter_rows(soup),
            "preconditions": _unique_lines(_section_text(soup, "clidesc")),
            "constraints": support_sentences,
            "examples": _extract_examples(soup),
            "metadata": metadata,
        }
    return ParsedPage(
        source_path=relative,
        title=title,
        toc_path=toc_paths.get(relative.lower(), [title]),
        page_type=page_type,
        encoding=encoding,
        text_content=plain,
        metadata=metadata,
        command=command,
        model_tokens=model_tokens,
        support_sentences=support_sentences,
    )


def iter_html_pages(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.html"))


def json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
