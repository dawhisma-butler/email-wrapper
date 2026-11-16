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
2. Edit the **RELAY SETTINGS** block at the top of `sermail.py`: set
   `SMTP_USERNAME`, `SMTP_PASSWORD` and (optionally) `DEFAULT_FROM`.
3. Restrict who can read the file, since it holds the password. **Do not commit
   the filled-in password to git.**

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

## Keeping the password out of the script

Leave `SMTP_PASSWORD` blank and use either:

- **OS credential store (encrypted).** `pip install keyring`, then once, as the
  account that runs the jobs:

  ```bat
  python -m keyring set sermail <SMTP_USERNAME>
  ```

  On Windows this stores the password in Credential Manager, encrypted for that
  account. Each account that sends mail needs to store it once.
- **Environment variable** `SERMAIL_SMTP_PASSWORD`.

Every setting can be overridden the same way (`SERMAIL_SMTP_HOST`,
`SERMAIL_SMTP_PORT`, `SERMAIL_SMTP_USERNAME`, `SERMAIL_DEFAULT_FROM`, ...).
