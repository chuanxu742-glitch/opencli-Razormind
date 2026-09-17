"""Shared browser-login session state classifications."""

from backend.models.browser import BrowserAccountStatus

ACTIVE_BROWSER_SESSION_STATUSES = (
    BrowserAccountStatus.OPENING.value,
    BrowserAccountStatus.PRESENTING.value,
    BrowserAccountStatus.REFRESHING.value,
    BrowserAccountStatus.VERIFYING.value,
    BrowserAccountStatus.CHALLENGE.value,
    BrowserAccountStatus.UNKNOWN.value,
    BrowserAccountStatus.SAVING.value,
)
