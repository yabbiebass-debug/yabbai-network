-- =====================================================================
-- YABBAI.NETWORK — LEGAL PATCH v2026-06-16
-- Addresses the external review's valid gaps: Notifiable Data Breaches
-- clause, named overseas providers, explicit AI-hallucination language,
-- and an accessibility statement. Demonstrates the versioning system:
-- new versions go active, prior versions are retired, users re-consent.
-- Run AFTER 008_legal.sql + the original seed. Find/replace the same
-- placeholders ([LEGAL ENTITY], [ABN], [CONTACT EMAIL], [STATE]) first.
-- =====================================================================

-- 1) Retire the prior active versions of the docs we're replacing.
update legal_documents set active = false
 where slug in ('privacy','terms') and version = '2026-06-15';

-- 2) Insert the upgraded versions.
insert into legal_documents (slug, version, title, body, effective, active) values

('privacy','2026-06-16','Privacy Policy',
$DOC$
PRIVACY POLICY — YABBAI.NETWORK

Last updated: 16 June 2026. [LEGAL ENTITY] (ABN [ABN]) is committed to protecting your
privacy in accordance with the Privacy Act 1988 (Cth) and the Australian Privacy Principles
(APPs), including the Notifiable Data Breaches (NDB) scheme.

1. WHAT WE COLLECT
- Account data: name, email, business details you provide.
- Order/service data: briefs, intake answers, project details, payment status (we do NOT
  store full card numbers — payment data is handled by our payment processor).
- Usage data: actions on the Platform, diagnostics/configurator answers you submit.
- Technical data: IP address, device/browser info, captured at events such as policy
  acceptance for security and audit.
- Communications: messages you send us.

2. HOW WE COLLECT IT
Directly from you (signup, forms, tools), automatically (usage/technical), and from third
parties only where you have authorised it.

3. WHY WE USE IT
To provide and improve the Platform and services; process orders and payments; generate
AI-assisted plans/scopes you request; communicate with you; maintain security; and meet
legal obligations. We process AI requests through model providers — only the content needed
to fulfil your request is sent.

4. DISCLOSURE
We share data with service providers who help us operate (hosting, payments, AI model
providers, email) under confidentiality obligations; where required by law; and in a
business sale. We do NOT sell your personal information.

5. OVERSEAS DISCLOSURE & PROVIDERS
Some providers store or process data outside Australia. Our key processors include:
- Anthropic (United States) — AI model processing;
- OpenRouter (United States / EU) — AI model routing, where enabled;
- Supabase (United States / global) — database, authentication, hosting;
- Stripe (global) — payment processing.
We take reasonable steps to ensure overseas recipients handle your information consistently
with the APPs, relying on these providers' contractual safeguards (e.g. standard contractual
clauses or equivalent) where applicable.

6. SECURITY
We use reasonable technical and organisational measures (access controls, row-level
security, secrets kept server-side). No system is perfectly secure; you use the Platform at
your own risk to that extent.

7. DATA BREACH NOTIFICATION
We maintain procedures to detect, assess and respond to data breaches. If an eligible data
breach occurs that is likely to result in serious harm, we will notify affected individuals
and the Office of the Australian Information Commissioner (OAIC) as required by the
Notifiable Data Breaches scheme, and take reasonable steps to contain and remediate it.

8. RETENTION
We keep personal information only as long as needed for the purposes above or as required by
law, then delete or de-identify it.

9. YOUR RIGHTS
You may request access to or correction of your personal information, and may complain about
a privacy breach. Contact us; if unresolved you may contact the OAIC.

10. COOKIES
The Platform uses minimal cookies/local storage needed to operate (e.g. authentication). We
do not use them to sell your data.

11. CHANGES & CONTACT
We may update this Policy; the current version applies. Contact: [CONTACT EMAIL].
$DOC$,
 '2026-06-16', true),

