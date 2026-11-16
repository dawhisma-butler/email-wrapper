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
import base64
import getpass
import json
import mimetypes
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
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
SMTP_USERNAME = ""              # Leave both blank and run
SMTP_PASSWORD = ""              #   sermail --store-credentials
                                # to keep them encrypted instead (see README).
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


# ---------------------------------------------------------------------------
# Encrypted credential storage (Windows DPAPI).
#
# "sermail --store-credentials" encrypts the username and password with the
# Windows Data Protection API and writes the result to a file. The encryption
# key is managed by Windows and never leaves this computer, so the file is
# useless if copied elsewhere.
#   per-user file : only the Windows account that created it can decrypt it
#   shared file   : any account on this computer can decrypt it (--all-users)
# ---------------------------------------------------------------------------
CRED_FILE_NAME = "sermail.cred"
_DPAPI_UI_FORBIDDEN = 0x1
_DPAPI_LOCAL_MACHINE = 0x4


def user_cred_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "sermail", CRED_FILE_NAME)


def shared_cred_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CRED_FILE_NAME)


def _dpapi(data, encrypt, machine_scope=False):
    """Encrypt or decrypt bytes with the Windows Data Protection API."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    flags = _DPAPI_UI_FORBIDDEN
    if encrypt:
        if machine_scope:
            flags |= _DPAPI_LOCAL_MACHINE
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "sermail", None, None,
                                      None, flags, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                        None, flags, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def store_credentials(all_users):
    """Prompt for the SER username/password and save them encrypted."""
    if os.name != "nt":
        raise UsageError("--store-credentials uses Windows DPAPI and is only "
                         "available on Windows (on Linux, see the keyring "
                         "option in the README)")
    username = input("SER SMTP username: ").strip()
    password = getpass.getpass("SER SMTP password (input hidden): ")
    if not username or not password:
        raise UsageError("username and password are both required")
    if getpass.getpass("Re-enter password: ") != password:
        raise UsageError("passwords did not match; nothing was saved")

    plain = json.dumps({"username": username, "password": password}).encode("utf-8")
    blob = _dpapi(plain, encrypt=True, machine_scope=all_users)
    path = shared_cred_path() if all_users else user_cred_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(base64.b64encode(blob))
    who = ("any account on this computer" if all_users
           else "only this Windows account (%s)" % getpass.getuser())
    print("sermail: encrypted credentials saved to %s" % path)
    print("sermail: they can be decrypted by %s" % who)


def load_stored_credentials():
    """Return (username, password) from an encrypted file, or None."""
    if os.name != "nt":
        return None
    for path in (user_cred_path(), shared_cred_path()):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                plain = _dpapi(base64.b64decode(fh.read()), encrypt=False)
            data = json.loads(plain.decode("utf-8"))
            return data["username"], data["password"]
        except Exception as exc:  # noqa: BLE001
            raise SendError("cannot decrypt %s (%s); re-run "
                            "sermail --store-credentials as this account"
                            % (path, exc))
    return None


def resolve_credentials():
    """Find the SMTP login: settings/env first, then encrypted file, then keyring."""
    username = setting("SMTP_USERNAME", SMTP_USERNAME)
    password = setting("SMTP_PASSWORD", SMTP_PASSWORD)
    if username and password:
        return username, password
    stored = load_stored_credentials()
    if stored:
        return stored
    if username:
        try:
            import keyring  # optional dependency
        except ImportError:
            pass
        else:
            password = keyring.get_password(KEYRING_SERVICE, username) or ""
    return username, password


# ---------------------------------------------------------------------------
# Relay certificate trust.
#
# Python only trusts CA certificates that are already present in the operating
# system's store. Windows installs many public root CAs lazily, the first time
# Windows itself needs them, so Python can fail with CERTIFICATE_VERIFY_FAILED
# against a perfectly valid server.
#
# "sermail --update-certs" fixes that without touching the OS store: it asks
# Windows to build and validate the relay's certificate chain (which makes
# Windows fetch the root from Microsoft's trusted root program if needed) and
# saves the intermediate and root CA certificates to sermail-ca.pem next to
# this script. Every send trusts that file in addition to the OS store.
# Nothing is saved unless Windows itself trusts the chain.
#
# The file contains only public CA certificates. It can be copied to other
# servers (Windows or Linux) along with sermail.py.
# ---------------------------------------------------------------------------
CA_BUNDLE_NAME = "sermail-ca.pem"

_CHAIN_PS = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$X509 = 'System.Security.Cryptography.X509Certificates'
$files = @(Get-ChildItem -Path $env:SERMAIL_CERT_DIR -Filter '*.der' | Sort-Object Name)
$leaf = New-Object "$X509.X509Certificate2" -ArgumentList $files[0].FullName
$chain = New-Object "$X509.X509Chain"
$chain.ChainPolicy.RevocationMode = 'NoCheck'
foreach ($f in ($files | Select-Object -Skip 1)) {
    $extra = New-Object "$X509.X509Certificate2" -ArgumentList $f.FullName
    [void]$chain.ChainPolicy.ExtraStore.Add($extra)
}
$trusted = $chain.Build($leaf)
$status = (@($chain.ChainStatus | ForEach-Object { $_.StatusInformation.Trim() }) -join '; ')
$certs = @()
foreach ($e in $chain.ChainElements) {
    $c = $e.Certificate
    $certs += @{
        subject    = $c.Subject
        issuer     = $c.Issuer
        notAfter   = $c.NotAfter.ToUniversalTime().ToString('yyyy-MM-dd')
        thumbprint = $c.Thumbprint
        der        = [Convert]::ToBase64String($c.RawData)
    }
}
@{ trusted = $trusted; status = $status; certs = $certs } | ConvertTo-Json -Depth 4 -Compress
"""


