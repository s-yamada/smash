#!/usr/bin/env python3
import argparse
import json
import logging
import os
import poplib
import re
import sys
import imaplib
import shutil
import unicodedata
from dataclasses import dataclass
from email import message_from_bytes
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
from typing import Any

@dataclass
class MailItem:
    seq: int
    account_name: str
    source_user: str
    from_addr: str
    date: str
    subject: str
    raw_message: bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smash",
        usage="smash [account or email]",
        description="Registered mail servers from accounts.json are fetched and shown in a mail-like interface.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="account name or email address. if omitted, all accounts are targeted.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable info logs",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="enable info logs",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logs",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list configured account names and exit",
    )
    return parser.parse_args()


def configure_logging(args: argparse.Namespace) -> None:
    level = logging.WARNING
    if args.debug:
        level = logging.DEBUG
    elif args.verbose or args.info:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )


def load_accounts(path: str = "accounts.json") -> list[dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found.")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("accounts.json must be a JSON array.")

    return data


def filter_accounts(accounts: list[dict[str, Any]], target: str | None) -> list[dict[str, Any]]:
    if not target:
        return accounts

    needle = target.strip().lower()
    selected = []

    for acc in accounts:
        name = str(acc.get("name", "")).lower()
        user = str(acc.get("user", "")).lower()
        if needle == name or needle == user:
            selected.append(acc)

    return selected


def print_account_list(accounts: list[dict[str, Any]]) -> None:
    if not accounts:
        print("登録済みアカウントはありません。")
        return

    for acc in accounts:
        print(acc.get("name", acc.get("user", "(unknown)")))


def _header_text(msg: Message, key: str, fallback: str = "") -> str:
    value = msg.get(key, fallback)
    if value is None:
        return fallback
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _extract_body_text(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue

            ctype = part.get_content_type()
            try:
                payload = part.get_content()
            except Exception:
                raw = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                payload = raw.decode(charset, errors="replace")

            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                payload = payload.decode(charset, errors="replace")

            if ctype == "text/plain":
                plain_parts.append(payload)
            elif ctype == "text/html":
                html_parts.append(payload)
    else:
        try:
            payload = msg.get_content()
        except Exception:
            raw = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = raw.decode(charset, errors="replace")

        if isinstance(payload, bytes):
            charset = msg.get_content_charset() or "utf-8"
            payload = payload.decode(charset, errors="replace")

        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain_parts.append(payload)
        elif ctype == "text/html":
            html_parts.append(payload)

    if plain_parts:
        return "\n\n".join(plain_parts).strip()

    if html_parts:
        html = "\n\n".join(html_parts)
        # Keep first version simple: strip tags for plain-text fallback.
        text = re.sub(r"<[^>]+>", "", html)
        return text.strip()

    return "(本文をテキストとして取得できませんでした)"


def _linkify_urls(text: str) -> str:
    pattern = re.compile(r"https?://[^\s<>()\[\]{}\"']+")

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]

        if not url:
            return match.group(0)

        # OSC 8 hyperlink. Unsupported terminals will typically show the URL text as-is.
        linked = f"\033]8;;{url}\033\\{url}\033]8;;\033\\"
        return linked + trailing

    return pattern.sub(replace, text)


def _fetch_from_imap(acc: dict[str, Any]) -> list[MailItem]:
    host = acc["host"]
    port = int(acc.get("port", 993))
    user = acc["user"]
    password = acc["pass"]
    account_name = acc.get("name", user)

    logging.info(f"[{account_name}] IMAP接続: {host}:{port}")

    items: list[MailItem] = []
    src = imaplib.IMAP4_SSL(host, port)
    try:
        src.login(user, password)
        status, _ = src.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP INBOX select failed")

        status, data = src.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        ids = data[0].split() if data and data[0] else []
        for index, msg_id in enumerate(reversed(ids), 1):
            status, fetched = src.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue

            raw = fetched[0][1]
            msg = message_from_bytes(raw, policy=default)
            items.append(
                MailItem(
                    seq=index,
                    account_name=str(account_name),
                    source_user=str(user),
                    from_addr=_header_text(msg, "From", "(no from)"),
                    date=_header_text(msg, "Date", "(no date)"),
                    subject=_header_text(msg, "Subject", "(no subject)"),
                    raw_message=raw,
                )
            )
    finally:
        try:
            src.logout()
        except Exception:
            pass

    return items


def _fetch_from_pop3(acc: dict[str, Any]) -> list[MailItem]:
    host = acc["host"]
    port = int(acc.get("port", 995))
    user = acc["user"]
    password = acc["pass"]
    account_name = acc.get("name", user)

    logging.info(f"[{account_name}] POP3接続: {host}:{port}")

    items: list[MailItem] = []
    src = poplib.POP3_SSL(host, port)
    try:
        src.user(user)
        src.pass_(password)
        total = len(src.list()[1])

        for index, mail_no in enumerate(range(total, 0, -1), 1):
            resp, lines, _ = src.retr(mail_no)
            if not resp.startswith(b"+OK"):
                continue

            raw = b"\r\n".join(lines)
            msg = message_from_bytes(raw, policy=default)
            items.append(
                MailItem(
                    seq=index,
                    account_name=str(account_name),
                    source_user=str(user),
                    from_addr=_header_text(msg, "From", "(no from)"),
                    date=_header_text(msg, "Date", "(no date)"),
                    subject=_header_text(msg, "Subject", "(no subject)"),
                    raw_message=raw,
                )
            )
    finally:
        try:
            src.quit()
        except Exception:
            pass

    return items


def fetch_mail_items(acc: dict[str, Any]) -> list[MailItem]:
    acc_type = str(acc.get("type", "")).lower()
    if acc_type == "imap":
        return _fetch_from_imap(acc)
    if acc_type == "pop3":
        return _fetch_from_pop3(acc)
    raise ValueError(f"Unsupported account type: {acc_type}")


def _char_display_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in {"F", "W", "A"}:
        return 2
    return 1


def _display_width(text: str) -> int:
    return sum(_char_display_width(ch) for ch in text)


def _truncate_to_width(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text

    ellipsis = "..."
    ellipsis_width = _display_width(ellipsis)
    if width <= ellipsis_width:
        return "." * width

    out: list[str] = []
    used = 0
    limit = width - ellipsis_width

    for ch in text:
        ch_width = _char_display_width(ch)
        if used + ch_width > limit:
            break
        out.append(ch)
        used += ch_width

    return "".join(out) + ellipsis


def _fit_cell(text: str, width: int) -> str:
    trimmed = _truncate_to_width(text, width)
    pad = max(0, width - _display_width(trimmed))
    return trimmed + (" " * pad)


def _format_date_text(date_text: str) -> str:
    try:
        dt = parsedate_to_datetime(date_text)
    except Exception:
        return date_text

    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def _list_column_widths(show_account: bool) -> dict[str, int]:
    cols = shutil.get_terminal_size(fallback=(120, 30)).columns

    # no(4) + separators
    fixed_overhead = 8 if show_account else 7
    from_w = 24
    date_w = 19
    min_subject = 24
    min_account = 14

    base_total = fixed_overhead + from_w + date_w + min_subject
    if show_account:
        base_total += min_account
    if cols < base_total:
        shortage = base_total - cols
        reducible = max(0, from_w - 16)
        cut = min(shortage, reducible)
        from_w -= cut
        shortage -= cut
        reducible = max(0, date_w - 16)
        cut = min(shortage, reducible)
        date_w -= cut

    dynamic = max(0, cols - (fixed_overhead + from_w + date_w))
    if show_account:
        subject_w = max(min_subject, int(dynamic * 0.7))
        account_w = max(min_account, dynamic - subject_w)
        if subject_w + account_w > dynamic:
            subject_w = max(min_subject, dynamic - min_account)
            account_w = max(min_account, dynamic - subject_w)
    else:
        subject_w = max(min_subject, dynamic)
        account_w = 0

    return {
        "from": from_w,
        "date": date_w,
        "subject": subject_w,
        "account": account_w,
    }


def print_list(items: list[MailItem], show_account: bool = True) -> None:
    if not items:
        print("メールがありません。")
        return

    widths = _list_column_widths(show_account)
    if show_account:
        header = (
            f"{'No.':>4} "
            f"{_fit_cell('From', widths['from'])} "
            f"{_fit_cell('Date', widths['date'])} "
            f"{_fit_cell('Subject', widths['subject'])} "
            f"{_fit_cell('Account', widths['account'])}"
        )
    else:
        header = (
            f"{'No.':>4} "
            f"{_fit_cell('From', widths['from'])} "
            f"{_fit_cell('Date', widths['date'])} "
            f"{_fit_cell('Subject', widths['subject'])}"
        )

    print("\n" + header)
    print("-" * _display_width(header))
    for item in items:
        from_col = _fit_cell(item.from_addr, widths["from"])
        date_col = _fit_cell(_format_date_text(item.date), widths["date"])
        subject_col = _fit_cell(item.subject, widths["subject"])
        if show_account:
            account_col = _fit_cell(item.account_name, widths["account"])
            print(f"{item.seq:>4} {from_col} {date_col} {subject_col} {account_col}")
        else:
            print(f"{item.seq:>4} {from_col} {date_col} {subject_col}")


def print_detail(item: MailItem) -> None:
    msg = message_from_bytes(item.raw_message, policy=default)
    body = _linkify_urls(_extract_body_text(msg))

    print("\n" + "=" * 80)
    print(f"No      : {item.seq}")
    print(f"Account : {item.account_name} ({item.source_user})")
    print(f"From    : {item.from_addr}")
    print(f"Date    : {_format_date_text(item.date)}")
    print(f"Subject : {item.subject}")
    print("-" * 80)
    print(body)
    print("=" * 80 + "\n")


def interact(items: list[MailItem], show_account: bool = True) -> None:
    if not items:
        return

    index_map = {item.seq: item for item in items}
    ordered_seqs = sorted(index_map.keys())
    last_seq: int | None = None

    while True:
        value = input("番号を入力してください (qで終了): ").strip()
        if value.lower() in {"q", "quit", "exit"}:
            break

        if value.lower() == "l":
            print_list(items, show_account=show_account)
            continue

        if value == "":
            if not ordered_seqs:
                print("表示できるメールがありません。")
                continue

            if last_seq is None:
                next_seq = ordered_seqs[0]
            else:
                next_seq = None
                for seq in ordered_seqs:
                    if seq > last_seq:
                        next_seq = seq
                        break
                if next_seq is None:
                    print("最後のメールです。")
                    continue

            item = index_map[next_seq]
            print_detail(item)
            last_seq = next_seq
            continue

        if not value.isdigit():
            print("数字、l（一覧表示）、またはEnter（次のメール）を入力してください。")
            continue

        seq = int(value)
        item = index_map.get(seq)
        if not item:
            print("その番号は存在しません。")
            continue

        print_detail(item)
        last_seq = seq


def main() -> int:
    args = parse_args()
    configure_logging(args)

    try:
        accounts = load_accounts()
    except Exception as exc:
        logging.error(str(exc))
        return 1

    if args.list:
        print_account_list(accounts)
        return 0

    targets = filter_accounts(accounts, args.target)
    if not targets:
        logging.error("対象アカウントが見つかりませんでした。")
        return 1

    all_items: list[MailItem] = []
    offset = 0

    for acc in targets:
        name = acc.get("name", acc.get("user", "unknown"))
        try:
            account_items = fetch_mail_items(acc)
        except Exception as exc:
            logging.error(f"[{name}] 取得失敗: {exc}")
            continue

        # Renumber globally to keep a single interactive index across accounts.
        for item in account_items:
            item.seq += offset
        offset += len(account_items)
        all_items.extend(account_items)

    if not all_items:
        print("表示できるメールがありません。")
        return 0

    print_list(all_items, show_account=args.target is None)
    interact(all_items, show_account=args.target is None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
