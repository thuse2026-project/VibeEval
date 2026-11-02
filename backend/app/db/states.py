"""Canonical states shared by exam, workspace and submission modules."""

from enum import StrEnum


class SessionStatus(StrEnum):
    ADMITTED = "admitted"
    PROVISIONING = "provisioning"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    PROVISION_FAILED = "provision_failed"
    SUBMITTING = "submitting"
    SUBMISSION_FAILED = "submission_failed"
    SUBMITTED = "submitted"
