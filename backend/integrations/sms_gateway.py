"""SMS / email / push gateway adapter. The mock never writes OTPs or message bodies to logs."""
from __future__ import annotations

import logging
import sqlite3

from core import mask_email, mask_mobile, new_id, now_iso

from .base import resolve

log = logging.getLogger("asr.sms")


class MockSmsGateway:
    is_mock = True

    def send_sms(self, mobile: str, text: str) -> dict:
        log.info("MOCK SMS to %s (%d chars)", mask_mobile(mobile), len(text))
        return {"message_id": "MOCK-SMS-" + new_id()[:8], "delivered_at": now_iso(), "mock": True}

    def send_email(self, email: str, subject: str, body: str) -> dict:
        log.info("MOCK email to %s: %s", mask_email(email), subject[:60])
        return {"message_id": "MOCK-MAIL-" + new_id()[:8], "delivered_at": now_iso(), "mock": True}

    def send_push(self, device_ref: str, title: str, body: str) -> dict:
        return {"message_id": "MOCK-PUSH-" + new_id()[:8], "delivered_at": now_iso(), "mock": True}


def get(conn: sqlite3.Connection):
    return resolve(conn, "sms_gateway", MockSmsGateway)
