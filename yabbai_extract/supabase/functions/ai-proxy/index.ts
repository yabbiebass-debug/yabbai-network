// ai-proxy — server-side AI router. Anthropic primary, Ollama fallback.
// Keys live in Supabase secrets. The browser never sees them.
// Deploy: supabase functions deploy ai-proxy
import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

async function callAnthropic(system: string, prompt: string): Promise<string> {
  const key = Deno.env.get("ANTHROPIC_API_KEY");
  if (!key) throw new Error("ANTHROPIC_API_KEY not set");
  const model = Deno.env.get("ANTHROPIC_MODEL") ?? "claude-sonnet-4-6";
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": key,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      system,
      messages: [{ role: "user", content: prompt }],
    }),
  });
  if (!r.ok) throw new Error(`Anthropic ${r.status}: ${await r.text()}`);
  const data = await r.json();
  return (data.content ?? []).map((b: { text?: string }) => b.text ?? "").join("");
}

async function callOpenRouter(system: string, prompt: string): Promise<string> {
  const key = Deno.env.get("OPENROUTER_API_KEY");
  if (!key) throw new Error("OPENROUTER_API_KEY not set");
  const model = Deno.env.get("OPENROUTER_MODEL") ?? "anthropic/claude-sonnet-4.6";
  const r = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${key}`,
      "content-type": "application/json",
      "HTTP-Referer": Deno.env.get("OPENROUTER_REFERER") ?? "https://yabbai.network",
      "X-Title": "YABBAI Realm",
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      messages: [
        { role: "system", content: system },
        { role: "user", content: prompt },
      ],
    }),
  });
  if (!r.ok) throw new Error(`OpenRouter ${r.status}: ${await r.text()}`);
  const data = await r.json();
  return data.choices?.[0]?.message?.content ?? "";
}

async function callOllama(system: string, prompt: string): Promise<string> {
  const url = Deno.env.get("OLLAMA_URL"); // e.g. http://pop-os:11434 over Tailscale
  if (!url) throw new Error("OLLAMA_URL not set");
  const r = await fetch(`${url}/api/generate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      model: Deno.env.get("OLLAMA_MODEL") ?? "llama3.1:8b",
      system,
      prompt,
      stream: false,
    }),
  });
  if (!r.ok) throw new Error(`Ollama ${r.status}`);
  const data = await r.json();
  return data.response ?? "";
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    // Require a signed-in user — this proxy is not public.
    const supa = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_ANON_KEY")!,
      { global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } } },
    );
    const { data: { user } } = await supa.auth.getUser();
    if (!user) return new Response(JSON.stringify({ error: "auth required" }),
      { status: 401, headers: { ...cors, "content-type": "application/json" } });

    const { system = "", prompt = "" } = await req.json();
    // Routing order is configurable via AI_ROUTE (comma list), default: anthropic,openrouter,ollama.
    // Each tier is tried until one succeeds — a self-healing clan brain.
    const route = (Deno.env.get("AI_ROUTE") ?? "anthropic,openrouter,ollama")
      .split(",").map((s) => s.trim()).filter(Boolean);
    const callers: Record<string, (s: string, p: string) => Promise<string>> = {
      anthropic: callAnthropic,
      openrouter: callOpenRouter,
      ollama: callOllama,
    };
    let text = "", engine = "", lastErr = "";
    for (const tier of route) {
      const fn = callers[tier];
      if (!fn) continue;
      try {
        text = await fn(system, prompt);
        engine = tier;
        break;
      } catch (e) {
        lastErr = String(e);
      }
    }
    if (!engine) throw new Error(`all routes failed: ${lastErr}`);
    return new Response(JSON.stringify({ engine, text }),
      { headers: { ...cors, "content-type": "application/json" } });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }),
      { status: 500, headers: { ...cors, "content-type": "application/json" } });
  }
});
