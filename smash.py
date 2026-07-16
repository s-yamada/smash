#!/usr/bin/env python3
import argparse
import json
import logging
import os
import poplib
import re
import subprocess
import sys
import imaplib
import shutil
import termios
import tty
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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


ACCOUNTS_PATH = os.path.expanduser("~/.smash/accounts.json")


def load_accounts(path: str = ACCOUNTS_PATH) -> list[dict[str, Any]]:
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


def _get_leaf_parts(msg: Message) -> list[tuple[str, str]]:
    """添付ファイル以外のリーフパートを返す。戻り値は (content_type, decoded_text) のリスト。"""

    def decode_part(part: Message) -> str:
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    if msg.is_multipart():
        results = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            results.append((part.get_content_type(), decode_part(part)))
        return results

    return [(msg.get_content_type(), decode_part(msg))]


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


class _HtmlToText(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"})
    _SKIP_TAGS = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._link_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag == "a" and self._skip_depth == 0:
            self._link_stack.append(dict(attrs).get("href", ""))
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "a" and self._link_stack:
            href = self._link_stack.pop()
            if href and self._skip_depth == 0:
                self._parts.append(f"\n{href}")
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS and tag != "br":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).splitlines()]
        result: list[str] = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    result.append("")
                prev_blank = True
            else:
                result.append(line)
                prev_blank = False
        return "\n".join(result).strip()


def _html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
    except Exception:
        return html
    return parser.get_text()


def _render_body(ctype: str, text: str) -> str:
    if ctype == "text/plain":
        return _linkify_urls(text)
    if ctype == "text/html":
        return _html_to_text(text)
    return text


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


def _parse_date_for_sort(date_text: str) -> datetime:
    """Dateヘッダをソート用のdatetimeに変換する。パース失敗時は最古扱いにして末尾に回す。"""
    try:
        dt = parsedate_to_datetime(date_text)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def _page_or_print(text: str) -> None:
    """端末の高さを超える場合は less / more でページングする。"""
    term_height = shutil.get_terminal_size(fallback=(120, 30)).lines
    if text.count("\n") < term_height - 2:
        print(text)
        return
    for cmd in (["less", "-R", "-F", "-X"], ["more"]):
        try:
            subprocess.run(cmd, input=text, text=True, check=False)
            return
        except (FileNotFoundError, OSError):
            continue
    print(text)


def print_detail(item: MailItem) -> None:
    msg = message_from_bytes(item.raw_message, policy=default)

    header = (
        "\n" + "=" * 80 + "\n"
        + f"No      : {item.seq}\n"
        + f"Account : {item.account_name} ({item.source_user})\n"
        + f"From    : {item.from_addr}\n"
        + f"Date    : {_format_date_text(item.date)}\n"
        + f"Subject : {item.subject}\n"
        + "-" * 80
    )

    parts = _get_leaf_parts(msg)
    if not parts:
        print(header)
        print("(本文をテキストとして取得できませんでした)")
        print("=" * 80 + "\n")
        return

    if len(parts) == 1:
        ctype, text = parts[0]
        _page_or_print(header + "\n" + _render_body(ctype, text) + "\n" + "=" * 80 + "\n")
    else:
        print(header)
        print(f"[マルチパート: {len(parts)} パート]")
        for i, (ctype, _) in enumerate(parts, 1):
            print(f"  {i}. {ctype}")
        print()

        selected = None
        while selected is None:
            value = input(f"表示するパートを選んでください (1-{len(parts)}, Enter でデフォルト): ").strip()
            if value == "":
                # デフォルト: text/plain があれば優先、なければ先頭
                selected = next(
                    (i for i, (ct, _) in enumerate(parts) if ct == "text/plain"),
                    0,
                )
            elif value.isdigit() and 1 <= int(value) <= len(parts):
                selected = int(value) - 1
            else:
                print(f"1〜{len(parts)} の数字を入力してください。")

        ctype, text = parts[selected]
        body_header = f"--- パート {selected + 1}: {ctype} ---\n"
        _page_or_print(body_header + _render_body(ctype, text) + "\n" + "=" * 80 + "\n")


