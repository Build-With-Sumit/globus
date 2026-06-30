# PRD — Narada, the Globus Outbound Agent

**Status:** Draft v1 (2026-07-01) — awaiting sign-off before execution
**Owner:** Sumit Aneja
**Lead engineer:** Claude (this session)
**Target ship:** Plugin spine + first wave (Gmail + Prospeo + Freshsales)
within one focused session; subsequent plugins land as credentials arrive

> *"Narada Muni, the celestial messenger of the Mahabharata, travels
> between courts carrying news, planting ideas, and connecting people
> who'd otherwise never meet. Globus's outbound agent does the same
> for marketers."*

---

## 1. Product overview

**Narada** turns Globus into the single tool a marketer needs to run
end-to-end cold outbound campaigns. ICP → leads → email find + verify
→ personalised copy → send → multi-step sequences → reply
classification → CRM sync → analytics — all driven by chat (LLM
tool dispatch) or a structured dashboard, with the member's choice
of any underlying SaaS tool (or none, using just Gmail + Globus).

> *"Marketers don't need anything else. They can just come to Globus
> AI and use the outbound agents for everything."* — Sumit, 2026-07-01

Narada is a **plugin platform**. Every external SaaS (Smartlead,
Apollo, Lemlist, NeverBounce, HubSpot, Heyreach, etc.) is a plugin
implementing one of five protocols. Narada doesn't know which
provider is behind a given call — the marketer picks at campaign
time, and Globus orchestrates.

### Where Narada lives in Globus

| Surface | Role |
|---|---|
| **Agents catalog card at `/members/globus/agents`** | Narada appears alongside Research / Sales-Desk / Infra-Watch with a "Run now" button. Schedule + capabilities + latest brief shown. |
| **Dedicated dashboard at `/members/narada`** | Full campaign UI: list, create, copy review queue, sequence editor, live stats. The "heavy" surface. |
| **Chat** (`/members/globus`) | Natural language → tool dispatch. "Narada, run a 50-lead VideoraIQ campaign to fintech CMOs" → Skill orchestrates the full pipeline. |
| **Cron** | Scheduled campaigns ("every Tuesday 8 AM, run the weekly newsletter outbound"). |

## 2. Target user

Three personas:

| Persona | Primary need | Surface they use |
|---|---|---|
| **Solo founder / operator** | Run cold outreach without becoming a tool jockey. Pays for 1-2 SaaS, wants Globus to handle the rest. | Chat — natural language ("run a 50-lead campaign for VideoraIQ targeting fintech CMOs") |
| **Marketing/SDR at a small B2B** | Replace agency. Already has tools (Apollo, Smartlead) — wants ONE place to orchestrate them all. | Dashboard — structured campaign list, copy review queue, sequence editor |
| **Agency operator** | Run outbound for 10+ clients. Per-client sub-accounts, per-client tools, isolated suppression. | Both — chat for speed, dashboard for client management |

V1 ships for the first two; V3 unlocks multi-tenant agency mode.

## 3. Vision

Today, an SDR runs cold outbound by stitching together: Apollo for
data, Smartlead for sending, ChatGPT for copy, Sheets for tracking,
HubSpot for CRM. Five vendors, two dashboards, manual handoffs.

In Globus, the same SDR types one sentence and the agent does the
entire pipeline. The five vendors are still under the hood (if they
chose to use them) but the SDR never sees their dashboards. Reply
came in? Globus auto-classifies it and pushes the right hot lead to
their CRM. Copy underperforming? Globus learns from what worked across
all the marketer's past campaigns and proposes new angles.

## 4. Key capabilities (the V1.0 functional surface)

