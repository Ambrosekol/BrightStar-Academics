"""Which form-submitting routes write_paths_pg_posts.py drives, and which it cannot.

This file has no dependencies and needs no database, so a fast contract test can import it
and compare it with the application's own list of routes:

    coverage.unaccounted(app.url_map)     # must be empty

Every route that accepts a POST (or PUT, PATCH, DELETE) must be in EXERCISED or in EXEMPT.
That is the guard: a new route cannot ship without a real submission being written for it
in write_paths_pg_posts.py (and its rule added to EXERCISED), or a one-line reason here.

write_paths_pg_posts.py checks the other direction on every run: what it actually submitted
must equal EXERCISED, so this list cannot claim more than the script really does.
"""

# Rules the script cannot submit, each with the reason. Empty is the goal. A rule here is
# reported in the coverage summary on every run.
EXEMPT = {
}

# The rules write_paths_pg_posts.py submits a real, valid form to (and then junk).
EXERCISED = frozenset({
    "/admin/administration/admins/<int:aid>/credentials/reset",
    "/admin/administration/admins/<int:aid>/edit",
    "/admin/administration/admins/<int:aid>/toggle",
    "/admin/administration/admins/new",
    "/admin/administration/controls/<int:cid>/resolve",
    "/admin/administration/messages/send",
    "/admin/administration/notifications/<int:nid>/read",
    "/admin/administration/resources/<resource_type>/<path:resource_id>/lock",
    "/admin/administration/resources/<resource_type>/<path:resource_id>/unlock",
    "/admin/administration/roles/<int:rid>/edit",
    "/admin/administration/roles/new",
    "/admin/banks/<bid>/edit",
    "/admin/banks/<bid>/questions/<int:qid>/delete",
    "/admin/banks/<bid>/questions/<int:qid>/edit",
    "/admin/banks/<bid>/questions/new",
    "/admin/banks/<bid>/questions/reorder",
    "/admin/banks/import",
    "/admin/banks/new",
    "/admin/candidates/<int:cid>/credentials/reset",
    "/admin/candidates/<int:cid>/delete",
    "/admin/candidates/new",
    "/admin/entrance-config/<int:config_id>/activate",
    "/admin/entrance-config/save",
    "/admin/entrance-config/standard",
    "/admin/examinations/<bid>/toggle",
    "/admin/finance/assessments/new",
    "/admin/finance/fee-items/<int:item_id>/edit",
    "/admin/finance/fee-items/<int:item_id>/toggle",
    "/admin/finance/fee-items/new",
    "/admin/finance/payments/<int:payment_id>/allocate",
    "/admin/finance/payments/<int:payment_id>/void",
    "/admin/finance/payments/new",
    "/admin/finance/receipt-settings",
    "/admin/finance/receipts/<int:payment_id>/email",
    "/admin/finance/receipts/<int:payment_id>/whatsapp",
    "/admin/library/books/<int:book_id>/edit",
    "/admin/library/books/<int:book_id>/toggle",
    "/admin/library/books/new",
    "/admin/library/issue",
    "/admin/library/loans/<int:loan_id>/return",
    "/admin/login",
    "/admin/practice-tests/save",
    "/admin/password",
    "/admin/results/<int:aid>/grant-retake",
    "/admin/results/<int:aid>/regrade",
    "/admin/school/results/release-term",
    "/admin/school/onboarding/dismiss",
    "/admin/school/onboarding/show",
    "/admin/school/assessments/<int:assessment_id>/edit",
    "/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/delete",
    "/admin/school/assessments/<int:assessment_id>/questions/<int:question_id>/edit",
    "/admin/school/assessments/<int:assessment_id>/questions/new",
    "/admin/school/assessments/<int:assessment_id>/questions/reorder",
    "/admin/school/assessments/<int:assessment_id>/toggle",
    "/admin/school/assignments/<int:assignment_id>/delete",
    "/admin/school/assignments/<int:assignment_id>/edit",
    "/admin/school/assignments/<int:assignment_id>/questions",
    "/admin/school/assignments/<int:assignment_id>/students/<int:student_id>",
    "/admin/school/assignments/new",
    "/admin/school/branding/save",
    "/admin/school/classes/<int:class_id>/edit",
    "/admin/school/classes/<int:class_id>/toggle",
    "/admin/school/delivery/email/clear",
    "/admin/school/delivery/email/save",
    "/admin/school/report-cards/comments",
    "/admin/school/report-cards/my-signature",
    "/admin/school/report-cards/settings",
    "/admin/school/delivery/email/test",
    "/admin/school/delivery/whatsapp/check",
    "/admin/school/delivery/whatsapp/clear",
    "/admin/school/delivery/whatsapp/save",
    "/admin/school/examinations/<int:assessment_id>/delete",
    "/admin/school/examinations/new",
    "/admin/school/parent-feedback/<int:feedback_id>/reply",
    "/admin/school/parent-feedback/<int:feedback_id>/status",
    "/admin/school/parents/<int:pid>/credentials/reset",
    "/admin/school/parents/<int:pid>/edit",
    "/admin/school/parents/new",
    "/admin/school/practice-tests/<int:assessment_id>/delete",
    "/admin/school/practice-tests/new",
    "/admin/school/projects/<int:project_id>/delete",
    "/admin/school/projects/<int:project_id>/edit",
    "/admin/school/projects/<int:project_id>/students/<int:student_id>",
    "/admin/school/projects/new",
    "/admin/school/promotion",
    "/admin/school/promotion/<int:run_id>",
    "/admin/school/promotion/progressions",
    "/admin/school/results/<int:result_id>/edit",
    "/admin/school/results/<int:result_id>/workflow",
    "/admin/school/results/manual/new",
    "/admin/school/results/release-schedule",
    "/admin/school/sessions",
    "/admin/school/students/<int:sid>/account",
    "/admin/school/students/<int:sid>/account/toggle",
    "/admin/school/students/<int:sid>/edit",
    "/admin/school/students/<int:sid>/history",
    "/admin/school/students/<int:sid>/history/<int:history_id>/edit",
    "/admin/school/students/<int:sid>/toggle",
    "/admin/school/students/new",
    "/admin/school/subjects/<int:subject_id>/delete",
    "/admin/school/subjects/<int:subject_id>/edit",
    "/admin/school/subjects/<int:subject_id>/final-lock/<int:class_id>",
    "/admin/school/subjects/<int:subject_id>/lock/<int:class_id>",
    "/admin/school/subjects/new",
    "/admin/school/subjects/quick-create",
    "/admin/school/tests/<int:assessment_id>/delete",
    "/admin/school/tests/new",
    "/answer",
    "/candidate/papers/<int:paper_id>/start",
    "/entrance-practice/<group>/<subject>",
    "/forgot-password",
    "/login",
    "/logout",
    "/parent/feedback",
    "/parent/feedback/<int:feedback_id>/reply",
    "/parent/password",
    "/platform/login",
    "/platform/logout",
    "/platform/numbering/preview",
    "/platform/password",
    "/platform/schools/<slug>/admins",
    "/platform/schools/<slug>/branding",
    "/platform/schools/<slug>/domains",
    "/platform/schools/<slug>/enter",
    "/platform/schools/<slug>/numbering",
    "/platform/schools/<slug>/status",
    "/platform/schools/new",
    "/platform/team/<int:admin_id>/docs-access",
    "/platform/team/<int:admin_id>/remove",
    "/platform/team/<int:admin_id>/reset-password",
    "/platform/team/<int:admin_id>/restore",
    "/platform/team/new",
    "/practice",
    "/practice/<int:assessment_id>",
    "/reset-password/<token>",
    "/student/assessments/<int:assessment_id>/answer",
    "/student/assessments/<int:assessment_id>/start",
    "/student/assignments/<int:assignment_id>",
    "/student/assignments/<int:assignment_id>/take",
    "/student/password",
    "/student/practice/<int:assessment_id>",
    "/submit",
})

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def write_rules(url_map):
    """Every URL rule of the application that accepts a write, as {rule: endpoint}."""
    return {rule.rule: rule.endpoint for rule in url_map.iter_rules() if rule.methods & WRITE_METHODS}


def unaccounted(url_map):
    """Rules that accept a write but are neither exercised nor exempt (should be empty)."""
    return sorted(rule for rule in write_rules(url_map) if rule not in EXERCISED and rule not in EXEMPT)


def stale(url_map):
    """Entries here that name a rule the application no longer has, or that are listed twice."""
    live = write_rules(url_map)
    gone = sorted(r for r in (set(EXERCISED) | set(EXEMPT)) if r not in live)
    both = sorted(set(EXERCISED) & set(EXEMPT))
    return gone, both
