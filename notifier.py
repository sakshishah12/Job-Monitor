"""Notification delivery via Telegram and/or Email."""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class JobAlert:
    company: str
    title: str
    location: str
    matched_keywords: list[str]
    url: str


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, alert: JobAlert) -> bool:
        message = self._format_message(alert)
        try:
            response = requests.post(
                self.api_url,
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=30,
            )
            response.raise_for_status()
            logger.info("Telegram alert sent for: %s", alert.title)
            return True
        except requests.RequestException as exc:
            logger.error("Telegram notification failed: %s", exc)
            return False

    @staticmethod
    def _format_message(alert: JobAlert) -> str:
        keywords = ", ".join(alert.matched_keywords)
        return (
            f"<b>New Job Match!</b>\n\n"
            f"<b>Company:</b> {alert.company}\n"
            f"<b>Title:</b> {alert.title}\n"
            f"<b>Location:</b> {alert.location or 'Not specified'}\n"
            f"<b>Keywords:</b> {keywords}\n"
            f"<b>Apply:</b> {alert.url}"
        )


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        sender: str,
        password: str,
        recipient: str,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.password = password
        self.recipient = recipient

    def send(self, alert: JobAlert) -> bool:
        subject = f"[Job Alert] {alert.company} - {alert.title}"
        body = self._format_body(alert)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.starttls()
                server.login(self.sender, self.password)
                server.sendmail(self.sender, [self.recipient], msg.as_string())
            logger.info("Email alert sent for: %s", alert.title)
            return True
        except smtplib.SMTPException as exc:
            logger.error("Email notification failed: %s", exc)
            return False

    @staticmethod
    def _format_body(alert: JobAlert) -> str:
        keywords = ", ".join(alert.matched_keywords)
        return (
            f"New Job Match!\n\n"
            f"Company: {alert.company}\n"
            f"Title: {alert.title}\n"
            f"Location: {alert.location or 'Not specified'}\n"
            f"Matched Keywords: {keywords}\n"
            f"Apply: {alert.url}\n"
        )


class NotificationManager:
    def __init__(self) -> None:
        self.telegram: Optional[TelegramNotifier] = None
        self.email: Optional[EmailNotifier] = None
        self._configure()

    def _configure(self) -> None:
        enable_telegram = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
        enable_email = os.getenv("ENABLE_EMAIL", "false").lower() == "true"

        if enable_telegram:
            token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                self.telegram = TelegramNotifier(token, chat_id)
            else:
                logger.warning("Telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")

        if enable_email:
            sender = os.getenv("EMAIL_SENDER", "")
            password = os.getenv("EMAIL_PASSWORD", "")
            recipient = os.getenv("EMAIL_RECIPIENT", sender)
            if sender and password:
                self.email = EmailNotifier(
                    smtp_host=os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com"),
                    smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
                    sender=sender,
                    password=password,
                    recipient=recipient,
                )
            else:
                logger.warning("Email enabled but EMAIL_SENDER or EMAIL_PASSWORD missing")

    def send_alert(self, alert: JobAlert) -> None:
        if not self.telegram and not self.email:
            logger.warning(
                "No notification channels configured. Set ENABLE_TELEGRAM or ENABLE_EMAIL in .env"
            )
            return

        if self.telegram:
            self.telegram.send(alert)
        if self.email:
            self.email.send(alert)
