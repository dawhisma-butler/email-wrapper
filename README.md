# sermail

A single command-line email sender for the Proofpoint SER relay
(`smtp-us.ser.proofpoint.com`). Relay host, port and credentials live in one
place — `sermail.py` — so the `.bat`, Python and scheduled jobs that send mail
never need to change when those do.

- `sermail.py` — the sender. Windows and Linux, Python 3.8+, standard library only.
- `sermail.bat` — Windows launcher so callers can run `sermail ...`.

## Setup

1. Put `sermail.py` and `sermail.bat` in one folder (e.g. `C:\Scripts\sermail`)
   and, optionally, add that folder to `PATH`.
2. Optionally set `DEFAULT_FROM` in the **RELAY SETTINGS** block at the top of
   `sermail.py`.
3. Run `sermail --store-credentials` to save the SER username and password
   encrypted (see [Encrypted credentials](#encrypted-credentials-windows)).

The sender address must belong to a domain registered with your SER account.

## Usage

```bat
sermail --to user@example.edu --subject "Backup finished" --body "All good."

sermail --from "Reports <reports@example.edu>" ^
        --to "a@example.edu;b@example.edu" --cc boss@example.edu ^
        --subject "Nightly report" --body-file C:\jobs\report.txt ^
        --attach C:\jobs\report.csv --attach C:\jobs\chart.png

type body.html | sermail --to a@example.edu --subject "Status" --html --body-file -
```

On Linux, call `sermail.py` directly with the same options.

| Option | Meaning |
| --- | --- |
| `-f`, `--from` | Sender (defaults to `DEFAULT_FROM`) |
| `-t`, `--to`, `-c`, `--cc`, `-b`, `--bcc` | Recipients; repeatable, or comma/semicolon lists |
| `--reply-to` | Reply-To address(es) |
| `-s`, `--subject` | Subject |
| `-m`, `--body` | Body text; a literal `\n` becomes a newline |
| `-F`, `--body-file` | Read body from a file, or `-` for stdin |
| `--html` | Body is HTML |
| `--encoding` | Encoding of `--body-file` (default `utf-8`) |
| `-a`, `--attach` | Attach a file; repeatable |
| `--priority` | `high`, `normal` or `low` |
| `--header "Name: value"` | Extra header; repeatable |
| `--dry-run` | Build and print the message without sending |
| `--debug` | Show the SMTP conversation |
| `-q`, `--quiet` | No output on success |

Exit codes: `0` sent, `1` send failed, `2` bad usage. In a batch file:

```bat
call sermail --to ops@example.edu --subject "Job failed" --body "See log." --attach job.log
if errorlevel 1 echo Email could not be sent
```

From Python:

```python
import subprocess
subprocess.run(["sermail.bat", "--to", "ops@example.edu", "--subject", "Done", "--body", "ok"], check=True)
```

Temporary failures (connection problems, 4xx responses) are retried twice.

## Encrypted credentials (Windows)

Leave `SMTP_USERNAME` and `SMTP_PASSWORD` blank in `sermail.py` and run this
once on each server, logged in as the account that runs the jobs:

```bat
sermail --store-credentials
```

It prompts for the SER username and password (typing is hidden) and saves them
encrypted with the Windows Data Protection API (DPAPI) in
`%LOCALAPPDATA%\sermail\sermail.cred`. Only that Windows account, on that
computer, can decrypt the file.

If several accounts on the server send mail, store one shared copy instead:

```bat
sermail --store-credentials --all-users
```

This writes `sermail.cred` next to `sermail.py`, decryptable by any account on
that computer (but still not on any other computer). Limit who can read the
folder with NTFS permissions.

When the SER password changes, re-run the same command. `sermail.cred` is tied
to one computer, so it cannot be copied between servers and is ignored by git.

Lookup order: values in `sermail.py` / `SERMAIL_*` environment variables, then
the per-user file, then the shared file, then `keyring`.

## Relay certificates

Python only trusts CA certificates already in the operating system's store,
and Windows installs many public roots lazily. If a send fails with
`CERTIFICATE_VERIFY_FAILED`, or as periodic maintenance, run:

```bat
sermail --update-certs
```

On Windows this:

1. Connects to the relay and reads the certificate chain it presents.
2. Asks Windows to build and validate that chain. Windows fetches the root from
   Microsoft's trusted root program if it is not installed yet.
3. Only if Windows trusts the chain, saves the intermediate and root CA
   certificates to `sermail-ca.pem` next to `sermail.py`.
4. Does a fully verified test connection and prints each certificate's expiry.

Every send trusts `sermail-ca.pem` in addition to the OS store. The OS
certificate store is not modified, so no administrator rights are needed
(only write access to the script folder). PowerShell is used for step 2;
OpenSSL is not required.

On Linux the command only performs the verified test connection, because the
system CA package normally already trusts the relay. If it does not, update the
`ca-certificates` package or copy `sermail-ca.pem` from a Windows server.

`sermail-ca.pem` holds public certificates only, so it can be copied to other
servers alongside `sermail.py`. To keep it current automatically, schedule
`sermail --update-certs` monthly; exit code 0 means the relay verifies.

## Other options

- **`keyring` (Windows or Linux).** `pip install keyring`, set `SMTP_USERNAME`
  in the script, then `python -m keyring set sermail <SMTP_USERNAME>`.
- **Environment variables.** Every setting can be overridden with a
  `SERMAIL_`-prefixed variable (`SERMAIL_SMTP_HOST`, `SERMAIL_SMTP_PORT`,
  `SERMAIL_SMTP_USERNAME`, `SERMAIL_SMTP_PASSWORD`, `SERMAIL_DEFAULT_FROM`, ...).
