#!/usr/bin/env python3
"""
sermail.py - central command-line email sender for Proofpoint SER.

All relay settings (host, port, credentials) live in this one file so that
calling scripts (.bat, Python, cron jobs, ...) never need to know them.

Runs on Windows and Linux with a stock Python 3.8+ install (no third-party
packages required).

Examples:
    sermail.py --to user@example.edu --subject "Job done" --body "All good."
    sermail.py --to a@x.edu,b@x.edu --cc boss@x.edu --subject "Report" ^
               --body-file report.txt --attach report.csv --attach chart.png
    type body.html | sermail.py --to a@x.edu --subject "Hi" --html --body-file -

Exit codes:
    0  message accepted by the relay (or --dry-run succeeded)
    1  send failed (connection, authentication, rejected by relay, ...)
    2  bad command-line usage (missing recipient, attachment not found, ...)
"""

import argparse
import mimetypes
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

# ============================================================================
# RELAY SETTINGS - edit here when the relay or credentials change.
#
# Each value can also be overridden with an environment variable of the same
# name prefixed with SERMAIL_ (e.g. SERMAIL_SMTP_PASSWORD), which is handy for
# testing and keeps secrets out of this file if you prefer.
# ============================================================================
SMTP_HOST = "smtp-us.ser.proofpoint.com"
SMTP_PORT = 587                 # SER requires STARTTLS on 587
SMTP_USERNAME = ""              # <-- SER SMTP username
SMTP_PASSWORD = ""              # <-- SER SMTP password (see README for
                                #     keeping this out of the script)
DEFAULT_FROM = ""               # used when --from is not given, e.g.
                                # "IT Notifications <noreply@example.edu>"
TIMEOUT_SECONDS = 30
RETRIES = 2                     # extra attempts on temporary failures
RETRY_DELAY_SECONDS = 5
# ============================================================================

# Name under which the password is looked up in the OS credential store
# (Windows Credential Manager / Linux Secret Service) when SMTP_PASSWORD is
# blank and the optional "keyring" package is installed.
KEYRING_SERVICE = "sermail"

EXIT_OK, EXIT_SEND_FAILED, EXIT_USAGE = 0, 1, 2


def setting(name, default):
    """Return SERMAIL_<name> from the environment, else the value above."""
    value = os.environ.get("SERMAIL_" + name)
    if value is None or value == "":
        return default
    return type(default)(value)


def resolve_password(username):
    password = setting("SMTP_PASSWORD", SMTP_PASSWORD)
    if password:
        return password
    try:
        import keyring  # optional dependency
    except ImportError:
        return ""
    return keyring.get_password(KEYRING_SERVICE, username) or ""


def split_addresses(values):
    """Flatten repeated options and comma/semicolon separated lists."""
    if not values:
        return []
    joined = ",".join(values).replace(";", ",")
    return [(name, addr) for name, addr in getaddresses([joined]) if addr]


def format_addresses(pairs):
    from email.utils import formataddr
    return ", ".join(formataddr(p) for p in pairs)


def read_body(args):
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.read()
        with open(args.body_file, "r", encoding=args.encoding) as fh:
            return fh.read()
    # Let callers write "\n" on a command line, where real newlines are awkward
    # (especially in .bat files).
    return (args.body or "").replace("\\n", "\n")


def build_message(args):
    sender = args.sender or setting("DEFAULT_FROM", DEFAULT_FROM)
    if not sender:
        raise UsageError("no sender: pass --from or set DEFAULT_FROM in sermail.py")

    to, cc, bcc = (split_addresses(v) for v in (args.to, args.cc, args.bcc))
    if not (to or cc or bcc):
        raise UsageError("no recipients: pass at least one of --to, --cc, --bcc")

    msg = EmailMessage()
    msg["From"] = sender
    if to:
        msg["To"] = format_addresses(to)
    if cc:
        msg["Cc"] = format_addresses(cc)
    # Bcc is deliberately not written as a header; those addresses are only
    # added to the SMTP envelope below.
    if args.reply_to:
        msg["Reply-To"] = format_addresses(split_addresses(args.reply_to))
    msg["Subject"] = args.subject or ""
    msg["Date"] = formatdate(localtime=True)
    sender_addr = getaddresses([sender])[0][1]
    domain = sender_addr.rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)

    if args.priority == "high":
        msg["X-Priority"], msg["Importance"] = "1", "High"
    elif args.priority == "low":
        msg["X-Priority"], msg["Importance"] = "5", "Low"

    for header in args.header or []:
        name, sep, value = header.partition(":")
        if not sep or not name.strip():
            raise UsageError("--header must look like 'Name: value', got %r" % header)
        msg[name.strip()] = value.strip()

    msg.set_content(read_body(args), subtype="html" if args.html else "plain")

    for path in args.attach or []:
        if not os.path.isfile(path):
            raise UsageError("attachment not found: %s" % path)
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))

    envelope_to = [addr for _, addr in to + cc + bcc]
    return msg, sender_addr, envelope_to


