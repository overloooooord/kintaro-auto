"""
Generic IMAP email reader for TikTok 6-digit verification codes (merged from swegby/kintaro_auth).
Supports Firstmail, Rambler, Mail.ru, Yandex, GMX, NotLetters, and custom IMAP servers.
"""

from __future__ import annotations

import email
import email.utils
import html
import imaplib
import re
import time
from collections.abc import Callable
from email.header import decode_header
from typing import Any

KNOWN_IMAP_SERVERS: dict[str, tuple[str, int]] = {
    "firstmail.ltd": ("imap.firstmail.ltd", 993),
    "firstmailler.net": ("imap.firstmail.ltd", 993),
    "fmailler.com": ("imap.firstmail.ltd", 993),
    "fmaillerbox.com": ("imap.firstmail.ltd", 993),
    "fmaild.com": ("imap.firstmail.ltd", 993),
    "dfirstmail.com": ("imap.firstmail.ltd", 993),
    "vargosmail.com": ("imap.firstmail.ltd", 993),
    "polosmail.com": ("imap.firstmail.ltd", 993),
    "duhastmail.com": ("imap.firstmail.ltd", 993),
    "tacoblastmail.com": ("imap.firstmail.ltd", 993),
    "fuhrenmail.com": ("imap.firstmail.ltd", 993),
    "fringmail.com": ("imap.firstmail.ltd", 993),
    "bekommenmail.com": ("imap.firstmail.ltd", 993),
    "rambler.ru": ("imap.rambler.ru", 993),
    "lenta.ru": ("imap.rambler.ru", 993),
    "autorambler.ru": ("imap.rambler.ru", 993),
    "myrambler.ru": ("imap.rambler.ru", 993),
    "ro.ru": ("imap.rambler.ru", 993),
    "mail.ru": ("imap.mail.ru", 993),
    "inbox.ru": ("imap.mail.ru", 993),
    "list.ru": ("imap.mail.ru", 993),
    "bk.ru": ("imap.mail.ru", 993),
    "yandex.ru": ("imap.yandex.ru", 993),
    "ya.ru": ("imap.yandex.ru", 993),
    "gmx.com": ("imap.gmx.com", 993),
    "gmx.de": ("imap.gmx.net", 993),
    "notletters.com": ("imap.notletters.com", 993),
    "gmail.com": ("imap.gmail.com", 993),
}

_TIKTOK_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_TIKTOK_SUBJ_RE = re.compile(r"(\d{6})\s*(?:is your|—|–|-|:|\s+код)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def resolve_imap_server(email_addr: str, custom_host: str | None = None) -> tuple[str, int]:
    if custom_host and custom_host.strip():
        h = custom_host.strip()
        if ":" in h:
            host, port_s = h.rsplit(":", 1)
            return host.strip(), int(port_s)
        return h, 993
    domain = email_addr.split("@", 1)[-1].lower().strip()
    if domain in KNOWN_IMAP_SERVERS:
        return KNOWN_IMAP_SERVERS[domain]
    return ("imap.firstmail.ltd", 993)


def _decode_mime(raw: str | None) -> str:
    if not raw:
        return ""
    parts: list[str] = []
    for chunk, enc in decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(str(chunk))
    return "".join(parts)


def _extract_body(msg: Any) -> str:
    texts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    txt = payload.decode(charset, errors="ignore")
                    if ctype == "text/html":
                        txt = html.unescape(_TAG_RE.sub(" ", txt))
                    texts.append(txt)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            txt = payload.decode(charset, errors="ignore")
            if msg.get_content_type() == "text/html":
                txt = html.unescape(_TAG_RE.sub(" ", txt))
            texts.append(txt)
    return "\n".join(texts)


def prepare_imap_mailbox(
    email_addr: str,
    mail_password: str,
    *,
    custom_imap_host: str | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[tuple[str, int], set[str]]:
    """Verifies IMAP login and snapshots existing message UIDs before TikTok sends a new OTP."""
    host, port = resolve_imap_server(email_addr, custom_imap_host)
    if log_fn:
        log_fn(f"Подключение к IMAP {host}:{port} ({email_addr})...")
    conn = imaplib.IMAP4_SSL(host, port, timeout=20)
    try:
        conn.login(email_addr, mail_password)
        conn.select("INBOX")
        typ, data = conn.uid("search", None, "ALL")
        existing: set[str] = set()
        if typ == "OK" and data and data[0]:
            existing = {u.decode("ascii", errors="ignore") for u in data[0].split()}
        return (host, port), existing
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def wait_for_imap_code(
    email_addr: str,
    mail_password: str,
    *,
    since_timestamp: float,
    pre_server: tuple[str, int] | None = None,
    known_old_uids: set[str] | None = None,
    custom_imap_host: str | None = None,
    timeout_sec: int = 120,
    poll_interval_sec: float = 2.5,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    """Polls IMAP INBOX strictly for a brand-new 6-digit TikTok OTP code arriving after `since_timestamp`."""
    host, port = pre_server or resolve_imap_server(email_addr, custom_imap_host)
    old_uids = set(known_old_uids or ())
    deadline = time.time() + max(15, timeout_sec)

    while time.time() < deadline:
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=20)
            try:
                conn.login(email_addr, mail_password)
                for folder in ("INBOX", "Junk", "Spam"):
                    typ_sel, _ = conn.select(folder, readonly=True)
                    if typ_sel != "OK":
                        continue
                    typ, data = conn.uid("search", None, "ALL")
                    if typ != "OK" or not data or not data[0]:
                        continue
                    uids = [u.decode("ascii", errors="ignore") for u in data[0].split()]
                    for uid in reversed(uids[-15:]):
                        if folder == "INBOX" and uid in old_uids:
                            continue
                        ftyp, fdata = conn.uid("fetch", uid, "(RFC822)")
                        if ftyp != "OK" or not fdata or not isinstance(fdata[0], tuple):
                            continue
                        msg = email.message_from_bytes(fdata[0][1])
                        date_hdr = msg.get("Date")
                        if date_hdr:
                            try:
                                parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                                if parsed_dt and parsed_dt.timestamp() < since_timestamp - 15.0:
                                    continue
                            except Exception:
                                pass
                        sender = _decode_mime(msg.get("From")).lower()
                        subj = _decode_mime(msg.get("Subject"))
                        body = _extract_body(msg)
                        combined_low = f"{sender} {subj} {body[:400]}".lower()
                        if "tiktok" not in combined_low and "bytedance" not in combined_low:
                            continue
                        m_sub = _TIKTOK_SUBJ_RE.search(subj)
                        if m_sub:
                            code = m_sub.group(1)
                            if log_fn:
                                log_fn(f"Получен НОВЫЙ код TikTok по IMAP ({host}): {code}")
                            return code
                        m_body = _TIKTOK_CODE_RE.search(body)
                        if m_body:
                            code = m_body.group(1)
                            if log_fn:
                                log_fn(f"Получен НОВЫЙ код TikTok по IMAP ({host}): {code}")
                            return code
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(poll_interval_sec)

    raise TimeoutError(f"Новый 6-значный код TikTok не пришёл по IMAP ({email_addr}) за {timeout_sec}с.")