| # | Capability | Detail |
|---|---|---|
| 1 | **Campaign brief** | One prompt → ICP, angle, target volume, success metric persisted as a campaign object |
| 2 | **Lead sourcing** | Pluggable: marketer picks Prospeo / Apollo / Hunter / Clay / etc. (or auto: cheapest provider that covers the ICP) |
| 3 | **Email find + verify** | Two-stage: find via lead-source provider; verify via verifier provider. Both pluggable. |
| 4 | **Per-prospect research** | Pull LinkedIn / recent posts / company news / intent signals. Pluggable enrichment providers. |
| 5 | **Personalised copy gen** | 3 variants per prospect, drawing on past-winning-angle memory across the marketer's campaigns |
| 6 | **Human review queue** | Marketer approves copy (one-click ✓ / edit / reject) before send. Skill-driven full-auto mode available for trusted campaigns. |
| 7 | **Sending** | Pluggable: Gmail/Workspace / Smartlead / Lemlist / Bison / Apollo / etc. Per-campaign sender choice. |
| 8 | **Sequences** | Multi-step with branching (if reply → stop; if open + no reply → send v2; etc.) |
| 9 | **Reply detection + classification** | Read inbox via existing Gmail integration. Auto-classify: interested / not / OOO / unsubscribe / referred / wrong-person. |
| 10 | **CRM sync** | Pluggable: Freshsales / HubSpot / Pipedrive / Salesforce / Close / etc. Pushes hot replies as Tasks/Deals/Notes. |
| 11 | **Suppression list** | Auto-managed: unsubscribes, hard bounces, prior do-not-contact. Per-member-scoped. |
| 12 | **CAN-SPAM / GDPR compliance** | Auto-inject unsubscribe link + physical-address footer. Honour `List-Unsubscribe` headers on replies. |
| 13 | **Analytics dashboard** | Per-campaign + per-mailbox + per-variant metrics. Open / reply / unsubscribe / spam-complaint rates. |
| 14 | **Winning-angle memory** | Globus learns: "for fintech CMOs, opening with X gets 3x reply rate." Pulled into future copy gen. |

## 5. Functional categories + top 10 tools per category

The integration list. Each category has a protocol; each tool is a
plugin implementing it. Build order: spine first, then the top 1-2 in
each category, then deepen as marketer demand emerges.

### 5.1 Lead source (find prospects matching ICP)

| # | Tool | Notes | Composio? |
|---|---|---|---|
| 1 | **Apollo.io** | Largest B2B database. Built-in sequences (overlap with sender). | Yes |
| 2 | **Prospeo** | The one Sumit picked. Lead search + email find + verify in one. Cheapest entry tier. | Custom |
| 3 | **Hunter** | Email find + verify focused. Domain search workflow. | Yes |
| 4 | **Clay** | The "ETL for prospecting" — combines 50+ data sources. | Yes |
| 5 | **ZoomInfo** | Enterprise-tier data. Pricey. | Custom |
| 6 | **Cognism** | EU-focused (better GDPR compliance for European lists). | Custom |
| 7 | **Lusha** | Strong on mobile numbers + LinkedIn enrichment. | Custom |
| 8 | **RocketReach** | Personal email focus (contact at any company). | Custom |
| 9 | **UpLead** | Verified-only data, lower volume but higher quality. | Custom |
| 10 | **FindyMail** | Cheap LinkedIn → email lookup. Used heavily by agencies. | Custom |

### 5.2 Email verifier (confirm deliverability before send)

| # | Tool | Notes |
|---|---|---|
| 1 | **NeverBounce** | Industry standard. Per-lookup pricing. |
| 2 | **ZeroBounce** | Bulk validation. Catch-all detection. |
| 3 | **MillionVerifier** | Cheapest at scale. |
| 4 | **Hunter Verify** | If marketer's already on Hunter for finds. |
| 5 | **Prospeo verify** | Built into Prospeo subscription. |
| 6 | **BriteVerify** | Email + phone validation. |
| 7 | **EmailListVerify** | Free tier 1000/day. |
| 8 | **Bouncer** | EU-based, GDPR-friendly. |
| 9 | **DeBounce** | Free tier; tier-up pricing. |
| 10 | **Snov.io Verify** | Bundled with Snov sender. |

### 5.3 Sender (push the email)

