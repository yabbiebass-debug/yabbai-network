-- =====================================================================
-- YABBAI.NETWORK — LEGAL DOCUMENT SEED
-- Drafted templates fitted to this platform. REVIEW WITH AN AUSTRALIAN
-- LAWYER before relying on them — especially for NDIS/medical/finance
-- clients and anything touching consumer guarantees.
-- Find/replace before running:
--   [LEGAL ENTITY]  e.g. BASHAM Automations Pty Ltd
--   [ABN]           your ABN
--   [CONTACT EMAIL] e.g. legal@yabbai.network
--   [STATE]         e.g. Victoria
-- Version stamp is today's date; bump it whenever you change any text.
-- =====================================================================

insert into legal_documents (slug, version, title, body, effective, active) values

('terms','2026-06-15','Terms of Service',
$DOC$
TERMS OF SERVICE — YABBAI.NETWORK

Last updated: 15 June 2026. Operated by [LEGAL ENTITY] (ABN [ABN]) ("we","us","our").
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

3. ORDERS, PRICING & PAYMENT
Prices are shown in AUD and may exclude GST unless stated; GST is added where applicable.
Setup fees are one-time; retainers are recurring until cancelled per the relevant agreement.
Payment is processed by third-party providers (e.g. Stripe); by paying you also accept their
terms. We may change prices prospectively (existing paid orders are not affected).

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
The Platform uses AI to generate plans, drafts, scopes, scripts and other content. AI output
can be wrong or incomplete and is provided "as is" for your review and judgement. It is not
professional advice. You are responsible for checking and how you use it.

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

14. CHANGES
We may update these Terms; the version shown at the time you accept governs your use until a
new version is accepted. Material changes will be notified and may require re-acceptance.

15. GOVERNING LAW
These Terms are governed by the laws of [STATE], Australia, and you submit to the
non-exclusive jurisdiction of its courts.

16. CONTACT
[CONTACT EMAIL]
$DOC$,
 current_date, true),

('privacy','2026-06-15','Privacy Policy',
$DOC$
PRIVACY POLICY — YABBAI.NETWORK

Last updated: 15 June 2026. [LEGAL ENTITY] (ABN [ABN]) is committed to protecting your
privacy in accordance with the Privacy Act 1988 (Cth) and the Australian Privacy Principles
(APPs).

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
legal obligations. We process AI requests through model providers (e.g. Anthropic, and where
enabled, routed providers) — only the content needed to fulfil your request is sent.

4. DISCLOSURE
We share data with service providers who help us operate (hosting, payments, AI model
providers, email) under confidentiality obligations; where required by law; and in a
business sale. We do NOT sell your personal information.

5. OVERSEAS
Some providers may store or process data outside Australia. We take reasonable steps to
ensure appropriate handling consistent with the APPs.

6. SECURITY
We use reasonable technical and organisational measures (access controls, row-level
security, secrets kept server-side). No system is perfectly secure; you use the Platform at
your own risk to that extent.

7. RETENTION
We keep personal information only as long as needed for the purposes above or as required by
law, then delete or de-identify it.

8. YOUR RIGHTS
You may request access to or correction of your personal information, and may complain about
a privacy breach. Contact us; if unresolved you may contact the Office of the Australian
Information Commissioner (OAIC).

9. COOKIES
The Platform uses minimal cookies/local storage needed to operate (e.g. authentication). We
do not use them to sell your data.

10. CHANGES & CONTACT
We may update this Policy; the current version applies. Contact: [CONTACT EMAIL].
$DOC$,
 current_date, true),

