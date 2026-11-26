from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote
from kintaro_auto.models import AccountRecord, UploadTask


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir)
        self.accounts_dir = self.root / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)

    def account_path(self, account_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in account_id)
        return self.accounts_dir / f"{safe_id}.json"

    def save_account(self, account: AccountRecord) -> Path:
        path = self.account_path(account.account_id)
        _atomic_write_json(path, account.model_dump(mode="json"))
        return path

    def load_account(self, account_id: str) -> AccountRecord:
        path = self.account_path(account_id)
        if not path.exists():
            raise FileNotFoundError(f"Аккаунт {account_id!r} не найден в {path}")
        with path.open("r", encoding="utf-8") as f:
            return AccountRecord.model_validate(json.load(f))

    def list_accounts(self) -> list[AccountRecord]:
        records: list[AccountRecord] = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                records.append(AccountRecord.model_validate(json.load(f)))
        return records

    def import_accounts_file(self, source_path: Path) -> list[AccountRecord]:
        with source_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raise ValueError("Файл аккаунтов должен содержать JSON-массив объектов.")
        imported: list[AccountRecord] = []
        for item in raw:
            incoming = AccountRecord.model_validate(item)
            existing_path = self.account_path(incoming.account_id)
            if existing_path.exists():
                existing = self.load_account(incoming.account_id)
                # Preserve existing runtime device/session history unless incoming explicitly replaces device
                if incoming.device is None and existing.device is not None:
                    incoming.device = existing.device
                incoming.authenticated = existing.authenticated or (
                    incoming.auth_method == "device_only" and incoming.device is not None
                )
                incoming.first_authenticated_at = existing.first_authenticated_at
                incoming.last_hosts_update_at = existing.last_hosts_update_at
                incoming.last_token_beat_at = existing.last_token_beat_at
                incoming.last_warmup_at = existing.last_warmup_at
                incoming.pending_verify = existing.pending_verify
                incoming.upload_history = existing.upload_history
            else:
                if incoming.auth_method == "device_only" and incoming.device is not None:
                    incoming.authenticated = True
            self.save_account(incoming)
            imported.append(incoming)
        return imported


_EMAIL_TOKEN_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_markdown_links(raw: str) -> str:
    s = html.unescape(raw.strip())
    s = re.sub(r"\[\+\+([^\]]+?)\+\+\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\((?:mailto:|https?://)[^)]+\)", r"\1", s)
    s = re.sub(r"\+\+(.+?)\+\+", r"\1", s)
    return s


def normalize_proxy_url(raw_proxy: str) -> str:
    """
    Supports standard URLs (`socks5://...`, `http://...`) verbatim AND
    shorthand `host:port:user:pass` or `host:port` formats.
    """
    raw = (raw_proxy or "").strip()
    if not raw or "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{quote(user, safe='-_')}:{quote(pw, safe='-_')}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    return raw


def _looks_like_proxy_token(token: str) -> bool:
    t = token.strip().lower()
    if t.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://")):
        return True
    if re.match(r"^[a-z0-9.-]+:\d{2,5}(:.+)?$", t):
        return True
    if "@" in t and re.search(r":\d{2,5}$", t):
        return True
    return False


