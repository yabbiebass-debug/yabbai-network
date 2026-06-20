// _shared/ratelimit.ts — drop-in guard for the public AI doors.
// Import in realm-intake / diagnose / vault-config and call guard() first.
// Two layers: (1) per-IP sliding-window via the rate_check DB function,
// (2) optional Cloudflare Turnstile verification if TURNSTILE_SECRET is set.
import { createClient, SupabaseClient } from "npm:@supabase/supabase-js@2";

export function clientIP(req: Request): string {
  // Supabase/Cloudflare forward the real client IP here.
  return (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim()
    || req.headers.get("cf-connecting-ip")
    || "0.0.0.0";
}

// Returns null if allowed; a Response (429/403) if the caller should be blocked.
export async function guard(
  req: Request,
  opts: { bucket: string; limit: number; windowSecs: number; turnstileToken?: string },
): Promise<Response | null> {
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "content-type" };
  const supa: SupabaseClient = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SERVICE_ROLE_KEY")!,
  );
  const ip = clientIP(req);

  // (1) Optional Turnstile — only enforced if a secret is configured.
  const tsSecret = Deno.env.get("TURNSTILE_SECRET");
  if (tsSecret) {
    const token = opts.turnstileToken ?? "";
    if (!token) {
      return new Response(JSON.stringify({ ok: false, error: "verification required" }),
        { status: 403, headers: { ...cors, "content-type": "application/json" } });
    }
    try {
      const v = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ secret: tsSecret, response: token, remoteip: ip }),
      });
      const vj = await v.json();
      if (!vj.success) {
        return new Response(JSON.stringify({ ok: false, error: "verification failed" }),
          { status: 403, headers: { ...cors, "content-type": "application/json" } });
      }
    } catch {
      // If Turnstile itself errors, fall through to rate limiting rather than hard-fail.
    }
  }

  // (2) Per-IP sliding window.
  const { data: allowed } = await supa.rpc("rate_check", {
    p_bucket: opts.bucket, p_ip: ip, p_limit: opts.limit, p_window_secs: opts.windowSecs,
  });
  if (allowed === false) {
    return new Response(JSON.stringify({ ok: false, error: "rate limit — slow down and try again shortly" }),
      { status: 429, headers: { ...cors, "content-type": "application/json", "Retry-After": "60" } });
  }
  return null; // allowed
}
