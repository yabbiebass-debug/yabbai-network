// cycle-runner — one real agent cycle over YOUR data.
// QUALIFIER scores unscored leads · PITCHER drafts outreach for Hot leads (→ gate)
// SUPPORTER ensures churn-risk has a save play queued · TREASURER logs the money.
// Runs with the caller's JWT, so RLS applies — the runner can only touch your rows.
// It has NO code path that creates clients, sends messages, or moves money.
// Deploy: supabase functions deploy cycle-runner
import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const QUALIFIER_SYS = `You score inbound leads for a done-for-you automation agency serving Australian small service businesses (1-10 staff: trades, clinics, gyms, real estate/accommodation, NDIS). Score 0-100 on ICP fit from business type, stated pain, and buying signals. Reply with ONLY JSON: {"score":0-100,"fit":"Hot|Warm|Cold","reason":"one sentence"}. Never invent details not present in the lead.`;

const PITCHER_SYS = `You draft cold outreach for an automation agency (AU small service businesses). Given a prospect, write a specific, non-salesy first touch. Lead with a concrete observation about THEIR business and one automation that saves time or wins leads. Offer a free automation audit as the hook. Under 90 words. Reply with ONLY JSON: {"subject":"","email":"","dm":""}.`;

async function ai(req: Request, system: string, prompt: string): Promise<string> {
  const url = `${Deno.env.get("SUPABASE_URL")}/functions/v1/ai-proxy`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      Authorization: req.headers.get("Authorization") ?? "",
      apikey: Deno.env.get("SUPABASE_ANON_KEY") ?? "",
    },
    body: JSON.stringify({ system, prompt }),
  });
  if (!r.ok) throw new Error(`ai-proxy ${r.status}`);
  const { text } = await r.json();
  return text;
}

function parseJSON<T>(raw: string): T | null {
  try {
    const m = raw.match(/\{[\s\S]*\}/);
    return m ? JSON.parse(m[0]) as T : null;
  } catch { return null; }
}

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

    const log = (agent: string, message: string, n: number) =>
      supa.from("events").insert({ agent, message, cycle_n: n });

    // cycle number
    const { count } = await supa.from("cycles").select("*", { count: "exact", head: true });
    const n = (count ?? 0) + 1;
    await supa.from("cycles").insert({ n });

    const summary: Record<string, number> = { qualified: 0, drafted: 0, saves: 0 };

    // ---- QUALIFIER: score up to 5 unscored leads (cost-capped) ----
    const { data: unscored } = await supa.from("leads")
      .select("*").is("fit", null).neq("stage", "Dead").limit(5);
    for (const l of unscored ?? []) {
      const raw = await ai(req, QUALIFIER_SYS,
        `Business: ${l.biz}\nNiche: ${l.niche ?? "?"}\nSuburb: ${l.suburb ?? "?"}\nPain: ${l.pain ?? "?"}\nNotes: ${l.notes ?? ""}`);
      const j = parseJSON<{ score: number; fit: string; reason: string }>(raw);
      if (!j) continue;
      const fit = ["Hot", "Warm", "Cold"].includes(j.fit) ? j.fit : "Warm";
      await supa.from("leads").update({
        score: Math.max(0, Math.min(100, j.score | 0)),
        fit,
        stage: fit === "Cold" ? "Dead" : l.stage,
      }).eq("id", l.id);
      summary.qualified++;
    }
    if (summary.qualified) await log("QUALIFIER", `scored ${summary.qualified} lead(s) against ICP`, n);

    // ---- PITCHER: draft outreach for Hot leads → Director gate ----
    const { data: hot } = await supa.from("leads")
      .select("*").eq("fit", "Hot").eq("stage", "New").limit(3);
    for (const l of hot ?? []) {
      const raw = await ai(req, PITCHER_SYS,
        `Business: ${l.biz}\nNiche: ${l.niche ?? "?"}\nSuburb: ${l.suburb ?? "?"}\nPain: ${l.pain ?? "?"}`);
      const j = parseJSON<{ subject: string; email: string; dm: string }>(raw);
      if (!j) continue;
      await supa.from("leads").update({ stage: "Outreach drafted" }).eq("id", l.id);
      await supa.from("approvals").insert({
        type: "outreach",
        title: `Send outreach → ${l.biz}`,
        detail: `Subject: ${j.subject}\n\n${j.email}\n\nDM: ${j.dm}`,
        agent: "PITCHER",
        payload: { lead_id: l.id, draft: j },
      });
      summary.drafted++;
    }
    if (summary.drafted) await log("PITCHER", `queued ${summary.drafted} outreach draft(s) for your approval`, n);

    // ---- SUPPORTER: every churn-risk client has a save play queued ----
    const { data: risky } = await supa.from("clients")
      .select("id,biz").eq("health", "risk");
    for (const c of risky ?? []) {
      const { count: pend } = await supa.from("approvals")
        .select("*", { count: "exact", head: true })
        .eq("type", "save").eq("status", "pending")
        .eq("payload->>client_id", c.id);
      if (!pend) {
        await supa.from("approvals").insert({
          type: "save",
          title: `Churn risk → ${c.biz}`,
          detail: "Payment failed or activity dropped. Save play: check-in + one quick win this week.",
          agent: "SUPPORTER",
          payload: { client_id: c.id },
        });
        summary.saves++;
      }
    }
    if (summary.saves) await log("SUPPORTER", `flagged ${summary.saves} churn risk(s) with save plays`, n);

    // ---- TREASURER: read the money, never move it ----
    const { data: m } = await supa.from("money_view").select("*").single();
    await log("TREASURER",
      `MRR $${m?.mrr ?? 0}/mo · cash $${m?.cash_collected ?? 0} · ${m?.active_clients ?? 0} active`, n);

    await supa.from("cycles").update({ summary }).eq("n", n);
    return new Response(JSON.stringify({ cycle: n, ...summary }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }),
      { status: 500, headers: { ...cors, "content-type": "application/json" } });
  }
});
