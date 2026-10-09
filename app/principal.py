"""Identity of the authenticated operator on the shared-password preview deployment.

The preview credential carries no per-person identity, so audit records use this
explicit generic actor rather than a person's name.
"""

PREVIEW_PRINCIPAL = "preview-operator"