| # | Tool | Notes |
|---|---|---|
| 1 | **Gmail / Google Workspace** | Free if member already has Workspace. Native via Composio Gmail plugin + `gmail.send` scope. Per-Sumit-memory: default. |
| 2 | **Microsoft Outlook / 365** | Sister to Gmail. Via Composio Outlook plugin. |
| 3 | **Smartlead** | The cold-email SaaS gold standard. API on $174 tier. Throwaway-domain rotation. |
| 4 | **Lemlist** | API on every tier (cheapest at $69). lemwarm built-in. |
| 5 | **Instantly** | Strong warmup. API on Scale tier ($194). |
| 6 | **EmailBison** | What the @harsh_biz tweet author used. "Sickest API." Custom plugin. |
| 7 | **Apollo Sequences** | If using Apollo for leads, send via same vendor. |
| 8 | **Reply.io** | Multi-channel (email + LinkedIn + call). |
| 9 | **Mailshake** | Older but stable. Strong personalisation. |
| 10 | **Custom SMTP** | Last-resort fallback for any SMTP provider (AWS SES, Mailgun, Postmark, Brevo). |

### 5.4 CRM (sync hot replies + deal data)

| # | Tool | Notes |
|---|---|---|
| 1 | **Freshsales** | Sumit's CRM. Already wired in prod (custom). |
| 2 | **HubSpot** | Most popular for SMB. Via Composio. |
| 3 | **Salesforce** | Enterprise standard. Via Composio. |
| 4 | **Pipedrive** | SDR-friendly pipeline visuals. Via Composio. |
| 5 | **Close** | Built-for-outbound CRM. |
| 6 | **Attio** | Modern, Notion-like. |
| 7 | **Copper** | Native Gmail integration. |
| 8 | **Zoho CRM** | Cheaper enterprise option. |
| 9 | **Monday Sales CRM** | If marketer's already on Monday. |
| 10 | **Folk** | Lightweight, contact-first. |

### 5.5 LinkedIn outbound (DMs, connection requests, profile views)

| # | Tool | Notes |
|---|---|---|
| 1 | **Heyreach** | Most popular agency tool, multi-account safe. |
| 2 | **Dripify** | Strong sequence builder. |
| 3 | **Expandi** | Cloud-based (no browser extension). |
| 4 | **Lemlist (LinkedIn)** | If on Lemlist already, native integration. |
| 5 | **Phantombuster** | Most flexible, scrapes anything. |
| 6 | **Waalaxy** | Chrome extension, freemium. |
| 7 | **Linked Helper** | Desktop app, advanced. |
| 8 | **Skylead** | Smart sequences with email integration. |
| 9 | **La Growth Machine** | Multi-channel (LI + email + Twitter). |
| 10 | **We-Connect** | Enterprise-friendly, multi-seat. |

### 5.6 Enrichment / per-prospect research (signals for personalisation)

| # | Tool | Notes |
|---|---|---|
| 1 | **Apollo enrichment** | Auto if using Apollo for leads. |
| 2 | **Clearbit (now HubSpot Breeze)** | Industry standard for company+person enrichment. |
| 3 | **Crunchbase** | Funding/news data. |
| 4 | **BuiltWith** | Tech stack signals ("uses Stripe + Segment"). |
| 5 | **SimilarWeb** | Traffic + competitive intel. |
| 6 | **Owler** | Company news, exec changes. |
| 7 | **Phantombuster (LinkedIn profile)** | Recent posts + engagement signals. |
| 8 | **LinkedIn Sales Navigator** | Via Phantom or browser session. |
| 9 | **Lix** | LinkedIn search + enrichment, cheap. |
| 10 | **Prospeo enrichment** | If on Prospeo already. |

### 5.7 Email warmup (build sender reputation)

| # | Tool | Notes |
|---|---|---|
| 1 | **Smartlead warmup** | Bundled with Smartlead subscription. |
| 2 | **Lemwarm (Lemlist)** | Bundled with Lemlist. |
| 3 | **Warmup Inbox** | Standalone, $19/inbox. |
| 4 | **Mailwarm** | Standalone. |
| 5 | **Folderly** | Deliverability monitoring + warmup. |
| 6 | **Allegrow** | Inbox placement focus. |
| 7 | **Warmy.io** | AI-driven warmup. |
| 8 | **Mailreach** | Per-inbox pricing. |
| 9 | **Trulinx** | Newer, AI-based. |
| 10 | **Warmbox** | Cheaper alternative. |

