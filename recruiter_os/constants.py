"""Enumerations and the email status state machine."""
from __future__ import annotations

from enum import Enum


class S(str, Enum):
    """Email message lifecycle statuses (persisted as plain strings)."""
    DRAFT = "Draft"
    AWAITING = "Awaiting Approval"
    APPROVED = "Approved"
    SCHEDULED = "Scheduled"
    SENDING = "Sending"
    SUBMITTED = "Submitted to Gmail"
    UNKNOWN = "Delivery Unknown"
    BOUNCED = "Bounced"
    REPLIED = "Replied"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    SUPPRESSED = "Suppressed"
    REJECTED = "Rejected"


# Allowed transitions. Anything else raises InvalidTransition.
TRANSITIONS: dict[str, set[str]] = {
    S.DRAFT: {S.AWAITING, S.APPROVED, S.REJECTED, S.CANCELLED, S.SUPPRESSED},
    S.AWAITING: {S.DRAFT, S.APPROVED, S.REJECTED, S.CANCELLED, S.SUPPRESSED},
    S.APPROVED: {S.DRAFT, S.AWAITING, S.SCHEDULED, S.CANCELLED, S.SUPPRESSED},
    S.SCHEDULED: {S.APPROVED, S.SENDING, S.CANCELLED, S.SUPPRESSED},
    S.SENDING: {S.SUBMITTED, S.FAILED, S.UNKNOWN, S.SCHEDULED},  # SCHEDULED = transient-failure retry
    S.SUBMITTED: {S.BOUNCED, S.REPLIED, S.UNKNOWN},
    S.UNKNOWN: {S.SUBMITTED, S.BOUNCED, S.REPLIED, S.SCHEDULED},  # SCHEDULED only via explicit manual re-queue
    S.FAILED: {S.SCHEDULED, S.CANCELLED},  # manual retry of eligible failures
    S.BOUNCED: set(),
    S.REPLIED: {S.BOUNCED},
    S.CANCELLED: set(),
    S.SUPPRESSED: set(),
    S.REJECTED: {S.DRAFT},
}
# Statuses that mean "the email left (or may have left) our hands"
SENT_STATES = {S.SUBMITTED, S.UNKNOWN, S.BOUNCED, S.REPLIED}
# Statuses that still occupy the "one initial outreach per contact" slot
PENDING_STATES = {S.DRAFT, S.AWAITING, S.APPROVED, S.SCHEDULED, S.SENDING}
INACTIVE_STATES = {S.CANCELLED, S.REJECTED}

VERIFICATION = ["Unreviewed", "Syntax Valid", "Domain Checked", "Verified Through Reliable Source",
                "Invalid", "Suppressed", "Ready for Outreach"]
RELEVANCE = ["Unreviewed", "Relevant", "Maybe", "Not Relevant"]
REPLY_CATEGORIES = ["Positive Response", "Request for Resume", "Request for More Information",
                    "Opportunity Shared", "Not Interested", "Rejection", "Out of Office", "Unclassified"]
CAMPAIGN_STATES = ["Draft", "Active", "Paused", "Stopped", "Completed"]
ROLE_CATEGORIES = ["SDE Intern", "SWE Intern", "Backend Intern", "Full-Stack Intern", "AI Engineer Intern"]
GENERATION_MODES = {"A": "Standard Internship Outreach", "B": "Technical Outreach", "C": "Role-Specific Outreach"}
