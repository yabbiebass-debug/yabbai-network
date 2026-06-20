// vault-config — the configurator brain. Public (it serves anonymous shoppers),
// guarded like realm-intake: honeypot, hard caps, product-key lookup.
// Two actions:
//   "next"  → given the Q&A so far, ask the ONE next most useful question
//             (the "prompts you for every change, like a professional LLM" feel)
//   "final" → produce a personalized setup guide; store the session; if an email
//             was given, write a LEAD into the realm — inbound, zero outreach.
// Deploy: supabase functions deploy vault-config --no-verify-jwt
import { createClient } from "npm:@supabase/supabase-js@2";
import { guard } from "../_shared/ratelimit.ts";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
};

async function ai(system: string, prompt: string, maxTokens = 700): Promise<string> {
  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) throw new Error("brain offline");
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({
      model: Deno.env.get("ANTHROPIC_MODEL") ?? "claude-sonnet-4-6",
      max_tokens: maxTokens, system,
      messages: [{ role: "user", content: prompt }],
    }),
  });
  if (!r.ok) throw new Error(`anthropic ${r.status}`);
  const d = await r.json();
  return (d.content ?? []).map((b: { text?: string }) => b.text ?? "").join("");
}

const NEXT_SYS = `You are the setup configurator for a digital product. Given the product's
config goals and the Q&A so far, decide the single next most useful question to fully
personalise this buyer's setup. Be specific and concrete (names, colours, suburbs, tools,
links — every detail that would change their build). One question only. Plain, friendly
Australian tone. If you already have enough for an excellent personalised setup (or 10+
questions asked), set done=true. Reply ONLY JSON:
{"done":false,"question":"...","why":"one short clause shown under the question"}
or {"done":true}`;

const FINAL_SYS = `You write a personalised setup guide for a digital product the buyer is
configuring. Use ONLY their answers — never invent details they didn't give. Output clean
markdown: a one-line summary of THEIR setup, then numbered steps customised with their
exact names/colours/links/choices, then a short "next tweaks you might want" list. Plain
Australian English. No prices, no revenue promises, no legal/medical advice. End with:
"This guide was generated for your answers — buy the product to get the files it configures."`;

function clampQA(raw: unknown): { q: string; a: string }[] {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, 12).map((x) => ({
    q: String(x?.q ?? "").slice(0, 300),
    a: String(x?.a ?? "").slice(0, 400),
  }));
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const b = await req.json();
    if (b.website) return new Response(JSON.stringify({ ok: true }), { headers: cors }); // honeypot

    const blocked = await guard(req, { bucket: "vault", limit: 25, windowSecs: 300, turnstileToken: b.turnstile });
    if (blocked) return blocked;

    const slug = String(b.slug ?? "").slice(0, 80);
    const qa = clampQA(b.qa);
    const action = b.action === "final" ? "final" : "next";
    if (!slug) throw new Error("missing product");

    const supa = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SERVICE_ROLE_KEY")!);
    const { data: p } = await supa.from("vault_products")
      .select("*").eq("slug", slug).eq("active", true).single();
    if (!p) throw new Error("unknown product");

    const context =
      `PRODUCT: ${p.title} — ${p.tagline ?? ""}\nCONFIG GOALS: ${JSON.stringify(p.config_schema)}\n` +
      `Q&A SO FAR:\n${qa.map((x) => `Q: ${x.q}\nA: ${x.a}`).join("\n") || "(none yet)"}`;

    if (action === "next") {
      const raw = await ai(NEXT_SYS, context, 300);
      const m = raw.match(/\{[\s\S]*\}/);
      const j = m ? JSON.parse(m[0]) : { done: true };
      if (qa.length >= 12) j.done = true;
      return new Response(JSON.stringify(j), { headers: { ...cors, "content-type": "application/json" } });
    }

    // final
    const guide = await ai(FINAL_SYS, context, 900);
    const email = String(b.email ?? "").trim().slice(0, 160);
    const validEmail = /.+@.+\..+/.test(email) ? email : null;

    await supa.from("vault_sessions").insert({
      owner: p.owner, product_id: p.id, qa, guide, email: validEmail,
    });

    // The no-outreach pipeline: a configurator run with an email IS an inbound lead.
    if (validEmail) {
      const bizAns = qa.find((x) => /business|company|name/i.test(x.q))?.a;
      await supa.from("leads").insert({
        owner: p.owner,
        biz: (bizAns || validEmail).slice(0, 120),
        email: validEmail,
        pain: `configured "${p.title}" in the Vault`,
        source: "vault",
        notes: `Vault session — ${p.slug}. They customised the template; warm inbound.`,
      });
    }

    return new Response(JSON.stringify({ ok: true, guide }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e) }),
      { status: 400, headers: { ...cors, "content-type": "application/json" } });
  }
});