### 5.8 Spam testing / deliverability monitoring

| # | Tool | Notes |
|---|---|---|
| 1 | **Mail-tester.com** | Free single-test for spam score. |
| 2 | **GlockApps** | Inbox placement across providers. |
| 3 | **MXToolbox** | DNS + blacklist diagnostics. |
| 4 | **Smartlead SmartDelivery** | Bundled. |
| 5 | **Folderly** | Continuous monitoring. |
| 6 | **Google Postmaster Tools** | Free, Google-domain-specific reputation data. |
| 7 | **Microsoft SNDS** | Free, Outlook-specific. |
| 8 | **250ok** (now Validity) | Enterprise. |
| 9 | **Litmus** | Email rendering + spam testing. |
| 10 | **Inbox Insight** | Newer. |

### 5.9 Calendar / scheduling (for replies wanting a meeting)

| # | Tool | Notes |
|---|---|---|
| 1 | **Google Calendar** | Via Composio. Default for Workspace users. |
| 2 | **Microsoft Outlook Calendar** | Via Composio. |
| 3 | **Calendly** | Native API. Public booking links. |
| 4 | **Cal.com** | Open source, self-host friendly. |
| 5 | **SavvyCal** | Founder-friendly, lightweight. |
| 6 | **Chili Piper** | Round-robin SDR routing. |
| 7 | **HubSpot Meetings** | Bundled with HubSpot. |
| 8 | **YouCanBookMe** | Cheaper Calendly alternative. |
| 9 | **Acuity Scheduling** | Service-business focused. |
| 10 | **Tidycal** | One-time payment, cheap. |

### 5.10 Analytics / event tracking

| # | Tool | Notes |
|---|---|---|
| 1 | **PostHog** | Open source, self-host friendly. |
| 2 | **Mixpanel** | Standard for event analytics. |
| 3 | **Amplitude** | Strong cohort analysis. |
| 4 | **Segment** | Event router (sends to all the others). |
| 5 | **Heap** | Auto-capture. |
| 6 | **June.so** | B2B-specific dashboards. |
| 7 | **Pendo** | Product analytics + in-app guides. |
| 8 | **Smartlead built-in** | Per-campaign metrics if on Smartlead. |
| 9 | **Lemlist built-in** | Same. |
| 10 | **Globus native** | Our own `globus_narada_*` tables. Always-on. |

### 5.11 WhatsApp outbound (high-conversion in IN / SEA markets)

| # | Tool | Notes |
|---|---|---|
| 1 | **Meta WhatsApp Cloud API** | Official, free up to N msgs/mo. Template-based. |
| 2 | **Twilio WhatsApp Business** | Most mature wrapper. |
| 3 | **Wati** | Popular in India, easy onboarding. |
| 4 | **AiSensy** | India-focused, cheap. |
| 5 | **Interakt** | India-focused, full-feature. |
| 6 | **MessageBird (Bird)** | EU-friendly. |
| 7 | **360Dialog** | Official BSP, enterprise. |
| 8 | **Gallabox** | India, cheap. |
| 9 | **Vonage** | Enterprise multi-channel. |
| 10 | **Globus' own WA bridge** | Chrome extension (already shipped) — for OUTBOUND from your own WA Web. |

### 5.12 Phone / voice outreach (cold call + voicemail drops)

| # | Tool | Notes |
|---|---|---|
| 1 | **AirCall** | Industry standard cloud phone. |
| 2 | **Dialpad** | AI transcription. |
| 3 | **Orum** | AI-powered parallel dialer. |
| 4 | **Nooks** | Parallel dialer, modern. |
| 5 | **JustCall** | Affordable, multi-channel. |
| 6 | **Kixie** | Built for outbound SDR. |
| 7 | **Aloware** | Cloud contact centre. |
| 8 | **PhoneBurner** | Power dialer focus. |
| 9 | **Smartlead Voice** | If on Smartlead. |
| 10 | **Twilio Programmable Voice** | DIY substrate. |

---

## 6. Architecture

### 6.1 Plugin protocols

Five protocols. Each plugin implements exactly one (or two for combined tools like Apollo):

