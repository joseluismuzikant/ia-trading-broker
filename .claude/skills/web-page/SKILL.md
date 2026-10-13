---
name: web-page
description: Add a server-rendered page or form (FastAPI router plus Jinja2 template) with owner scoping and CSRF. Use when a task adds or changes a screen, a form, or an endpoint under app/api.
---

# Add a page or a form

Look at `app/api/history.py` and `templates/history.html` for the pattern.

1. **Router** in `app/api/<area>.py`, registered in `app/main.py`.
2. **Auth.** `auth: AuthContext = Depends(require_user)`; pass
   `auth.user.id` to services. Never read a user id from input.
3. **Data** comes from services only. Pages that can work from saved data
   must not call a broker on GET.
4. **Forms.** Include the CSRF token from `get_csrf_token` and validate it
   with `validate_form_csrf`. Respond to a POST with a 303 redirect.
5. **Template** extends `base.html`; format money and dates with the helpers
   in `app/formatting.py` (Argentina time, local separators).
6. **Danger.** Buttons that submit real orders say so in their label and show
   the account type (real or paper).
7. **Tests.** Page renders for the owner; another user gets "not found"; a
   POST without CSRF is refused.
8. **README.** Add the route to the Endpoints table.
