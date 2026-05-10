"""SEC submission .txt 에서 사람이 읽을 수 있는 본문만 추출.

EDGAR가 배포하는 8-K full submission .txt 는 SGML 래퍼 + XBRL/XHTML 태그가
대부분이라 raw 그대로 8000자를 자르면 헤더만 잘려서 LLM 분류 정확도가 급락한다
(W3 classify에서 contract_value 추출률 1/692). HTML 태그를 벗겨 본문 plain text를
얻은 뒤 LLM에 전달.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# SGML 래퍼 제거: <SEC-HEADER>...</SEC-HEADER> 같은 메타데이터 블록만 통째로 제거.
# 주의: <XBRL>...</XBRL> 은 본문 HTML 자체를 감싸므로 절대 통째로 제거 금지 — 태그만.
_SGML_BLOCKS_RE = re.compile(
    r"<(SEC-HEADER)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
# 단일 태그 SGML 라인 (<SEC-DOCUMENT>, <DOCUMENT>, <TEXT>, <XBRL>, <TYPE> 등) 만 제거
_SGML_INLINE_RE = re.compile(
    r"</?(SEC-DOCUMENT|DOCUMENT|TEXT|XBRL|XML|TYPE|SEQUENCE|FILENAME|DESCRIPTION)[^>]*>",
    re.IGNORECASE,
)
# XBRL 콘텍스트만 들어 있는 contextRef/factor 같은 inline 태그도 정리
_XBRL_INLINE_RE = re.compile(r"<ix:[^>]+>|</ix:[^>]+>", re.IGNORECASE)


def _strip_sgml_wrappers(text: str) -> str:
    """SEC SGML 헤더 + 첨부물 메타블록을 제거."""
    text = _SGML_BLOCKS_RE.sub("", text)
    text = _SGML_INLINE_RE.sub("", text)
    return text


def _html_to_text(html: str) -> str:
    """HTML/XBRL 태그를 제거하고 plain text 반환."""
    # XBRL inline 태그를 먼저 제거 (BeautifulSoup이 ix:* 태그를 잘 못 다루는 경우 대비)
    html = _XBRL_INLINE_RE.sub("", html)
    # lxml 우선, 없으면 html.parser fallback (배포 환경에 lxml 의존 X)
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_DOC_BLOCK_RE = re.compile(
    r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL | re.IGNORECASE
)
_DOC_TYPE_RE = re.compile(r"<TYPE>([^\s<]+)", re.IGNORECASE)
_DOC_FILENAME_RE = re.compile(r"<FILENAME>([^\s<]+)", re.IGNORECASE)
_TEXT_BLOCK_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.DOTALL | re.IGNORECASE)
# uuencoded binary 첨부물의 시그니처 (begin 644 ... 또는 한 줄에 60자+ ASCII가 반복)
_LIKELY_BINARY = re.compile(r"begin\s+\d{3}\s+|^M[\x21-\x60]{60,}", re.MULTILINE)


def _iter_8k_documents(submission: str) -> list[tuple[str, str, str]]:
    """submission에서 (type, filename, inner_text) 리스트 반환. TYPE 우선 8-K 만."""
    docs: list[tuple[str, str, str]] = []
    for m in _DOC_BLOCK_RE.finditer(submission):
        block = m.group(1)
        type_m = _DOC_TYPE_RE.search(block)
        fn_m = _DOC_FILENAME_RE.search(block)
        text_m = _TEXT_BLOCK_RE.search(block)
        doc_type = (type_m.group(1) if type_m else "").upper()
        filename = fn_m.group(1) if fn_m else ""
        inner = text_m.group(1) if text_m else block
        docs.append((doc_type, filename, inner))
    return docs


def extract_body(submission_text: str, max_chars: int = 8000) -> str:
    """SEC submission .txt 입력 → 사람이 읽을 8-K 본문 plain text (선두 max_chars).

    전략:
    1. <DOCUMENT> 블록을 모두 파싱
    2. TYPE=8-K 인 첫 블록의 <TEXT>...</TEXT> 본문을 우선 추출
    3. uuencoded binary 시그니처가 보이면 다음 8-K 후보로 이동
    4. 모두 실패 시 SGML 헤더 이후 본문에서 best-effort plain text
    """
    if not submission_text:
        return ""
    try:
        docs = _iter_8k_documents(submission_text)
        # TYPE=8-K 가 우선, 그 외 첨부는 후순위
        ordered = sorted(
            docs,
            key=lambda d: (
                0 if d[0] == "8-K" else 1,
                0 if d[1].lower().endswith((".htm", ".html", ".txt")) else 1,
            ),
        )
        for _type, fn, inner in ordered:
            is_html_8k = _type == "8-K" and fn.lower().endswith((".htm", ".html"))
            # 8-K HTML은 XBRL inline에 binary 시그니처가 우연히 매칭되는 경우가 있어
            # binary 체크를 스킵. EX-99.x 등 첨부물에서만 binary 검사.
            if not is_html_8k and _LIKELY_BINARY.search(inner[:2000]):
                continue
            cleaned = _strip_sgml_wrappers(inner)
            plain = _html_to_text(cleaned)
            if len(plain) >= 200:
                return plain[:max_chars]
        # fallback: SGML 헤더 이후 plain
        tail_start = submission_text.find("</SEC-HEADER>")
        tail = (
            submission_text[tail_start + len("</SEC-HEADER>"):]
            if tail_start >= 0
            else submission_text
        )
        cleaned = _strip_sgml_wrappers(tail)
        plain = _html_to_text(cleaned)
        return plain[:max_chars]
    except Exception as exc:
        logger.warning("body extract failed: %s — falling back to raw", exc)
        return submission_text[:max_chars]


def detect_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    """본문에서 키워드 매칭 (case-insensitive). PSS keyword fallback 용."""
    lower = text.lower()
    return [k for k in keywords if k.lower() in lower]