('terms','2026-06-16','Terms of Service',
$DOC$
TERMS OF SERVICE — YABBAI.NETWORK

Last updated: 16 June 2026. Operated by [LEGAL ENTITY] (ABN [ABN]) ("we","us","our").
By creating an account or using yabbai.network and its connected surfaces (the "Platform"),
you agree to these Terms. If you do not agree, do not use the Platform.

1. WHO WE ARE & WHAT WE PROVIDE
We provide automation, web, AI-integration and related digital products and services,
including done-for-you builds, subscription retainers, downloadable templates and kits,
and free tools. Some features connect third-party services (e.g. Stripe, Supabase, AI
model providers). We are not those third parties and are not responsible for their conduct.

2. ELIGIBILITY & ACCOUNTS
You must be at least 18 and able to form a binding contract. You are responsible for your
account credentials and all activity under your account. Provide accurate information and
keep it current.

3. ORDERS, PRICING, GST & PAYMENT
Prices are shown in AUD. Unless expressly stated as "GST inclusive", prices are exclusive of
GST, and where GST applies it is added at checkout and shown on your tax invoice. Setup fees
are one-time; retainers are recurring until cancelled per the relevant agreement. Payment is
processed by third-party providers (e.g. Stripe); by paying you also accept their terms. We
may change prices prospectively (existing paid orders are not affected).

4. DIGITAL PRODUCTS & LICENCE
Unless a product states otherwise, purchasing a downloadable template, kit or tool grants
you a non-exclusive, non-transferable licence to use it for your own and your clients'
projects. You may not resell, redistribute, or relist our products as your own. Products
that bundle third-party open-source components remain subject to those components' licences.

5. SERVICES, SCOPE & DELIVERY
Service work is defined by the scope you accept (see the Client Agreement). We deliver the
systems described; we do not guarantee specific business outcomes such as revenue, lead
volume, or rankings. Timeframes are estimates.

6. ACCEPTABLE USE
Your use is subject to our Acceptable Use Policy. We may suspend or terminate accounts that
breach it, that create legal risk, or that misuse the Platform or its AI features.

7. AI-GENERATED CONTENT
The Platform uses AI to generate plans, drafts, scopes, scripts and other content. AI systems
can produce output that is inaccurate, incomplete, or fabricated (commonly called
"hallucinations"), and can reflect biases in training data. All AI output is provided "as is"
for your review and judgement, is not professional (legal, medical, financial or other)
advice, and must be verified by you before you rely on or act on it. You are responsible for
how you use AI output.

8. INTELLECTUAL PROPERTY
We own the Platform and our materials except your content and third-party components. You
own content you submit; you grant us a licence to use it to provide the services.

9. THIRD-PARTY SERVICES & LINKS
The Platform integrates and links to third-party services. We do not control them and are
not liable for them.

10. DISCLAIMERS
To the maximum extent permitted by law, the Platform and products are provided "as is"
without warranties of any kind. Nothing in these Terms excludes rights you have under the
Australian Consumer Law (ACL) that cannot be excluded. Where we are permitted to limit
liability for a failure to meet a consumer guarantee, our liability is limited to re-supply
or the cost of re-supply.

11. LIMITATION OF LIABILITY
To the maximum extent permitted by law, we are not liable for indirect, incidental,
consequential or special loss, or loss of profits, data or goodwill. Our total aggregate
liability arising from the Platform or a product is limited to the amount you paid us for
the relevant product or service in the 12 months before the claim.

12. INDEMNITY
You indemnify us against claims arising from your breach of these Terms, your content, or
your unlawful use of the Platform, to the extent permitted by law.

13. SUSPENSION & TERMINATION
You may stop using the Platform at any time. We may suspend or terminate access for breach
or legal/operational reasons. Provisions that by their nature should survive (IP, liability,
indemnity) survive termination.

14. ACCESSIBILITY
We aim to make the Platform usable for as many people as possible and work toward alignment
with recognised accessibility guidance (WCAG 2.1 AA) over time. If you encounter an access
barrier, contact [CONTACT EMAIL] and we will try to help and to improve.

15. CHANGES
We may update these Terms; the version shown at the time you accept governs your use until a
new version is accepted. Material changes will be notified and may require re-acceptance.

16. GOVERNING LAW
These Terms are governed by the laws of [STATE], Australia, and you submit to the
non-exclusive jurisdiction of its courts.

17. CONTACT
[CONTACT EMAIL]
$DOC$,
 '2026-06-16', true)

on conflict (slug, version) do nothing;

-- Result: any user who accepted the 2026-06-15 versions will now appear in
-- my_pending_policies for terms + privacy, and the consent gate re-prompts them
-- on next load. Their original 2026-06-15 acceptance record remains intact as proof.