def parse_shop_account_line(line: str, proxy: str, region: str) -> AccountRecord:
    """
    Parses shop & restriction-manager formats:
    1) Pipe format: `username|password|email|totp_token[|proxy]` (TikTok 2FA TOTP + optional inline proxy)
    2) Outlook-Hub colon format (5/7/8 fields):
       `tt_login:tt_pass:outlook_email:outlook_pass:rec_email:rec_pass:hotmail_2fa`
       (where `hotmail_2fa` is Microsoft Authenticator TOTP for Outlook, NOT TikTok TOTP!)
    """
    cleaned = _clean_markdown_links(line)
    effective_proxy = normalize_proxy_url(proxy)
    if "|" in cleaned:
        parts = [p.strip() for p in cleaned.split("|")]
        if len(parts) >= 3 and _looks_like_proxy_token(parts[-1]):
            effective_proxy = normalize_proxy_url(parts.pop())
        if len(parts) < 2:
            raise ValueError(
                f"Не удалось разобрать строку аккаунта {line!r}. Ожидается `username|password|email|totp[|proxy]`."
            )
        username = parts[0].lstrip("@")
        password = parts[1]
        email = parts[2] if len(parts) >= 3 and "@" in parts[2] else ""
        totp = ""
        if len(parts) >= 4:
            candidate = parts[-1].replace(" ", "").upper()
            if len(candidate) >= 16 and candidate.isalnum():
                totp = candidate
        return AccountRecord(
            account_id=username,
            proxy=effective_proxy,
            region=region,
            auth_region="DE",
            auth_method="username",
            username=username,
            password=password,
            email=email,
            totp_token=totp,
            raw_line=cleaned,
        )

    # Colon-separated Outlook-Hub / standard colon format
    raw_parts = [p.strip() for p in cleaned.split(":") if p.strip()]
    email_idxs = [i for i, tok in enumerate(raw_parts) if _EMAIL_TOKEN_RE.match(tok)]
    if email_idxs and email_idxs[0] == 2 and len(raw_parts) >= 5:
        tt_u = raw_parts[0].lstrip("@")
        tt_p = raw_parts[1]
        primary_email = raw_parts[2].lower()
        mail_login = primary_email
        mail_pw = ""
        rec_email = ""
        rec_pw = ""
        hotmail_2fa = ""

        if len(email_idxs) >= 3 and email_idxs[1] == 3 and email_idxs[2] == 5:
            mail_login = raw_parts[3]
            mail_pw = raw_parts[4]
            rec_email = raw_parts[5].lower()
            rec_pw = raw_parts[6] if len(raw_parts) > 6 else ""
            hotmail_2fa = raw_parts[7] if len(raw_parts) > 7 else ""
        elif len(email_idxs) >= 2 and email_idxs[1] == 5:
            mail_login = raw_parts[3]
            mail_pw = raw_parts[4]
            rec_email = raw_parts[5].lower()
            rec_pw = raw_parts[6] if len(raw_parts) > 6 else ""
            hotmail_2fa = raw_parts[7] if len(raw_parts) > 7 else ""
        elif len(email_idxs) >= 2 and email_idxs[1] == 4:
            mail_pw = raw_parts[3]
            rec_email = raw_parts[4].lower()
            rec_pw = raw_parts[5] if len(raw_parts) > 5 else ""
            hotmail_2fa = raw_parts[6] if len(raw_parts) > 6 else ""
        else:
            mail_pw = raw_parts[3] if len(raw_parts) > 3 else ""
            hotmail_2fa = raw_parts[4] if len(raw_parts) > 4 else ""

        return AccountRecord(
            account_id=tt_u or primary_email,
            proxy=effective_proxy,
            region=region,
            auth_region="DE",
            auth_method="email",
            username=tt_u,
            password=tt_p,
            email=primary_email,
            totp_token="",
            mail_login=mail_login,
            mail_password=mail_pw,
            recovery_email=rec_email,
            recovery_password=rec_pw,
            mail_totp=hotmail_2fa,
            use_outlook_hub=True,
            raw_line=cleaned,
        )

    if len(raw_parts) >= 2:
        username = raw_parts[0].lstrip("@")
        password = raw_parts[1]
        totp = raw_parts[2].replace(" ", "").upper() if len(raw_parts) >= 3 else ""
        return AccountRecord(
            account_id=username,
            proxy=effective_proxy,
            region=region,
            auth_region="DE",
            auth_method="username",
            username=username,
            password=password,
            totp_token=totp,
            raw_line=cleaned,
        )

    raise ValueError(
        f"Не удалось разобрать строку аккаунта {line!r}. Ожидается `username|password|email|totp` или `login:pass:outlook:mail_pass:rec_mail:rec_pass:2fa`."
    )


def format_account_copy_line(account: AccountRecord) -> str:
    """Returns a copy-ready credential line for clipboard export (preserving raw shop/Outlook format when known)."""
    if account.raw_line and account.raw_line.strip():
        return account.raw_line.strip()
    uname = (account.username or account.account_id or "").strip()
    if account.use_outlook_hub or account.mail_password:
        parts = [uname, account.password or "", account.email or "", account.mail_password or ""]
        if account.recovery_email:
            parts.extend([account.recovery_email, account.recovery_password or ""])
        if account.mail_totp:
            parts.append(account.mail_totp)
        return ":".join(parts)
    if account.password and (account.totp_token or account.email):
        return f"{uname}|{account.password}|{account.email or ''}|{account.totp_token or ''}"
    if account.password:
        return f"{uname}:{account.password}"
    return f"{uname}:{account.email}" if account.email else uname


def load_queue(path: Path) -> list[UploadTask]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("Файл очереди задач должен содержать JSON-массив.")
    return [UploadTask.model_validate(item) for item in raw]


def save_queue(path: Path, tasks: list[UploadTask]) -> None:
    _atomic_write_json(path, [t.model_dump(mode="json") for t in tasks])
