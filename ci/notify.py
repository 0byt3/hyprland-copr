"""Guarded SMTP failure notifications when all required SMTP settings are configured."""

from email.message import EmailMessage
from email.utils import formatdate
import os
from pathlib import Path
import smtplib
import socket
import ssl
import stat
import sys

from hyprland_copr.config import smtp_configured
from hyprland_copr.model import PipelineError


def check_smtp_credentials(config, roots=()):
    if not smtp_configured(config):
        return None
    path = Path(config["smtp"]["password_file"])
    if not path.is_absolute():
        raise PipelineError("smtp.password_file must be an absolute path")
    resolved = path.resolve()
    if any(resolved.is_relative_to(Path(root).resolve()) for root in roots):
        raise PipelineError("Keep SMTP credentials outside source, state, and artifact directories")
    try:
        metadata = path.lstat()
    except OSError:
        raise PipelineError("Provide a protected SMTP password file outside the checkout") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise PipelineError("SMTP password file must be a regular, user-owned file with mode 0600 or 0400")
    return path


def _read_password(config, roots=()):
    path = check_smtp_credentials(config, roots)
    if path is None:
        return None
    try:
        value = path.read_text().strip()
    except OSError:
        raise PipelineError("Cannot read SMTP password file") from None
    if not value or "\n" in value or "\r" in value:
        raise PipelineError("SMTP password file must contain a single non-empty line")
    return value


def send_failure_email(config, result, *, roots=(), output=None):
    if not smtp_configured(config):
        return False
    smtp = config["smtp"]
    password = _read_password(config, roots)
    hostname = socket.gethostname()
    status = result.get("status", "failed")
    phase = result.get("phase", "startup")
    component = result.get("component", "stack")
    destination = "/".join(str(item) for item in result.get("destination") or ())
    message = EmailMessage()
    message["From"] = smtp["from_address"]
    message["To"] = ", ".join(smtp["to_addresses"])
    message["Date"] = formatdate(localtime=True)
    message["Subject"] = f"[hyprland-copr] {status} on {hostname} during {phase} ({component})"
    lines = [
        f"Host: {hostname}",
        f"Destination: {destination or 'unconfigured'}",
        f"Component: {component}",
        f"Version: {result.get('version') or 'latest'}",
        f"Phase: {phase}",
        f"Status: {status}",
        f"Exit code: {result.get('exit_code', 1)}",
        f"Started at: {result.get('started_at', 'unknown')}",
        f"Finished at: {result.get('finished_at', 'unknown')}",
        f"Run directory: {output or 'not created'}",
        f"Error: {result.get('error', 'unknown error')}",
    ]
    message.set_content("\n".join(lines) + "\n")
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if smtp["security"] == "tls" else smtplib.SMTP
    options = {"context": context} if smtp["security"] == "tls" else {}
    with factory(
        smtp["host"], smtp["port"], timeout=smtp["timeout_seconds"], **options,
    ) as client:
        code, response = client.ehlo()
        if code != 250:
            raise smtplib.SMTPHeloError(code, response)
        if smtp["security"] == "starttls":
            client.starttls(context=context)
            code, response = client.ehlo()
            if code != 250:
                raise smtplib.SMTPHeloError(code, response)
        client.login(smtp["username"], password)
        refused = client.send_message(
            message,
            from_addr=smtp["from_address"],
            to_addrs=smtp["to_addresses"],
        )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    return True


def notify_failure(config, result, *, roots=(), output=None):
    if not smtp_configured(config):
        return False
    try:
        sent = send_failure_email(config, result, roots=roots, output=output)
    except Exception as error:
        print(
            f"Failure notification email could not be sent ({type(error).__name__})",
            file=sys.stderr,
            flush=True,
        )
        return False
    if sent:
        print("Sent failure notification email", file=sys.stderr, flush=True)
    return sent
