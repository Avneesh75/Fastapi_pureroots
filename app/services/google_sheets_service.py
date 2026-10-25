"""Google Sheets lead sink: appends one row per enquiry via Sheets API v4 ``values:append``.

Authentication uses a service account built from ``GOOGLE_SERVICE_ACCOUNT_EMAIL`` and
``GOOGLE_PRIVATE_KEY`` (scope ``https://www.googleapis.com/auth/spreadsheets``). Share the
spreadsheet with the service-account email as an Editor.
"""
import logging
import threading
from typing import Any
from urllib.parse import quote

from app.config import Settings
from app.services.lead_sink import LeadRecord, LeadSinkError

logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleSheetsLeadSink:
    enabled = True

    def __init__(
        self,
        spreadsheet_id: str,
        worksheet: str,
        service_account_email: str,
        private_key: str,
        *,
        session: Any | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.worksheet = worksheet or "Sheet1"
        self._service_account_email = service_account_email
        self._private_key = private_key
        self._session = session
        self._timeout = timeout
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleSheetsLeadSink":
        return cls(
            spreadsheet_id=settings.google_sheets_spreadsheet_id,
            worksheet=settings.google_sheets_worksheet,
            service_account_email=settings.google_service_account_email,
            private_key=settings.google_private_key,
        )

    @property
    def range_a1(self) -> str:
        """``'Enquiries'!A:I`` — the 9 columns of the lead row."""
        escaped = self.worksheet.replace("'", "''")
        return f"'{escaped}'!A:I"

    @property
    def append_url(self) -> str:
        return f"{SHEETS_API_BASE}/{self.spreadsheet_id}/values/{quote(self.range_a1, safe='')}:append"

    def _get_session(self) -> Any:
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from google.auth.transport.requests import AuthorizedSession
                    from google.oauth2 import service_account

                    credentials = service_account.Credentials.from_service_account_info(
                        {
                            "type": "service_account",
                            "client_email": self._service_account_email,
                            "private_key": self._private_key,
                            "token_uri": TOKEN_URI,
                        },
                        scopes=[SHEETS_SCOPE],
                    )
                    self._session = AuthorizedSession(credentials)
        return self._session

    def push(self, lead: LeadRecord) -> None:
        body = {"majorDimension": "ROWS", "values": [lead.to_row()]}
        params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
        try:
            response = self._get_session().post(self.append_url, params=params, json=body, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - network/auth errors from google-auth/requests
            raise LeadSinkError(f"Google Sheets request failed: {type(exc).__name__}: {exc}") from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            excerpt = str(getattr(response, "text", ""))[:300]
            raise LeadSinkError(f"Google Sheets API returned HTTP {status}: {excerpt}")
        logger.info("Lead appended to Google Sheets")
