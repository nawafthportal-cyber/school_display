# Admin console remediation — 2026-11-06

The release preserves school content, paid subscriptions, historical payment
amounts, invoice snapshots, receipt identifiers, and authenticator secrets.

| Finding | Resolution |
| --- | --- |
| F01 | Authenticated Cloudinary receipts served only through an authorized Django endpoint. Legacy assets change delivery type in place; asset identity, byte count and SHA-256 content are verified. No public signatures in forms. |
| F02 | Editing and deleting owners share ordered row locks and preserve at least one active owner. |
| F03 | Approval rejects an existing cancelled/pending subscription conflict without changing the request or historical subscription. |
| F04 | Request, subscription, payment and invoice commit together; a failure rolls back approval. |
| F05 | Every transition locks and rereads the request; a unique nullable request-to-payment relation prevents repeated payment records. |
| F06 | Production requires 2FA. Enroll the existing owner and save recovery codes before enforcing the existing policy. Deployment checks reject a disabled policy. |
| F07 | User and employee password changes use Django's configured password validators. |
| F08 | Field labels and custom search controls have explicit accessible names. |
| F09 | Closed mobile navigation is inert; open navigation traps keyboard focus and restores it on close. |
| F10 | Active plan usage excludes future starts. |
| F11 | Plan filters display duration and price alongside names. |
| F12 | Provider reconciliation consumes `last_event`, exposes outstanding delivery review, and runs in bounded hourly batches. It never resends messages or edits recipients. Historical bounces remain recorded accurately. |
| F13 | Report labels explicitly describe current active plan values rather than collected or monthly revenue. |
| F14 | Each image embeds the exact release SHA through a build argument, displayed in the console footer. |
| F15 | Existing payment amounts/methods/plans and invoice snapshots remain immutable when editing subscription notes or dates. Paid subscriptions cannot move between schools. |
| F16 | Separate reversible administrative display suspension preserves billing state; both TV HTML and API reject suspended access, running clients reload the suspension page, and monitoring excludes intentionally suspended schools. |

## Release procedure

1. Capture counts and hashes for school/business records; retain them on the
   host, outside recreated containers. Confirm active owner enrollment without
   displaying secrets. Back up PostgreSQL using the existing deploy workflow.
2. Set only `TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=True` in the protected production
   environment after enrollment. Do not regenerate the encryption key.
3. Require successful SQLite/browser/security CI and PostgreSQL concurrency CI
   for the merged main revision. Deploy that exact revision using deploy.yml.
4. Apply core.0039, subscriptions.0027 and mailcenter.0002. These add default-false
   suspension, a nullable unique request link and an optional reconciliation
   timestamp; they do not rewrite school content or financial history.
5. Run `secure_subscription_receipts` to inventory; then run with `--apply`.
   Verify the original public URL denies anonymous access, the private original
   is readable to an authorized user, and its asset ID, byte size and SHA-256 content match.
   Do not change assets back to public delivery during rollback.
6. Run `reconcile_mail_delivery --apply --limit 100`. Inspect aggregate outcomes;
   a provider-expired or unknown record remains unknown, never falsely delivered.
7. Verify exact source and image revisions, zero pending migrations, all project
   containers healthy, unchanged unrelated containers, school/business record
   hashes, owner policy, protected receipts, all admin GET pages, mobile keyboard
   behavior, public health and display/theme regressions. Never submit financial
   approvals or modify real school content for production testing.

Provider references: [Cloudinary authenticated media](https://cloudinary.com/documentation/control_access_to_media),
[Cloudinary rename and invalidation](https://cloudinary.com/documentation/image_upload_api_reference),
[Resend delivery event retrieval](https://resend.com/docs/api-reference/emails/retrieve-email).

Browser CI uses an explicit disposable SQLite file, so live-server threads have
separate connections; assertions remain unchanged. Preview windows never send
TV disconnect beacons. See [Django live-server thread guidance](https://docs.djangoproject.com/en/5.2/topics/testing/tools/#liveservertestcase).
