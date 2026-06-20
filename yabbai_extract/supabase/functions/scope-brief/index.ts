// scope-brief — turns a client's plain-words brief into a build plan mapped onto the
// FIXED catalog. The AI scopes; it cannot invent prices (DB trigger enforces it too).
// Quotes stay non-binding until paid AND Director-confirmed.
// Deploy: supabase functions deploy scope-brief
import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const CATALOG = {
  packages: {
    "Starter":  { fee: 1500, fits: "1 core automation (lead capture → CRM → instant reply), 5-7 day delivery" },
    "Growth":   { fee: 4000, fits: "3 systems (capture + outreach/nurture + onboarding/billing), AI agent, dashboard" },
    "Full Ops": { fee: 8000, fits: "the complete 6-system spine incl. support chatbot + metrics" },
  },
  tiers: {
    "Maintain": { mrr: 500,  fits: "monitoring, fixes, monthly report" },
    "Optimize": { mrr: 1500, fits: "Maintain + 2 improvements/mo + priority support + fleet upgrades" },
    "Scale":    { mrr: 3500, fits: "Optimize + dedicated build hours + strategy call + SLA" },
  },
};

const SYS = `You scope client briefs for an Australian done-for-you automation agency.
Map the brief onto EXACTLY this catalog (choose one package and one tier; never invent
prices or services): ${JSON.stringify(CATALOG)}.
Reply with ONLY JSON:
{"summary":"2 sentences, plain English, client's own outcome language",
 "deliverables":["3-6 concrete items they will receive"],
 "package":"Starter|Growth|Full Ops",
 "tier":"Maintain|Optimize|Scale",
 "timeline":"e.g. 5-7 business days",
 "end_user_benefit":"1 sentence: what THEIR customers feel",
 "out_of_scope":["anything they asked for that the package does not include"],
 "needs_human_call": true|false  // true if the brief is ambiguous, regulated, or custom}
Be honest in out_of_scope. Never promise revenue outcomes.`;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const supa = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_ANON_KEY")!,
      { global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } } },
    );
    const { data: { user } } = await supa.auth.getUser();
    if (!user) return new Response(JSON.stringify({ error: "auth required" }),
      { status: 401, headers: { ...cors, "content-type": "application/json" } });

    const { order_id } = await req.json();
    const { data: order } = await supa.from("orders").select("*")
      .eq("id", order_id).eq("client_user", user.id).single();
    if (!order) throw new Error("order not found");

    // Call the existing ai-proxy (Anthropic primary / Ollama fallback)
    const r = await fetch(`${Deno.env.get("SUPABASE_URL")}/functions/v1/ai-proxy`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: req.headers.get("Authorization") ?? "",
        apikey: Deno.env.get("SUPABASE_ANON_KEY") ?? "",
      },
      body: JSON.stringify({ system: SYS, prompt: `Client brief:\n${order.brief}` }),
    });
    if (!r.ok) throw new Error(`ai-proxy ${r.status}`);
    const { text } = await r.json();
    const m = text.match(/\{[\s\S]*\}/);
    if (!m) throw new Error("scope parse failed");
    const scope = JSON.parse(m[0]);

    // Pricing comes from the catalog, NEVER from the model output.
    const pkg = CATALOG.packages[scope.package as keyof typeof CATALOG.packages];
    const tier = CATALOG.tiers[scope.tier as keyof typeof CATALOG.tiers];
    if (!pkg || !tier) throw new Error("scope outside catalog");

    await supa.from("orders").update({
      scope,
      package: scope.package, fee: pkg.fee,
      tier: scope.tier, mrr: tier.mrr,
      status: "Scoped",
    }).eq("id", order_id);

    return new Response(JSON.stringify({ ok: true, scope, fee: pkg.fee, mrr: tier.mrr }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }),
      { status: 500, headers: { ...cors, "content-type": "application/json" } });
  }
});
