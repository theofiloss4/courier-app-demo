# =============================================================================
# Email delivery service: sends the shipment voucher/receipt by email.
#
# SMTP settings are optional (see app/config.py) - if they are not
# configured, the app must still function (just without emailing), which is
# why this returns an explicit status enum instead of raising an exception
# on missing configuration. In local development, Docker Compose points
# this at Mailpit, a fake SMTP server with a web inbox at localhost:8025,
# so no real emails are ever sent during development/testing.
# =============================================================================
import smtplib
from email.message import EmailMessage
from enum import StrEnum
from html import escape

from app.config import get_settings
from app.models.shipment import Shipment


class EmailDeliveryStatus(StrEnum):
    """Outcome of attempting to send a voucher email.

    Returned instead of raising an exception so the calling router (see
    app/routers/shipments.py) can decide how to react - e.g. still show the
    shipment successfully created page, but with a warning banner if the
    email could not be delivered.
    """

    SENT = "sent"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


def send_voucher_email(shipment: Shipment) -> EmailDeliveryStatus:
    """Email the shipment voucher (tracking number, weight, price) to the sender.

    Builds a message with both a plain-text and an HTML version (multipart
    email) so it displays well in any email client, then sends it over SMTP
    using the configured server. Returns a status instead of raising, so
    email failures never crash the shipment-creation flow itself.
    """

    settings = get_settings()
    # SMTP is optional: if it is not configured at all, skip sending
    # entirely rather than attempting a connection that would just fail.
    if not settings.smtp_host or not settings.smtp_from_email:
        return EmailDeliveryStatus.NOT_CONFIGURED

    tracking_url = f"{settings.public_base_url}/track/{shipment.tracking_number}"
    message = EmailMessage()
    message["Subject"] = f"Voucher αποστολής {shipment.tracking_number}"
    message["From"] = settings.smtp_from_email
    message["To"] = shipment.sender_email
    # set_content() defines the plain-text fallback body, shown by email
    # clients that cannot (or choose not to) render HTML.
    message.set_content(
        f"""Η αποστολή σας καταχωρίστηκε.

Tracking: {shipment.tracking_number}
Παραλήπτης: {shipment.recipient_name}
Χρεώσιμο βάρος: {shipment.chargeable_weight_kg:.2f} kg
Ενδεικτική χρέωση: {shipment.amount_eur:.2f} EUR
Η πληρωμή πραγματοποιείται στο εξωτερικό POS του καταστήματος.
Παρακολούθηση: {tracking_url}
"""
    )
    # Shipment fields (recipient name, tracking number) originate from user
    # input and are inserted directly into an HTML string below, so they
    # must be HTML-escaped here to prevent HTML/script injection into the
    # rendered email (an email-specific form of XSS).
    safe_tracking_number = escape(shipment.tracking_number)
    safe_recipient_name = escape(shipment.recipient_name)
    safe_tracking_url = escape(tracking_url, quote=True)
    # add_alternative() attaches the richer HTML version alongside the
    # plain-text one created above; compliant email clients prefer this one.
    message.add_alternative(
        f"""
        <h2>Courier App Demo</h2>
        <p>Η αποστολή σας καταχωρίστηκε.</p>
        <table>
          <tr><th>Tracking</th><td><strong>{safe_tracking_number}</strong></td></tr>
          <tr><th>Παραλήπτης</th><td>{safe_recipient_name}</td></tr>
          <tr><th>Χρεώσιμο βάρος</th><td>{shipment.chargeable_weight_kg:.2f} kg</td></tr>
          <tr><th>Ενδεικτική χρέωση</th><td>{shipment.amount_eur:.2f} EUR</td></tr>
        </table>
        <p>Η πληρωμή πραγματοποιείται στο εξωτερικό POS του καταστήματος.</p>
        <p><a href="{safe_tracking_url}">Παρακολούθηση αποστολής</a></p>
        """,
        subtype="html",
    )

    try:
        # `with` ensures the SMTP connection is always closed, even if
        # sending fails partway through.
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_use_tls:
                # Upgrades the plain connection to an encrypted one before
                # any credentials or message content are sent.
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return EmailDeliveryStatus.SENT
    except (OSError, smtplib.SMTPException):
        # Covers network errors (OSError, e.g. connection refused/timeout)
        # and SMTP protocol errors (e.g. rejected recipient, auth failure).
        # Any of these degrade gracefully to a FAILED status instead of
        # crashing the request that triggered the email.
        return EmailDeliveryStatus.FAILED
