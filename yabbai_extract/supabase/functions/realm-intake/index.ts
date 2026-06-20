// realm-intake — the END-USER door. Public by necessity (it lives on client websites),
// guarded by widget key + honeypot + hard caps. Every enquiry:
//   1. lands as a row the client and Director both see,
//   2. gets an instant, carefully-scoped AI reply (end user benefits NOW),
//   3. auto-writes the Mutual Ledger (leads_generated / bookings) — the ecosystem
//      starts feeding itself with real value data, no manual logging.
// Deploy: supabase functions deploy realm-intake --no-verify-jwt
import { createClient } from "npm:@supabase/supabase-js@2";
import { guard } from "../_shared/ratelimit.ts";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
};

const REPLY_SYS = (biz: string) => `You are the instant assistant for "${biz}", an Australian small business. Reply to the customer's enquiry in 2-3 warm, plain sentences, Australian tone. Confirm a real person will follow up shortly. If they asked about booking, tell them the booking button below will sort them out. HARD RULES: never quote prices, never make promises or guarantees, never give medical, legal, or financial advice — for anything like that, say the team will cover it when they call back. No emojis.`;

const CANNED = "Thanks for reaching out — your message has landed and a real person will get back to you shortly. If you'd like to book, the button below will sort you out.";

async function aiReply(biz: string, msg: string): Promise<string> {
  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) return CANNED;
  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify({
        model: Deno.env.get("ANTHROPIC_MODEL") ?? "claude-sonnet-4-6",
        max_tokens: 220,
        system: REPLY_SYS(biz),
        messages: [{ role: "user", content: msg.slice(0, 1000) }],
      }),
    });
    if (!r.ok) return CANNED;
    const d = await r.json();
    const text = (d.content ?? []).map((b: { text?: string }) => b.text ?? "").join("").trim();
    return text || CANNED;
  } catch { return CANNED; }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const b = await req.json();
    // Honeypot: real humans never fill the hidden "website" field.
    if (b.website) return new Response(JSON.stringify({ ok: true }), { headers: cors });

    const key = String(b.key ?? "").slice(0, 64);
    const message = String(b.message ?? "").trim().slice(0, 1000);
    const name = String(b.name ?? "").trim().slice(0, 120);
    const contact = String(b.contact ?? "").trim().slice(0, 160);
    const want_booking = !!b.want_booking;
    if (!key || message.length < 3) throw new Error("missing fields");

    const supa = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SERVICE_ROLE_KEY")!);
    const { data: w } = await supa.from("widgets").select("*").eq("key", key).eq("active", true).single();
    if (!w) throw new Error("unknown widget");

    const reply = await aiReply(w.biz, message);

    await supa.from("enquiries").insert({
      widget_id: w.id, director: w.director, client_user: w.client_user,
      name, contact, message, ai_reply: reply, want_booking,
    });

    // The self-feeding ledger: value is written the moment it happens.
    const events = [{
      director: w.director, client_user: w.client_user, order_id: null,
      kind: "leads_generated", amount: 1, note: `widget enquiry — ${w.biz}`,
    }];
    if (want_booking) events.push({
      director: w.director, client_user: w.client_user, order_id: null,
      kind: "bookings", amount: 1, note: `booking intent — ${w.biz}`,
    });
    await supa.from("value_events").insert(events);

    return new Response(JSON.stringify({ ok: true, reply, booking_url: want_booking ? (w.booking_url ?? null) : null }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e) }),
      { status: 400, headers: { ...cors, "content-type": "application/json" } });
  }
});
