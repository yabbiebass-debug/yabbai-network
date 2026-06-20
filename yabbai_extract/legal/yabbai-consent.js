/* yabbai-consent.js — drop-in legal consent gate for any YABBAI.NETWORK surface.
   After a user signs in, call YabbaiConsent.gate(supa) BEFORE showing the app.
   It checks `my_pending_policies`; if any are outstanding it renders a blocking
   modal listing each policy (viewable in full) with a single "I agree" action,
   then records signed acceptance via accept_policies() with IP + user-agent.
   Returns a promise that resolves once all active policies are accepted.

   Usage in a surface:
     import './yabbai-consent.js'  (or inline this)
     const session = await ensureAuth();
     await YabbaiConsent.gate(supa, { context: 'signup' });
     // ...then load the app
*/
(function () {
  const css = `
  .yc-bg{position:fixed;inset:0;background:rgba(2,8,20,.92);z-index:99990;display:flex;
    align-items:center;justify-content:center;padding:20px;font-family:'JetBrains Mono',monospace}
  .yc-modal{background:#081226;border:1px solid rgba(153,69,255,.3);border-radius:8px;
    max-width:560px;width:100%;max-height:88vh;display:flex;flex-direction:column;color:#EEF1FB}
  .yc-head{padding:22px 24px 14px}
  .yc-head h2{font-family:'Unbounded',sans-serif;font-size:16px;font-weight:800}
  .yc-head h2 b{color:#9945FF}
  .yc-head p{font-size:11.5px;color:#9AA3C0;margin-top:8px;line-height:1.6}
  .yc-list{padding:0 24px;overflow-y:auto;flex:1}
  .yc-doc{border:1px solid rgba(153,69,255,.18);border-radius:5px;margin-bottom:10px;overflow:hidden}
  .yc-doctop{display:flex;align-items:center;gap:10px;padding:12px 14px;cursor:pointer}
  .yc-doctop b{font-size:12.5px;flex:1}
  .yc-doctop .v{font-size:9px;color:#5e6688;letter-spacing:1px}
  .yc-doctop .x{color:#9945FF;font-size:11px}
  .yc-body{display:none;padding:0 14px 14px;font-size:11px;color:#9AA3C0;line-height:1.7;
    white-space:pre-wrap;max-height:240px;overflow-y:auto;border-top:1px solid rgba(153,69,255,.12)}
  .yc-body.on{display:block}
  .yc-foot{padding:16px 24px 22px;border-top:1px solid rgba(153,69,255,.18)}
  .yc-check{display:flex;gap:10px;align-items:flex-start;font-size:12px;color:#EEF1FB;margin-bottom:14px;cursor:pointer}
  .yc-check input{margin-top:3px}
  .yc-check a{color:#14F195}
  .yc-btn{width:100%;padding:13px;border:none;border-radius:4px;background:#9945FF;color:#fff;
    font-family:'JetBrains Mono',monospace;font-weight:700;font-size:11.5px;letter-spacing:1.5px;
    text-transform:uppercase;cursor:pointer}
  .yc-btn[disabled]{opacity:.45;cursor:not-allowed}
  .yc-err{color:#FF4D6D;font-size:11px;margin-top:10px;min-height:14px}`;

  function injectCSS() {
    if (document.getElementById("yc-css")) return;
    const s = document.createElement("style");
    s.id = "yc-css"; s.textContent = css; document.head.appendChild(s);
  }

  async function getIP() {
    // Best-effort, non-blocking. Privacy-safe public IP only; ignore failure.
    try {
      const r = await fetch("https://api.ipify.org?format=json", { cache: "no-store" });
      const j = await r.json(); return j.ip || null;
    } catch { return null; }
  }

  async function gate(supa, opts = {}) {
    const context = opts.context || "signup";
    // What does this user still need to accept?
    const { data: pending, error } = await supa.from("my_pending_policies").select("*");
    if (error) { console.warn("[consent] pending check failed", error); return; }
    if (!pending || pending.length === 0) return; // all good

    // Fetch full texts for the pending docs
    const slugs = pending.map((p) => p.slug);
    const { data: docs } = await supa.from("legal_documents")
      .select("slug,version,title,body").in("slug", slugs).eq("active", true);
    const byslug = {}; (docs || []).forEach((d) => { byslug[d.slug] = d; });

    injectCSS();
    return new Promise((resolve) => {
      const bg = document.createElement("div");
      bg.className = "yc-bg";
      bg.innerHTML = `
        <div class="yc-modal" role="dialog" aria-modal="true" aria-label="Agreements">
          <div class="yc-head">
            <h2>YABBAI<b>.</b>NETWORK — Agreements</h2>
            <p>Before you continue, please review and accept the agreements below. Tap each to read it in full. Your acceptance is recorded with a timestamp.</p>
          </div>
          <div class="yc-list">
            ${pending.map((p) => {
              const d = byslug[p.slug] || { title: p.title, version: p.version, body: "" };
              return `<div class="yc-doc">
                <div class="yc-doctop" data-slug="${p.slug}">
                  <b>${d.title}</b><span class="v">v${d.version}</span><span class="x">read ▾</span>
                </div>
                <div class="yc-body" id="yc-body-${p.slug}">${escapeHtml(d.body)}</div>
              </div>`;
            }).join("")}
          </div>
          <div class="yc-foot">
            <label class="yc-check">
              <input type="checkbox" id="yc-agree">
              <span>I have read and agree to all of the agreements above, and I confirm I am at least 18 and authorised to accept them.</span>
            </label>
            <button class="yc-btn" id="yc-go" disabled>Agree &amp; continue</button>
            <div class="yc-err" id="yc-err"></div>
          </div>
        </div>`;
      document.body.appendChild(bg);

      bg.querySelectorAll(".yc-doctop").forEach((t) => {
        t.addEventListener("click", () => {
          const b = bg.querySelector("#yc-body-" + t.dataset.slug);
          b.classList.toggle("on");
          t.querySelector(".x").textContent = b.classList.contains("on") ? "hide ▴" : "read ▾";
        });
      });

      const chk = bg.querySelector("#yc-agree");
      const go = bg.querySelector("#yc-go");
      chk.addEventListener("change", () => { go.disabled = !chk.checked; });

      go.addEventListener("click", async () => {
        go.disabled = true; go.textContent = "Recording…";
        const ip = await getIP();
        const { error: e2 } = await supa.rpc("accept_policies", {
          p_slugs: slugs, p_context: context, p_ip: ip, p_ua: navigator.userAgent.slice(0, 300),
        });
        if (e2) {
          bg.querySelector("#yc-err").textContent = e2.message || "Could not record acceptance — try again.";
          go.disabled = false; go.textContent = "Agree & continue";
          return;
        }
        bg.remove();
        resolve(true);
      });
    });
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  window.YabbaiConsent = { gate };
})();
