// stripe-webhook — the only automated path that creates a client, and it requires
// REAL MONEY: a completed Stripe Checkout. Failed invoices flag churn risk.
// Deploy with JWT off (Stripe can't sign in):
//   supabase functions deploy stripe-webhook --no-verify-jwt
// Secrets: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, SERVICE_ROLE_KEY, OWNER_USER_ID
import Stripe from "npm:stripe@17";
import { createClient } from "npm:@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
  apiVersion: "2024-06-20",
});
const cryptoProvider = Stripe.createSubtleCryptoProvider();

// Map your live AUD Stripe Price IDs here after creating products.
const PRICE_MAP: Record<string, { package?: string; fee?: number; tier?: string; mrr?: number }> = {
  // "price_xxx_starter":  { package: "Starter",  fee: 1500 },
  // "price_xxx_growth":   { package: "Growth",   fee: 4000 },
  // "price_xxx_fullops":  { package: "Full Ops", fee: 8000 },
  // "price_xxx_maintain": { tier: "Maintain", mrr: 500 },
  // "price_xxx_optimize": { tier: "Optimize", mrr: 1500 },
  // "price_xxx_scale":    { tier: "Scale",    mrr: 3500 },
};

Deno.serve(async (req) => {
  const sig = req.headers.get("stripe-signature");
  const body = await req.text();
  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(
      body, sig!, Deno.env.get("STRIPE_WEBHOOK_SECRET")!, undefined, cryptoProvider);
  } catch (e) {
    return new Response(`Webhook signature verification failed: ${e}`, { status: 400 });
  }

  // Service role is required here (no user session). RLS is bypassed BY DESIGN for
  // this single, signature-verified entry point; owner is pinned to you.
  const supa = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SERVICE_ROLE_KEY")!,
  );
  const owner = Deno.env.get("OWNER_USER_ID")!;

  if (event.type === "checkout.session.completed") {
    const s = event.data.object as Stripe.Checkout.Session;
    const items = await stripe.checkout.sessions.listLineItems(s.id, { limit: 10 });

    let pkg: string | null = null, fee = 0, tier: string | null = null, mrr = 0;
    for (const li of items.data) {
      const m = PRICE_MAP[li.price?.id ?? ""];
      if (!m) continue;
      if (m.package) { pkg = m.package; fee = m.fee ?? 0; }
      if (m.tier) { tier = m.tier; mrr = m.mrr ?? 0; }
    }

    await supa.from("clients").insert({
      owner,
      biz: s.customer_details?.name ?? s.customer_details?.email ?? "New client",
      package: pkg, setup_fee: fee, setup_paid: fee > 0,
      tier, mrr,
      stripe_customer_id: typeof s.customer === "string" ? s.customer : s.customer?.id ?? null,
      created_via: "stripe_checkout",
    });
    await supa.from("events").insert({
      owner, agent: "STRIPE",
      message: `payment received: ${pkg ?? "subscription"} ${fee ? "$" + fee : ""}${tier ? " + " + tier + " $" + mrr + "/mo" : ""} — client created, onboarding fired`,
    });
  }

  if (event.type === "invoice.payment_failed") {
    const inv = event.data.object as Stripe.Invoice;
    const custId = typeof inv.customer === "string" ? inv.customer : inv.customer?.id;
    if (custId) {
      const { data: c } = await supa.from("clients")
        .select("id,biz").eq("stripe_customer_id", custId).maybeSingle();
      if (c) {
        await supa.from("clients").update({ health: "risk" }).eq("id", c.id);
        await supa.from("approvals").insert({
          owner, type: "save",
          title: `Churn risk → ${c.biz}`,
          detail: "Stripe invoice payment failed. Save play: payment update link + check-in.",
          agent: "STRIPE", payload: { client_id: c.id },
        });
      }
    }
  }

  return new Response(JSON.stringify({ received: true }),
    { headers: { "content-type": "application/json" } });
});