```python
class LeadSource(Protocol):
    name: str                          # "prospeo", "apollo"
    requires_credentials: list[str]    # ["PROSPEO_API_KEY"]
    def search(self, member: str, icp: ICPFilters, count: int) -> list[Lead]
    def find_email(self, member: str, name: str, company_domain: str) -> str | None
    def cost_per_call(self, action: str) -> int  # credits

class Verifier(Protocol):
    name: str
    requires_credentials: list[str]
    def verify(self, member: str, email: str) -> VerifyResult
    def cost_per_call(self) -> int

class Sender(Protocol):
    name: str
    requires_credentials: list[str]
    def is_available(self, member: str) -> bool
    def daily_send_cap(self, member: str) -> int
    def send(self, member: str, from_addr: str, to: str, subject: str,
             body: str, headers: dict, reply_to: str | None) -> SendResult
    def detect_replies(self, member: str, since: datetime) -> list[Reply]
    def supports_warmup(self) -> bool

class CRM(Protocol):
    name: str
    requires_credentials: list[str]
    def upsert_contact(self, member: str, lead: Lead) -> str  # CRM contact id
    def create_deal(self, member: str, contact_id: str, deal: DealData) -> str
    def log_activity(self, member: str, contact_id: str, activity: Activity) -> None

class LinkedInChannel(Protocol):
    name: str
    requires_credentials: list[str]
    def send_connection_request(self, member: str, linkedin_url: str, note: str) -> str
    def send_dm(self, member: str, linkedin_url: str, body: str) -> str
    def visit_profile(self, member: str, linkedin_url: str) -> None
```

Plus an **EnrichmentProvider** protocol and an **AnalyticsSink** protocol for the secondary categories.

### 6.2 Plugin registry

```python
# server/narada_plugins/__init__.py
LEAD_SOURCES: dict[str, LeadSource] = {}
VERIFIERS: dict[str, Verifier] = {}
SENDERS: dict[str, Sender] = {}
CRMS: dict[str, CRM] = {}
LINKEDIN_CHANNELS: dict[str, LinkedInChannel] = {}

def register_lead_source(plugin: LeadSource) -> None:
    LEAD_SOURCES[plugin.name] = plugin
```

Each plugin module self-registers on import. A `_load_plugins()` boot
step imports every `server/narada_plugins/*.py` so all plugins are
registered before the first request.

### 6.3 Composio substrate

Plugins that match an app already in Composio's 1000+ catalog use
the `server/composio_helpers.py` scaffolding (committed 2026-07-01,
commit `434b9ef`). Custom plugins (Smartlead, Lemlist, EmailBison,
Prospeo, NeverBounce, Heyreach) implement the protocols directly.

| Provider | Implementation route |
|---|---|
| Gmail / Workspace / Calendar / GitHub / Slack / Notion / HubSpot / Pipedrive / Salesforce / Apollo (read) | **Composio** |
| Smartlead / Lemlist / Instantly / Bison / Hunter / NeverBounce / ZeroBounce / Prospeo / Heyreach / Dripify / FindyMail / Apollo (write/sequences) | **Custom plugin** |

### 6.4 Per-member credential model

| Auth pattern | Storage |
|---|---|
| Composio-managed OAuth (Gmail, HubSpot, etc.) | `globus_composio_connections` (committed in `434b9ef`) |
| API key (Smartlead, Prospeo, NeverBounce, etc.) | `globus_narada_credentials` (NEW: per-member, per-tool, Fernet-encrypted) |
| Custom OAuth (e.g. some CRMs not in Composio) | `globus_oauth_connections` (existing, generalise from Google-only) |

Marketer adds credentials at `/members/narada/credentials` —
one section per tool, paste key or click "Connect via Composio."

## 7. UX

### 7.1 Chat-driven (primary for power users)

The LLM tool surface:

| Tool | Purpose |
|---|---|
| `narada_create_campaign(name, product, icp_description, sender, lead_source)` | Create + persist a campaign. |
| `narada_find_leads(campaign_id, count)` | Use the campaign's chosen lead source to populate prospects. |
| `narada_verify_emails(campaign_id, verifier=None)` | Run all unverified prospects through the chosen verifier. |
| `narada_enrich_prospects(campaign_id, depth='basic'|'deep')` | Pull research signals. |
| `narada_draft_copy(campaign_id, prospect_id=None, variants=3)` | Generate per-prospect copy. |
| `narada_review_queue(campaign_id)` | List prospects awaiting copy approval. |
| `narada_approve_copy(prospect_id, variant_idx)` | Mark one variant ready to send. |
| `narada_send_campaign(campaign_id, sender=None, max_per_day=50)` | Fire all approved sends through chosen sender. |
| `narada_check_replies(campaign_id)` | Pull replies, classify, push hot ones to CRM. |
| `narada_campaign_stats(campaign_id)` | Sent / opened / replied / unsubscribed / bounced rates. |
| `narada_list_campaigns()` | All campaigns for this member. |
| `narada_suppression_add(email, reason)` | Manual add to suppression list. |
| `narada_clone_winning_angle(from_campaign_id, to_campaign_id)` | Copy successful copy patterns. |

### 7.2 Dashboard (primary for non-power users)

Routes:

| Route | Purpose |
|---|---|
| `/members/narada` | Campaign list + create-new button. |
| `/members/narada/new` | Campaign builder (ICP, sender choice, lead source, target volume). |
| `/members/narada/<id>` | Campaign detail: status, prospects table, copy review queue, send button, live stats. |
| `/members/narada/<id>/sequence` | Multi-step sequence editor (drag-drop). |
| `/members/narada/<id>/replies` | Inbox view + classifications + CRM-push status. |
| `/members/narada/credentials` | Per-tool credential management. |
| `/members/narada/suppression` | Suppression list management. |
| `/members/narada/analytics` | Cross-campaign analytics. |

Both surfaces hit the same backend functions — chat is a thin
front-end for the same operations the dashboard exposes.

## 8. Data model

Schema additions (idempotent CREATE TABLE IF NOT EXISTS):