def ca_bundle_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CA_BUNDLE_NAME)


def make_tls_context():
    """OS trust store plus sermail-ca.pem, if that file exists."""
    context = ssl.create_default_context()
    path = ca_bundle_path()
    if os.path.isfile(path):
        context.load_verify_locations(cafile=path)
    return context


def fetch_presented_chain(host, port, timeout):
    """Return the DER certificates the relay presents, leaf first (unverified)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.starttls(context=context)
        sock = smtp.sock
        chain = []
        try:
            if hasattr(sock, "get_unverified_chain"):       # Python 3.13+
                chain = [bytes(c) for c in sock.get_unverified_chain()]
            else:                                           # Python 3.10 - 3.12
                import _ssl
                chain = [c.public_bytes(_ssl.ENCODING_DER)
                         for c in sock._sslobj.get_unverified_chain()]
        except Exception:  # noqa: BLE001 - fall back to the leaf alone
            chain = []
        return chain or [sock.getpeercert(binary_form=True)]


def verify_relay(host, port, timeout):
    """Do a fully verified TLS handshake; return the relay cert's expiry text."""
    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.starttls(context=make_tls_context())
        return smtp.sock.getpeercert().get("notAfter", "unknown")


def windows_build_chain(presented):
    """Have Windows build/validate the chain; return its JSON description."""
    script = base64.b64encode(_CHAIN_PS.encode("utf-16-le")).decode("ascii")
    with tempfile.TemporaryDirectory() as tmp:
        for i, der in enumerate(presented):
            with open(os.path.join(tmp, "%02d.der" % i), "wb") as fh:
                fh.write(der)
        env = dict(os.environ, SERMAIL_CERT_DIR=tmp)
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-EncodedCommand", script],
                env=env, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SendError("could not run PowerShell: %s" % exc)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SendError("PowerShell chain check failed: %s"
                        % (proc.stderr.strip() or "no output"))
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except ValueError:
        raise SendError("unexpected PowerShell output: %r" % proc.stdout[:200])


