"""Outbound email for password resets.

Configured through environment variables:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM

When they are not set the mailer refuses to send (fail closed): a password
reset is never handed out through an insecure fallback channel, and the
caller still answers the user with a generic message.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


def configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def send(to_address, subject, body):
    """Send an email. Returns True on success, False if not configured/failed."""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ.get("SMTP_USER") or ""
    password = os.environ.get("SMTP_PASSWORD") or ""
    sender = os.environ.get("MAIL_FROM") or ""
    if not host or not sender or not to_address:
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except Exception:
                    pass
                if user:
                    s.login(user, password)
                s.send_message(msg)
        return True
    except Exception as e:
        print("[mailer] send failed: %s" % type(e).__name__, flush=True)
        return False