_PROMPT = "[n=次 p=前 l=一覧 番号=指定 q=終了 ^L=リセット ^R=再読込] "


def _read_command(prompt: str) -> str:
    """プロンプトを表示してコマンドを読む。英字は1文字即時確定、数字はEnterまで蓄積。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x0c":  # Ctrl+L
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "\x0c"
            if ch == "\x12":  # Ctrl+R
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "\x12"
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break
            if ch in ("\x7f", "\x08"):  # Backspace
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if not ch.isprintable():
                continue
            # 英字など非数字の1文字は即時確定
            if not chars and not ch.isdigit():
                sys.stdout.write(ch + "\r\n")
                sys.stdout.flush()
                return ch
            chars.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return "".join(chars)


def interact(items: list[MailItem], show_account: bool = True, reload_fn=None) -> None:
    if not items:
        return

    def _make_state(mail_items: list[MailItem]):
        imap = {m.seq: m for m in mail_items}
        return imap, sorted(imap.keys())

    index_map, ordered_seqs = _make_state(items)
    last_seq: int | None = None

    while True:
        try:
            cmd = _read_command(_PROMPT)
        except KeyboardInterrupt:
            break

        cmd = cmd.strip()

        if cmd.lower() in ("q", "x"):
            break

        if cmd == "\x0c":  # Ctrl+L: 端末クリア＋一覧再表示
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            print_list(items, show_account=show_account)
            continue

        if cmd == "\x12":  # Ctrl+R: 再読み込み
            if reload_fn is None:
                print("再読み込みは利用できません。")
                continue
            print("再読み込み中...")
            new_items = reload_fn()
            if new_items is not None:
                items = new_items
                index_map, ordered_seqs = _make_state(items)
                last_seq = None
            print_list(items, show_account=show_account)
            continue

        if cmd.lower() == "l":
            print_list(items, show_account=show_account)
            continue

        if cmd.lower() in ("n", ""):
            if not ordered_seqs:
                print("表示できるメールがありません。")
                continue
            if last_seq is None:
                next_seq = ordered_seqs[0]
            else:
                next_seq = next((s for s in ordered_seqs if s > last_seq), None)
                if next_seq is None:
                    print("最後のメールです。")
                    continue
            item = index_map[next_seq]
            print_detail(item)
            last_seq = next_seq
            continue

        if cmd.lower() == "p":
            if last_seq is None:
                print("前のメールはありません。")
                continue
            prev_seq = None
            for s in ordered_seqs:
                if s >= last_seq:
                    break
                prev_seq = s
            if prev_seq is None:
                print("最初のメールです。")
                continue
            item = index_map[prev_seq]
            print_detail(item)
            last_seq = prev_seq
            continue

        if cmd.isdigit():
            seq = int(cmd)
            item = index_map.get(seq)
            if not item:
                print("その番号は存在しません。")
                continue
            print_detail(item)
            last_seq = seq
            continue

        print("コマンドが認識できません。n=次 p=前 l=一覧 番号=指定 q=終了")


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

    show_account = args.target is None

    def _fetch_all() -> list[MailItem]:
        result: list[MailItem] = []
        for acc in targets:
            name = acc.get("name", acc.get("user", "unknown"))
            try:
                account_items = fetch_mail_items(acc)
            except Exception as exc:
                logging.error(f"[{name}] 取得失敗: {exc}")
                continue
            result.extend(account_items)

        result.sort(key=lambda item: _parse_date_for_sort(item.date), reverse=True)
        for seq, item in enumerate(result, 1):
            item.seq = seq
        return result

    all_items = _fetch_all()

    if not all_items:
        print("表示できるメールがありません。")
        return 0

    print_list(all_items, show_account=show_account)
    interact(all_items, show_account=show_account, reload_fn=_fetch_all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