def write_ca_bundle(ca_certs):
    """Write the CA certificates as PEM; return True if the file changed."""
    parts = ["# CA certificates trusted by sermail for the SMTP relay.\n"
             "# Generated by: sermail --update-certs. Do not edit by hand.\n"]
    for c in ca_certs:
        parts.append("\n# Subject: %s\n# Expires: %s\n# SHA-1:   %s\n%s" % (
            c["subject"], c["notAfter"], c["thumbprint"],
            ssl.DER_cert_to_PEM_cert(base64.b64decode(c["der"]))))
    text = "".join(parts)
    path = ca_bundle_path()
    try:
        with open(path, "r", encoding="ascii") as fh:
            if fh.read() == text:
                return False
    except (OSError, UnicodeError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def update_certs():
    host = setting("SMTP_HOST", SMTP_HOST)
    port = setting("SMTP_PORT", SMTP_PORT)
    timeout = setting("TIMEOUT_SECONDS", TIMEOUT_SECONDS)
    print("sermail: checking certificate chain of %s:%s" % (host, port))

    if os.name == "nt":
        info = windows_build_chain(fetch_presented_chain(host, port, timeout))
        certs = info.get("certs") or []
        if isinstance(certs, dict):          # PowerShell unwraps 1-item lists
            certs = [certs]
        for i, c in enumerate(certs):
            role = "relay" if i == 0 else ("root" if c["subject"] == c["issuer"]
                                           else "intermediate")
            print("  %-12s expires %s  %s" % (role, c["notAfter"], c["subject"]))
        if not info.get("trusted"):
            raise SendError("Windows does not trust this chain (%s). "
                            "Nothing was changed." % (info.get("status") or "unknown reason"))
        if len(certs) < 2:
            raise SendError("no CA certificates found in the chain. Nothing was changed.")
        changed = write_ca_bundle(certs[1:])
        print("sermail: %s %s" % ("updated" if changed else "already up to date:",
                                  ca_bundle_path()))

    try:
        expires = verify_relay(host, port, timeout)
    except ssl.SSLCertVerificationError as exc:
        if os.name == "nt":
            raise SendError("chain still fails verification: %s" % exc)
        raise SendError(
            "%s\nThis computer's CA store does not trust the relay. Either "
            "update the OS CA package (ca-certificates), or copy %s from a "
            "Windows server where 'sermail --update-certs' succeeded into %s"
            % (exc, CA_BUNDLE_NAME, os.path.dirname(ca_bundle_path())))
    print("sermail: OK - relay certificate verifies (relay cert expires %s)" % expires)
    return EXIT_OK


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
    username, password = resolve_credentials()
    if not username or not password:
        raise SendError("SMTP credentials are not configured "
                        "(run: sermail --store-credentials)")

    context = make_tls_context()
    with smtplib.SMTP(host, port, timeout=setting("TIMEOUT_SECONDS", TIMEOUT_SECONDS)) as smtp:
        if debug:
            smtp.set_debuglevel(1)
        smtp.starttls(context=context)
        smtp.login(username, password)
        # Returns a dict of refused recipients (empty when all were accepted);
        # raises if every recipient was refused.
        return smtp.send_message(msg, from_addr=sender_addr, to_addrs=envelope_to)


def is_temporary(exc):
    if isinstance(exc, (smtplib.SMTPAuthenticationError,
                        ssl.SSLCertVerificationError)):
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
    creds = p.add_argument_group("credential setup")
    creds.add_argument("--store-credentials", action="store_true",
                       help="prompt for the SER username/password and save "
                            "them encrypted (Windows), then exit")
    creds.add_argument("--all-users", action="store_true",
                       help="with --store-credentials: let every account on "
                            "this computer use the saved credentials")
    certs = p.add_argument_group("certificate maintenance")
    certs.add_argument("--update-certs", action="store_true",
                       help="check the relay's TLS certificate chain and "
                            "refresh the CA bundle sermail trusts, then exit")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.store_credentials:
        try:
            store_credentials(args.all_users)
        except (UsageError, OSError, EOFError, KeyboardInterrupt) as exc:
            print("sermail: %s" % (exc or "cancelled"), file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    if args.update_certs:
        try:
            return update_certs()
        except (SendError, OSError, smtplib.SMTPException) as exc:
            print("sermail: certificate update failed: %s" % exc, file=sys.stderr)
            return EXIT_SEND_FAILED
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
            if isinstance(exc, ssl.SSLCertVerificationError):
                print("sermail: the relay's certificate chain is not trusted "
                      "on this computer; run: sermail --update-certs",
                      file=sys.stderr)
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
