"""Gmail: send over SMTP, and read the inbox over IMAP to spot replies and bounces.

Both use a Google App Password. The functions here block, so callers wrap them in
asyncio.to_thread.
"""

from __future__ import annotations

import email
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES X-FAILED-RECIPIENTS)])"
DAEMONS = ("mailer-daemon", "postmaster")


@dataclass(frozen=True)
class Mailbox:
    address: str
    password: str
    name: str
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_host: str = "imap.gmail.com"


@dataclass
class InboxReport:
    replied_ids: set[str] = field(default_factory=set)  # our Message-IDs that someone answered
    replied_from: set[str] = field(default_factory=set)  # watched addresses that wrote to us
    bounced: set[str] = field(default_factory=set)  # watched addresses that bounced


def build_message(box: Mailbox, to: str, subject: str, body: str, *, in_reply_to: str | None = None,
                  attachment: Path | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((box.name, box.address))
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=box.address.rsplit("@", 1)[-1])
    if in_reply_to:  # keeps a follow-up in the same thread
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)
    if attachment:
        message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf",
                               filename=attachment.name)
    return message


def send(box: Mailbox, message: EmailMessage) -> None:
    with smtplib.SMTP(box.smtp_host, box.smtp_port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(box.address, box.password)
        smtp.send_message(message)


def check_login(box: Mailbox) -> None:
    """Log in to SMTP and IMAP without sending anything; raises on bad credentials."""
    with smtplib.SMTP(box.smtp_host, box.smtp_port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(box.address, box.password)
    with imaplib.IMAP4_SSL(box.imap_host, ssl_context=ssl.create_default_context()) as imap:
        imap.login(box.address, box.password)


def read_headers(raw: bytes, message_ids: set[str], addresses: set[str], report: InboxReport) -> bool:
    """Fold one message's headers into the report. Returns True for a bounce that names nobody we watch."""
    headers = email.message_from_bytes(raw)
    sender = parseaddr(headers.get("From", ""))[1].lower()
    if sender.startswith(DAEMONS):
        failed = {addr.lower() for _, addr in getaddresses([headers.get("X-Failed-Recipients", "")]) if addr}
        report.bounced |= failed & addresses
        return not failed
    threaded = set(re.findall(r"<[^<>\s]+>", f"{headers.get('In-Reply-To', '')} {headers.get('References', '')}"))
    answered = threaded & message_ids
    if answered:
        report.replied_ids |= answered
    if sender in addresses:
        report.replied_from.add(sender)
    return False


def scan_inbox(box: Mailbox, since: date, message_ids: set[str], addresses: set[str],
               limit: int = 800) -> InboxReport:
    """Look through INBOX since `since` for replies to our emails and bounces of them."""
    report = InboxReport()
    addresses = {a.lower() for a in addresses}
    with imaplib.IMAP4_SSL(box.imap_host, ssl_context=ssl.create_default_context()) as imap:
        imap.login(box.address, box.password)
        imap.select("INBOX", readonly=True)
        _, found = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        numbers = (found[0] or b"").split()[-limit:]
        if not numbers:
            return report
        _, fetched = imap.fetch(b",".join(numbers), HEADERS)
        unclear: list[bytes] = []
        for item in fetched:
            if isinstance(item, tuple) and read_headers(item[1], message_ids, addresses, report):
                unclear.append(item[0].split()[0])
        if unclear:  # bounce notices without X-Failed-Recipients: look for the address in the body
            _, bodies = imap.fetch(b",".join(unclear), "(BODY.PEEK[TEXT]<0.6000>)")
            for item in bodies:
                if isinstance(item, tuple):
                    text = item[1].decode("utf-8", "replace").lower()
                    report.bounced |= {a for a in addresses if a in text}
    return report
