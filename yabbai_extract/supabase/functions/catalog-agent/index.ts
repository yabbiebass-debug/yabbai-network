// catalog-agent — three brains for scaling the catalog HONESTLY (JWT/staff-only):
//   "ideate" : your real skills/assets -> a batch of sellable product specs, each tagged
//              with build_effort so you see what's "have it" vs "needs building".
//   "listing": a real asset description -> polished store copy (title/tagline/desc/includes/config).
//   "audit"  : white-hat review of a product — security, licence, quality, and claims checks —
//              returns pass/flag. Nothing lists without this passing (DB-enforced too).
// Deploy: supabase functions deploy catalog-agent
import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const IDEATE_SYS = `You are a product strategist for a solo technical founder (automation,
web, AI, music, crypto-curious). Given their real skills and existing assets, propose
sellable DIGITAL products (templates, kits, tools, guides, services) across a WIDE range —
hobbyist to startup, $19 to $499. For EACH idea, honestly tag build_effort:
"have it" (an existing asset basically covers it), "hours" (a focused build), or "days"
(a real project). Never invent capabilities they didn't claim. Reply ONLY JSON:
{"ideas":[{"title":"","pitch":"one line","audience":"","build_effort":"have it|hours|days",
"price_band":49,"includes":["3-5 items"],"config_schema":{"goal":"","ask_about":["..."]}}]}
Give 6-10 ideas, ordered easiest-to-ship first.`;

const LISTING_SYS = `You write store listings for digital products. Given the asset and a few
facts, output polished, honest marketplace copy. No hype, no false promises, no revenue
guarantees. Reply ONLY JSON:
{"title":"","tagline":"one line","description":"2-3 sentences","includes":["3-6 concrete deliverables"],
"price_aud":49,"config_schema":{"goal":"tailor it to the buyer","ask_about":["3-5 questions"]}}`;

const AUDIT_SYS = `You are a white-hat product auditor: 50 years equivalent across security,
engineering and compliance, reviewing a digital product before it goes on sale. Assess it
honestly on four axes and return a verdict. FLAG (do not pass) if it: contains or enables
malware/exploits/scraping-of-private-data/credential theft; ships secrets or keys; violates
a licence (e.g. reselling GPL/proprietary code as your own); makes unsupportable claims
(guaranteed income, medical/legal/financial advice); or is too thin to deliver its promise.
Otherwise PASS. Reply ONLY JSON:
{"verdict":"pass|flag",
 "checks":{"security":"ok|issue + note","licence":"ok|issue + note","quality":"ok|issue + note","claims":"ok|issue + note"},
 "summary":"2 sentences, plain, what to fix if flagged"}`;

async function ai(req: Request, system: string, prompt: string, max = 1200): Promise<string> {
  const r = await fetch(`${Deno.env.get("SUPABASE_URL")}/functions/v1/ai-proxy`, {
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
const grab = (t: string) => { const m = t.match(/\{[\s\S]*\}/); if (!m) throw new Error("parse failed"); return JSON.parse(m[0]); };

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

    const body = await req.json();
    const mode = body.mode;

    if (mode === "ideate") {
      const out = grab(await ai(req, IDEATE_SYS,
        `MY SKILLS: ${String(body.skills ?? "").slice(0, 1500)}\nMY EXISTING ASSETS: ${String(body.assets ?? "").slice(0, 1500)}`));
      // persist as specs
      for (const idea of (out.ideas ?? []).slice(0, 10)) {
        await supa.from("product_specs").insert({
          title: String(idea.title ?? "").slice(0, 160),
          pitch: idea.pitch, audience: idea.audience,
          build_effort: ["have it", "hours", "days"].includes(idea.build_effort) ? idea.build_effort : "hours",
          price_band: Number(idea.price_band) || 0,
          spec: { includes: idea.includes ?? [], config_schema: idea.config_schema ?? {} },
        });
      }
      return new Response(JSON.stringify({ ok: true, ...out }), { headers: { ...cors, "content-type": "application/json" } });
    }

    if (mode === "listing") {
      const out = grab(await ai(req, LISTING_SYS,
        `ASSET: ${String(body.asset ?? "").slice(0, 1500)}\nFACTS: ${String(body.facts ?? "").slice(0, 800)}`));
      return new Response(JSON.stringify({ ok: true, listing: out }), { headers: { ...cors, "content-type": "application/json" } });
    }

    if (mode === "audit") {
      const { data: p } = await supa.from("vault_products").select("*").eq("id", body.product_id).single();
      if (!p) throw new Error("product not found");
      const out = grab(await ai(req, AUDIT_SYS,
        `TITLE: ${p.title}\nDESC: ${p.description}\nINCLUDES: ${(p.includes || []).join("; ")}\n` +
        `PRICE: $${p.price_aud}\nFULFILLMENT: ${p.fulfillment}\nASSET_REF: ${p.asset_ref ?? "(none)"}`));
      await supa.rpc("record_audit", {
        p_id: body.product_id, p_verdict: out.verdict,
        p_checks: out.checks ?? {}, p_summary: out.summary ?? "",
      });
      return new Response(JSON.stringify({ ok: true, audit: out }), { headers: { ...cors, "content-type": "application/json" } });
    }

    throw new Error("unknown mode");
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e) }),
      { status: 500, headers: { ...cors, "content-type": "application/json" } });
  }
});