('client-agreement','2026-06-15','Client Services Agreement',
$DOC$
CLIENT SERVICES AGREEMENT — YABBAI.NETWORK

This Agreement applies when you purchase done-for-you services or a retainer from
[LEGAL ENTITY] (ABN [ABN]).

1. SCOPE
We will design, build and deliver the systems set out in the scope you accept at checkout
("Scope"). Work outside the Scope is a separate order.

2. FEES
Setup fees are one-time and payable upfront; they are non-refundable once work commences,
subject to your non-excludable ACL rights. Retainers are billed monthly in advance from the
go-live date and continue until cancelled with 30 days' written notice.

3. YOUR RESPONSIBILITIES
You will provide timely access, information and approvals, and pay third-party subscription
costs (e.g. automation platforms, hosting, AI usage) unless we agree otherwise. Delays on
your side extend our timeframes.

4. NO OUTCOME GUARANTEE
We guarantee the systems are built and function as described in the Scope. We do NOT
guarantee revenue, lead volume, conversion rates, or other business outcomes, which depend
on factors outside our control.

5. REGULATED INDUSTRIES
If you operate in a regulated sector (e.g. NDIS, health, legal, financial services), you are
responsible for ensuring the systems and any content comply with the rules applying to you.
We build to your instructions and do not provide regulatory, legal, medical or financial
advice.

6. REVISIONS & ACCEPTANCE
The Scope states included revision rounds. On delivery you have a review window to confirm
the systems meet the Scope; after that, work is deemed accepted.

7. IP & HANDOVER
On full payment, you receive a licence to use the delivered systems for your business. Our
underlying tools, templates and methods remain ours. Third-party components remain under
their licences.

8. CONFIDENTIALITY
Each party keeps the other's non-public information confidential and uses it only to perform
this Agreement.

9. LIABILITY
Our liability under this Agreement is subject to the limitations in our Terms of Service and
the ACL.

10. TERMINATION
Either party may terminate for material unremedied breach. You remain liable for work done
and the current retainer period.
$DOC$,
 current_date, true),

('aup','2026-06-15','Acceptable Use Policy',
$DOC$
ACCEPTABLE USE POLICY — YABBAI.NETWORK

You must not use the Platform, its products, or its AI features to:
- break any law, or infringe others' rights (including IP);
- create, distribute or facilitate malware, exploits, credential theft, or unauthorised
  access to systems;
- scrape, harvest or process personal data without a lawful basis and consent;
- send spam or messages that breach the Spam Act 2003 (Cth) or the Do Not Call Register;
- resell, relabel or redistribute our products as your own, or violate third-party
  open-source licences;
- generate or distribute content that is unlawful, harassing, hateful, deceptive, or that
  makes false guarantees (e.g. guaranteed income, or medical/legal/financial advice
  presented as professional advice);
- misrepresent AI-generated output as human professional advice;
- attempt to disrupt, overload, reverse-engineer or circumvent the Platform's security.

We may remove content, suspend features, or terminate accounts that breach this Policy, and
we may report unlawful activity to authorities.
$DOC$,
 current_date, true),

('refund','2026-06-15','Refund & Returns Policy',
$DOC$
REFUND & RETURNS POLICY — YABBAI.NETWORK

This Policy operates alongside your rights under the Australian Consumer Law (ACL), which
cannot be excluded.

1. DIGITAL PRODUCTS (templates, kits, tools)
Because these are delivered instantly and can be copied, all sales are generally final once
downloaded. If a product is faulty, not as described, or fails to do what we said it would,
you are entitled to a remedy under the ACL — contact us and we will repair, replace, or
refund as appropriate.

2. SERVICES & RETAINERS
Setup fees are non-refundable once work has commenced, except where we fail to meet a
consumer guarantee. Retainers can be cancelled with 30 days' notice; you are not charged for
periods after the notice expires, but completed periods are not refunded.

3. HOW TO REQUEST
Email [CONTACT EMAIL] with your order details and the issue. We aim to respond within 5
business days.

4. CHARGEBACKS
Please contact us first — most issues are resolved quickly. Fraudulent chargebacks may be
disputed.
$DOC$,
 current_date, true)

on conflict (slug, version) do nothing;