```sql
-- One row per campaign
CREATE TABLE globus_narada_campaigns (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  member_email    VARCHAR(320) NOT NULL,
  name            VARCHAR(255),
  product         VARCHAR(120),               -- "VideoraIQ", "AdsGPT"
  icp_description TEXT,                       -- free-form natural language
  icp_filters     JSON,                       -- structured filters (industry, role, company_size)
  lead_source     VARCHAR(80),                -- plugin name: "prospeo"
  verifier        VARCHAR(80),                -- "neverbounce"
  sender          VARCHAR(80),                -- "gmail", "smartlead"
  sender_config   JSON,                       -- {from_addr, daily_cap, throwaway_domains}
  crm             VARCHAR(80),                -- "freshsales", null = no sync
  sequence_steps  JSON,                       -- [{day:1, copy_id:1}, {day:4, copy_id:2}]
  status          ENUM('draft','reviewing','sending','paused','done') DEFAULT 'draft',
  stats           JSON,                       -- {sent, opened, replied, ...}
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY ix_member_status (member_email, status)
);

-- One row per prospect per campaign
CREATE TABLE globus_narada_prospects (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  campaign_id     BIGINT NOT NULL,
  member_email    VARCHAR(320) NOT NULL,
  first_name      VARCHAR(120),
  last_name       VARCHAR(120),
  email           VARCHAR(320),
  email_verified  TINYINT(1) DEFAULT 0,
  company         VARCHAR(255),
  company_domain  VARCHAR(255),
  title           VARCHAR(255),
  linkedin_url    VARCHAR(512),
  enrichment      JSON,                       -- {tech_stack, recent_posts, news}
  copy_variants   JSON,                       -- [{subject, body, score}, ...]
  approved_variant_idx INT,                   -- null until human approves
  status          ENUM('new','verified','enriched','drafted','approved','sent','replied','unsubscribed','bounced','suppressed') DEFAULT 'new',
  source_metadata JSON,                       -- raw from the lead source for debugging
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_campaign_email (campaign_id, email),
  KEY ix_member_status (member_email, status)
);

-- One row per outbound send + reply
CREATE TABLE globus_narada_sends (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  campaign_id     BIGINT NOT NULL,
  prospect_id     BIGINT NOT NULL,
  member_email    VARCHAR(320) NOT NULL,
  step_idx        INT DEFAULT 0,              -- 0 = first send, 1 = follow-up 1
  sender          VARCHAR(80),                -- which plugin sent it
  from_addr       VARCHAR(320),
  to_addr         VARCHAR(320),
  subject         VARCHAR(512),
  body_preview    TEXT,
  message_id      VARCHAR(255),               -- RFC 822 Message-ID (for threading)
  thread_id       VARCHAR(255),               -- Gmail-style thread
  external_id     VARCHAR(255),               -- Smartlead/Lemlist message id
  status          ENUM('queued','sent','delivered','opened','replied','bounced','spam','failed') DEFAULT 'queued',
  reply_classification ENUM('interested','not_interested','ooo','unsubscribe','referred','wrong_person','question') NULL,
  reply_body      TEXT,
  reply_received_at TIMESTAMP NULL,
  sent_at         TIMESTAMP NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY ix_campaign (campaign_id),
  KEY ix_member_status (member_email, status)
);

-- Per-member suppression list (do-not-contact)
CREATE TABLE globus_narada_suppression (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  member_email    VARCHAR(320) NOT NULL,
  email           VARCHAR(320) NOT NULL,
  reason          ENUM('unsubscribed','bounced','manual','spam_complaint') NOT NULL,
  added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_member_email (member_email, email)
);

-- Per-member, per-tool credentials (Fernet-encrypted)
CREATE TABLE globus_narada_credentials (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  member_email    VARCHAR(320) NOT NULL,
  tool            VARCHAR(80) NOT NULL,       -- "smartlead", "prospeo"
  credential_enc  BLOB NOT NULL,              -- Fernet-encrypted JSON {api_key, ...}
  status          ENUM('active','expired','revoked') DEFAULT 'active',
  last_used_at    TIMESTAMP NULL,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_member_tool (member_email, tool)
);

-- Winning-angle memory (cross-campaign learning)
CREATE TABLE globus_narada_angle_memory (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  member_email    VARCHAR(320) NOT NULL,
  icp_tag         VARCHAR(120),               -- "fintech_cmo", "ecom_owner_50_500"
  angle_summary   VARCHAR(512),               -- "Opening with X stat works"
  example_copy    TEXT,
  campaigns_used  JSON,                       -- [campaign_id, ...]
  reply_rate      DECIMAL(5,2),
  sample_size     INT,
  last_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY ix_member_icp (member_email, icp_tag)
);
```

## 9. Build phases

The product is non-phased (every category gets all 10 tools
eventually), but engineering work has unavoidable sequencing — the
spine must come before plugins:

### Phase 0 — Plugin spine (must come first; nothing works without it)

- `server/narada_plugins/__init__.py` — registry + protocols
- `server/narada_plugins/types.py` — Lead / ICPFilters / SendResult / etc. dataclasses
- `server/narada_core.py` — campaign + prospect + send CRUD; orchestration helpers
- `server/narada_copy.py` — LLM-driven copy gen using existing `globus_call_chat`
- Schema deltas (above)
- 1-2 days of focused work

### Phase 1 — First wave (everything I need for end-to-end smoke)

