# Content Security Policy rollout

The application enforces CSP by default. Inline JavaScript uses a fresh response
nonce and template event handlers use delegated listeners in `csp-actions.js`.
The production environment must also set `CONTENT_SECURITY_POLICY_ENABLED=True`
and `CONTENT_SECURITY_POLICY_REPORT_ONLY=False`; an old environment override
otherwise keeps reporting without enforcement.

## Current policy

- JavaScript allows same-origin files, response nonces, Cloudflare Insights and jsDelivr.
- Objects are disabled and frame ancestors are same-origin only.
- Forms and frames also allow the Moyasar payment gateway for 3-D Secure.
- Camera, microphone, geolocation, payment and motion sensors are disabled through `Permissions-Policy`.
- Google Fonts, Font Awesome CSS, Cloudinary/HTTPS images and the privacy-enhanced YouTube player remain allowed where the current UI uses them.

Reports are sent to `/csp-report/`. The endpoint limits report volume, strips query strings from logged URLs and never stores the original request body.

## Verification and rollback

1. Run `core.test_csp` and the dashboard/TV browser regression tests before rollout.
2. Check browser policy violations on public pages, login, dashboards and TV pairing.
3. Confirm public HTML responses contain `Content-Security-Policy`, not just the report-only header.
4. Review `csp_violation` logs after deployment. If a required interaction is blocked,
   set `CONTENT_SECURITY_POLICY_REPORT_ONLY=True` and recreate the affected service
   while correcting the policy or resource. Do not disable all CSP reporting.

Do not add `'unsafe-eval'` to solve a third-party library problem. Prefer a local CSP-compatible build or a small native JavaScript replacement.
