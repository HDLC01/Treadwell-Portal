-- DEV/STAGING ONLY. Creates a stand-in `drafts` table (owned by the proposal
-- tool in prod) and seeds one sample published proposal so the portal can be
-- run and tested without the proposal tool. NEVER run this against prod.

create table if not exists public.drafts (
  id          text primary key,
  data        jsonb not null default '{}'::jsonb,
  owner_email text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  deleted_at  timestamptz
);

insert into public.drafts (id, data, owner_email)
values (
  'dev-proposal-acme',
  '{
    "project_name": "Acme Distribution Warehouse",
    "city_state": "Kansas City, KS",
    "contact_name": "Jordan Rivera",
    "contact_email": "customer@example.com",
    "work_type": "epoxy",
    "system_name": "Matte Epoxy",
    "texture": "Smooth, slip-resistant",
    "proposal_date": "2026-06-20",
    "site_visit_date": "2026-06-18",
    "scope_notes": "Surface prep by diamond grinding, full broadcast matte epoxy system across the warehouse floor, and cove base at perimeter walls.",
    "exclusions": "Moisture mitigation, crack repair beyond hairline, after-hours work, and removal of existing coatings unless noted.",
    "computed_bid": { "full_bid": { "total_base_bid": 21937, "per_sf": 8.77 } },
    "price_lines": [
      {"label": "24-hour rapid cure (optional)", "amount": 500},
      {"label": "Freight / shipping", "amount": 150}
    ],
    "rooms": [
      {"id": "room_1", "name": "Base Bid — Matte Epoxy", "is_base": true,
       "system_desc": "Matte Epoxy, 2,500 SF", "bid": {"total": 21937, "sales_tax": 1438}},
      {"id": "room_2", "name": "Upgrade — Polished Concrete Combo", "is_base": false,
       "base_total": 21937, "system_desc": "Epoxy + Polish combo, 2,500 SF",
       "bid": {"total": 28500, "sales_tax": 1850}}
    ]
  }'::jsonb,
  'estimator@wetreadwell.com'
)
on conflict (id) do update set data = excluded.data;

insert into public.portal_proposals
  (proposal_id, token, customer_email, customer_name, project_name, proposal_status)
values
  ('dev-proposal-acme', 'dev-token-acme-7Yx2Qn4PpL9', 'customer@example.com',
   'Jordan Rivera', 'Acme Distribution Warehouse', 'sent')
on conflict (proposal_id) do nothing;