- `outreach_plugins/gmail_composio.py` — Sender via Composio Gmail OAuth + `gmail.send` scope
- `outreach_plugins/prospeo.py` — LeadSource + Verifier (Prospeo does both)
- `outreach_plugins/freshsales.py` — CRM (reuse Sumit's existing Freshsales client)
- Dashboard: `/members/narada` (campaign list + create + detail + review queue)
- LLM tools wired into orchestrator
- Claude Skill that runs the full pipeline
- 2-3 days

### Phase 2 — Sender expansion

- Smartlead, Lemlist, EmailBison, Instantly, Apollo Sequences
- Custom SMTP fallback (AWS SES, Mailgun, Postmark, Brevo)
- 1-2 days per integration

### Phase 3 — Lead source + verifier expansion

- Apollo, Hunter, Clay, ZoomInfo, FindyMail, RocketReach
- NeverBounce, ZeroBounce, MillionVerifier
- 1-2 days per integration

### Phase 4 — CRM expansion + LinkedIn

- HubSpot, Salesforce, Pipedrive, Close (mostly via Composio — fast)
- Heyreach, Dripify, Expandi, Lemlist (LinkedIn), Phantombuster
- 1-2 days per integration

### Phase 5 — Sequences + warmup + analytics

- Multi-step sequence engine + scheduling
- Warmup provider plugins (Smartlead, Lemwarm, Warmup Inbox, etc.)
- Analytics dashboard + winning-angle memory
- 1-2 weeks

### Phase 6 — Voice + WhatsApp + multi-tenant

- Phone / voice plugins (AirCall, Twilio, etc.)
- WhatsApp plugins (Wati, Twilio, Meta Cloud API)
- Agency multi-tenant mode (sub-accounts, per-client isolation)
- 2-3 weeks

**Realistic total wall-clock for all 10 integrations per category**:
6-10 weeks of focused build, assuming credentials arrive in time.

## 10. Success metrics

| Metric | V1 target | V3 target |
|---|---|---|
| Time to first send (new marketer, fresh install) | < 30 min | < 10 min |
| Skill auto-run end-to-end (research → copy → send) | < 5 min for 50-lead campaign | < 2 min |
| Reply classification accuracy | > 85% | > 95% |
| Marketer time per campaign | < 30 min | < 10 min |
| Composio + custom plugin count | 5 working | 50 working |

## 11. Open questions

1. **Pricing model when Globus is OSS** — is Narada a paid
   add-on for the buildwithsumit hosted version? Free for self-host?
2. **Per-member credential storage** — Composio holds OAuth tokens
   server-side; API keys live in our DB (Fernet-encrypted). Single
   Fernet key per install OR per-member? Latter is safer but more
   ops complexity.
3. **LinkedIn TOS risk** — Heyreach/Dripify/Phantom all operate in a
   grey zone. Do we ship them as plugins with a "your TOS risk"
   disclaimer or refuse to wire them?
4. **Domain throwaway management** — does Globus help the marketer
   buy + warm new domains, or just let them paste the SMTP creds for
   domains they already have?
5. **Skill-driven auto-pilot vs human approval** — default mode for
   new marketers? Power-users want auto; cautious users want approve-
   every-send.
6. **AI cost ceiling per campaign** — copy gen for 1000 leads at
   ~$0.01/prospect = $10/campaign. Cap per-campaign LLM spend?
   Charge per send for self-hosted?

## 12. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Marketer pastes API keys → leak in logs / DB dump | Fernet encryption; log redaction on the `credential_enc` column; key rotation pattern. |
| Bad copy → globussoft.com domain reputation hit | Default to throwaway-domain via Smartlead for high-volume; require dedicated alias for Gmail-only mode; per-campaign send cap. |
| Plugin author writes infinite loop / leaks | Per-plugin timeout (30s default); per-plugin error isolation (one plugin's exception doesn't kill the campaign). |
| Marketer reaches Composio's free tier limit | Show usage meter on dashboard; warn before hard limit; doc upgrade path. |
| LinkedIn plugin gets a marketer's LinkedIn account suspended | TOS disclaimer; ship LinkedIn plugins as opt-in not default; rate-limit aggressively. |
| Composio outage takes down ALL OAuth-backed plugins | Status check + circuit breaker; degrade gracefully ("Composio unavailable; only API-key tools work right now"). |

## 13. Out of scope (for V1, possibly forever)

- Multi-tenant agency mode (V5+)
- Live A/B testing engine (V3)
- AI-generated voice for cold calls
- SMS outbound (lower priority than WhatsApp in target markets)
- Direct mail / postal automation
- Video personalisation (Loom / Vidyard equivalents)

---

## Sign-off

When you're ready, reply with anything that needs changing or
"approved" — I start with Phase 0 (plugin spine) immediately on
approval. Phase 1 follows directly after with no further decision
gates needed (Gmail + Prospeo + Freshsales — all credentials you
already have or are wiring).

Phases 2+ each unblock as you provide the next batch of API keys.
No more architecture decisions; just credential availability.
