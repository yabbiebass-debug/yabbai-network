// diagnose — the SEED. A free AI diagnostic anyone can run on their project (hobbyist to
// startup). It asks a couple of sharp questions, returns a genuinely useful personalized
// fix-plan, and — only when they volunteer an email — drops an inbound lead into the realm.
// Self-spreading: every result gets a shareable link. Public, guarded like other public fns.
// Deploy: supabase functions deploy diagnose --no-verify-jwt
import { createClient } from "npm:@supabase/supabase-js@2";
import { guard } from "../_shared/ratelimit.ts";

const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "content-type" };
const DIRECTOR = () => Deno.env.get("OWNER_USER_ID") ?? "";

async function ai(system: string, prompt: string, max = 800): Promise<string> {
  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) throw new Error("brain offline");
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({
      model: Deno.env.get("ANTHROPIC_MODEL") ?? "claude-sonnet-4-6",
      max_tokens: max, system, messages: [{ role: "user", content: prompt }],
    }),
  });
  if (!r.ok) throw new Error(`anthropic ${r.status}`);
  const d = await r.json();
  return (d.content ?? []).map((b: { text?: string }) => b.text ?? "").join("");
}

const NEXT_SYS = `You triage anyone building with AI or tech — hobbyist, vibe-coder, startup
founder, or small business. Given their situation and answers so far, ask the ONE next
question that most sharpens your diagnosis (their stack, the exact symptom, what they've
tried, their goal). One question, plain and friendly. If you have enough for a genuinely
useful plan (or 5+ asked), done=true. Reply ONLY JSON:
{"done":false,"question":"...","why":"short clause"} or {"done":true}`;

const PLAN_SYS = `You write a free, genuinely useful fix-plan for someone building with AI/tech.
Use ONLY what they told you. Output clean markdown:
1. One-line read of their actual situation.
2. "The likely cause" — 1-2 sentences, honest, no jargon-dumping.
3. "Do this next" — 3-5 concrete numbered steps they can try themselves right now.
4. "If you'd rather not DIY" — one line naming which kind of help fits (a quick fix
   session, a project rescue, an automation audit) — soft, not pushy.
Be real and specific — this is a gift that earns trust. No false promises. If their issue
touches anything regulated or risky, say so plainly. End with: "Built for your answers by
YABBAI — share it, or grab the done-for-you option if you want it handled."`;

function clampQA(raw: unknown) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, 6).map((x) => ({ q: String(x?.q ?? "").slice(0, 300), a: String(x?.a ?? "").slice(0, 500) }));
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const b = await req.json();
    if (b.website) return new Response(JSON.stringify({ ok: true }), { headers: cors }); // honeypot

    // Rate-limit the public door: 20 requests / 5 min / IP (skip for share-link reads).
    if (!b.share_id) {
      const blocked = await guard(req, { bucket: "diagnose", limit: 20, windowSecs: 300, turnstileToken: b.turnstile });
      if (blocked) return blocked;
    }

    // Public result fetch by share_id (the shareable seed link)
    if (b.share_id) {
      const supa = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SERVICE_ROLE_KEY")!);
      const { data } = await supa.from("diagnostics").select("situation,segment,fix_plan,recommended,created_at")
        .eq("share_id", String(b.share_id).slice(0, 40)).single();
      if (!data) throw new Error("not found");
      return new Response(JSON.stringify({ ok: true, result: data }), { headers: { ...cors, "content-type": "application/json" } });
    }

    const action = b.action === "final" ? "final" : "next";
    const segment = String(b.segment ?? "other").slice(0, 20);
    const situation = String(b.situation ?? "").trim().slice(0, 1200);
    const qa = clampQA(b.qa);
    if (situation.length < 5) throw new Error("tell us what you're building");

    const ctx = `SEGMENT: ${segment}\nSITUATION: ${situation}\nQ&A:\n${qa.map((x) => `Q:${x.q}\nA:${x.a}`).join("\n") || "(none)"}`;

    if (action === "next") {
      const raw = await ai(NEXT_SYS, ctx, 300);
      const m = raw.match(/\{[\s\S]*\}/); const j = m ? JSON.parse(m[0]) : { done: true };
      if (qa.length >= 5) j.done = true;
      return new Response(JSON.stringify(j), { headers: { ...cors, "content-type": "application/json" } });
    }

    // final: build the plan, store it, capture an inbound lead if email volunteered
    const plan = await ai(PLAN_SYS, ctx, 1000);
    const supa = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SERVICE_ROLE_KEY")!);
    const share_id = "dx_" + crypto.randomUUID().slice(0, 12);
    const email = String(b.email ?? "").trim().slice(0, 160);
    const validEmail = /.+@.+\..+/.test(email) ? email : null;
    const stack = String(b.stack ?? "").slice(0, 200);

    await supa.from("diagnostics").insert({
      director: DIRECTOR(), segment, situation, stack, qa, fix_plan: plan,
      email: validEmail, share_id,
    });

    if (validEmail && DIRECTOR()) {
      await supa.from("leads").insert({
        owner: DIRECTOR(),
        biz: (situation.slice(0, 60) || validEmail),
        email: validEmail,
        niche: segment,
        pain: "ran the free AI diagnostic",
        source: "diagnostic",
        notes: `Diagnostic (${segment}). Situation: ${situation.slice(0, 300)}`,
      });
    }

    return new Response(JSON.stringify({ ok: true, plan, share_id }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e) }),
      { status: 400, headers: { ...cors, "content-type": "application/json" } });
  }
});
