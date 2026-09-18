# Phase 3H — Administrator Login Redirect & Profile Photo Fix

## Findings
- A newly created/reset administrator has `password_must_change=1`.
- Login correctly redirects that account to `/admin/password`.
- The workspace-boundary middleware previously redirected `/admin/password` back to `/admin/home` when no workspace had been selected.
- `/admin/home` then enforced `password_must_change` and redirected back to `/admin/password`, producing `ERR_TOO_MANY_REDIRECTS`.
- The administrator profile uploader used the browser's native file input directly, which made the photo control visually misaligned and inconsistent with the rest of the profile editor.

## Corrections
- `/admin/password` is now explicitly outside workspace selection, alongside login/logout/workspace selection routes.
- The forced first-login password-change flow can therefore complete before a workspace is chosen.
- Administrator photo selection is now a controlled profile-photo component with a fixed preview frame, hidden native file input, labelled upload control and immediate client-side preview.
- Existing stored administrator photos continue to use the existing secure `static/uploads/admins/...` path and validation rules.

## Verification
- `app.py` Python AST parse: PASS
- Existing Super Admin scope count in packaged database: 0 — PASS
- Existing administrator photo files referenced by database: present — PASS
- Reset endpoint present and protected: PASS
- First-login password-change route exempted from workspace redirect: PASS
- Profile photo input/preview contract: PASS
- Existing multiple-role and multi-value boundary form controls preserved: PASS
