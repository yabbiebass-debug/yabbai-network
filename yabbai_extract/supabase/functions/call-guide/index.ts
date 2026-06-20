// call-guide — generates an INDIVIDUALIZED call guide for one REAL lead, so a caller
// (you, a contractor, a manager) opens a lead and gets a tailored script: opener tied to
// that business's niche + pain, three discovery questions, the relevant package to pitch,
// objection handling, and a clear next step. JWT-required (staff only).
// Deploy: supabase functions deploy call-guide
import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const SYS = `You write a one-call phone guide for a sales caller at an Australian done-for-you
automation agency (BASHAM Automations). The caller is ringing a small business to offer
automation that fixes a specific pain. Catalog to pitch from (pick the ONE that fits):
Starter $1,500 (1 core automation), Growth $4,000 (3 systems), Full Ops $8,000 (full spine);
retainers Maintain $500/mo, Optimize $1,500/mo, Scale $3,500/mo.
Given the lead's business, niche, suburb and pain, output ONLY JSON:
{
 "opener":"2 sentences, warm AU tone, names the business + the specific pain as the reason for the call",
 "discovery":["3 short questions that get them admitting the cost of the pain"],
 "pitch":"2-3 sentences mapping ONE package to their pain, with the time/lead saving framed in their terms",
 "recommend":{"package":"Starter|Growth|Full Ops","tier":"Maintain|Optimize|Scale"},
 "objections":[{"if":"common objection they'll raise","say":"the honest, non-pushy response"}],
 "close":"the specific next step to ask for — usually a 15-min screen-share or a follow-up time",
 "donts":["1-2 things NOT to promise (e.g. guaranteed revenue, anything regulated)"]
}
Never fabricate facts about the business. Never promise revenue outcomes. For regulated
niches (NDIS, medical, legal, financial) add a 'donts' note to keep claims compliant.`;

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

    const { lead_id } = await req.json();
    const { data: lead } = await supa.from("leads").select("*").eq("id", lead_id).single();
    if (!lead) throw new Error("lead not found");

    const r = await fetch(`${Deno.env.get("SUPABASE_URL")}/functions/v1/ai-proxy`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: req.headers.get("Authorization") ?? "",
        apikey: Deno.env.get("SUPABASE_ANON_KEY") ?? "",
      },
      body: JSON.stringify({
        system: SYS,
        prompt: `Business: ${lead.biz}\nNiche: ${lead.niche ?? "?"}\nSuburb: ${lead.suburb ?? "?"}\nPain: ${lead.pain ?? "?"}\nNotes: ${lead.notes ?? ""}`,
      }),
    });
    if (!r.ok) throw new Error(`ai-proxy ${r.status}`);
    const { text } = await r.json();
    const m = text.match(/\{[\s\S]*\}/);
    if (!m) throw new Error("guide parse failed");
    const guide = JSON.parse(m[0]);

    return new Response(JSON.stringify({ ok: true, guide }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e) }),
      { status: 500, headers: { ...cors, "content-type": "application/json" } });
  }
});