def send(msg, sender_addr, envelope_to, debug=False):
    host = setting("SMTP_HOST", SMTP_HOST)
    port = setting("SMTP_PORT", SMTP_PORT)
    username = setting("SMTP_USERNAME", SMTP_USERNAME)
    password = resolve_password(username)
    if not username or not password:
        raise SendError("SMTP credentials are not configured "
                        "(set SMTP_USERNAME / SMTP_PASSWORD in sermail.py)")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=setting("TIMEOUT_SECONDS", TIMEOUT_SECONDS)) as smtp:
        if debug:
            smtp.set_debuglevel(1)
        smtp.starttls(context=context)
        smtp.login(username, password)
        # Returns a dict of refused recipients (empty when all were accepted);
        # raises if every recipient was refused.
        return smtp.send_message(msg, from_addr=sender_addr, to_addrs=envelope_to)


def is_temporary(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return False
    return isinstance(exc, (smtplib.SMTPServerDisconnected,
                            smtplib.SMTPConnectError, OSError))


class UsageError(Exception):
    pass


class SendError(Exception):
    pass


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="sermail",
        description="Send an email through the Proofpoint SER relay.",
        epilog="Address options may be repeated and/or contain comma- or "
               "semicolon-separated lists.")
    p.add_argument("-f", "--from", dest="sender", metavar="ADDR",
                   help="sender address (default: DEFAULT_FROM in the script)")
    p.add_argument("-t", "--to", action="append", metavar="ADDR")
    p.add_argument("-c", "--cc", action="append", metavar="ADDR")
    p.add_argument("-b", "--bcc", action="append", metavar="ADDR")
    p.add_argument("--reply-to", action="append", metavar="ADDR")
    p.add_argument("-s", "--subject", default="")
    body = p.add_mutually_exclusive_group()
    body.add_argument("-m", "--body", help=r"message text (\n becomes a newline)")
    body.add_argument("-F", "--body-file", metavar="FILE",
                      help="read the message body from FILE ('-' for stdin)")
    p.add_argument("--html", action="store_true", help="body is HTML")
    p.add_argument("--encoding", default="utf-8",
                   help="encoding of --body-file (default: utf-8)")
    p.add_argument("-a", "--attach", action="append", metavar="FILE",
                   help="file to attach (repeatable)")
    p.add_argument("--priority", choices=["high", "normal", "low"], default="normal")
    p.add_argument("--header", action="append", metavar="'Name: value'",
                   help="extra message header (repeatable)")
    p.add_argument("--dry-run", action="store_true",
                   help="build the message and print it, but do not send")
    p.add_argument("--debug", action="store_true",
                   help="print the SMTP conversation to stderr")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print nothing on success")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        msg, sender_addr, envelope_to = build_message(args)
    except (UsageError, OSError, UnicodeError) as exc:
        print("sermail: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        print("Envelope from: %s" % sender_addr)
        print("Envelope to:   %s" % ", ".join(envelope_to))
        print("-" * 60)
        text = msg.as_string()
        print(text if len(text) < 5000 else text[:5000] + "\n[... truncated ...]")
        return EXIT_OK

    attempts = 1 + max(0, setting("RETRIES", RETRIES))
    for attempt in range(1, attempts + 1):
        try:
            refused = send(msg, sender_addr, envelope_to, debug=args.debug)
        except Exception as exc:  # noqa: BLE001 - report every failure uniformly
            if attempt < attempts and is_temporary(exc):
                print("sermail: attempt %d failed (%s), retrying..." % (attempt, exc),
                      file=sys.stderr)
                time.sleep(setting("RETRY_DELAY_SECONDS", RETRY_DELAY_SECONDS))
                continue
            print("sermail: send failed: %s" % exc, file=sys.stderr)
            return EXIT_SEND_FAILED
        for addr, (code, reason) in refused.items():
            reason = reason.decode(errors="replace") if isinstance(reason, bytes) else reason
            print("sermail: recipient refused: %s (%s %s)" % (addr, code, reason),
                  file=sys.stderr)
        if not args.quiet:
            print("sermail: sent to %d recipient(s)" % (len(envelope_to) - len(refused)))
        return EXIT_SEND_FAILED if refused else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
