/* yabbai-connect.js — the end-user layer. Drop-in widget for any client website.
   Embed:
   <script src="https://YOUR-SITE/yabbai-connect.js"
           data-key="yw_xxx"
           data-endpoint="https://YOUR_REF.supabase.co/functions/v1/realm-intake"
           data-biz="Box Hill Physio"
           data-accent="#9945FF" defer></script>
   No dependencies. No tracking. One POST, one instant reply.                    */
(function () {
  var s = document.currentScript;
  var KEY = s.getAttribute("data-key") || "";
  var EP = s.getAttribute("data-endpoint") || "";
  var BIZ = s.getAttribute("data-biz") || "Send us a message";
  var ACC = s.getAttribute("data-accent") || "#9945FF";
  if (!KEY || !EP) return console.warn("[yabbai] widget missing data-key/data-endpoint");

  var css = document.createElement("style");
  css.textContent =
    ".yw-btn{position:fixed;right:18px;bottom:18px;z-index:99998;width:56px;height:56px;border-radius:50%;" +
    "border:none;cursor:pointer;background:" + ACC + ";color:#fff;font-size:24px;line-height:56px;" +
    "box-shadow:0 6px 24px rgba(0,0,0,.28);transition:transform .15s}" +
    ".yw-btn:hover{transform:scale(1.06)}" +
    ".yw-panel{position:fixed;right:18px;bottom:84px;z-index:99999;width:min(340px,calc(100vw - 36px));" +
    "background:#fff;color:#15182b;border-radius:12px;box-shadow:0 12px 48px rgba(0,0,0,.30);" +
    "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;overflow:hidden;display:none}" +
    ".yw-panel.on{display:block}" +
    ".yw-head{background:" + ACC + ";color:#fff;padding:14px 16px;font-weight:700}" +
    ".yw-head small{display:block;font-weight:400;opacity:.85;font-size:11px;margin-top:2px}" +
    ".yw-body{padding:14px 16px}" +
    ".yw-body input,.yw-body textarea{width:100%;box-sizing:border-box;border:1px solid #d7dae6;border-radius:8px;" +
    "padding:9px 11px;margin-bottom:8px;font:inherit;font-size:13px}" +
    ".yw-body textarea{min-height:74px;resize:vertical}" +
    ".yw-check{display:flex;gap:8px;align-items:center;font-size:12.5px;color:#454a63;margin:2px 0 10px}" +
    ".yw-send{width:100%;border:none;border-radius:8px;padding:11px;font-weight:700;font-size:13px;" +
    "cursor:pointer;background:" + ACC + ";color:#fff}" +
    ".yw-send[disabled]{opacity:.6;cursor:wait}" +
    ".yw-reply{background:#f2f4fb;border-radius:10px;padding:12px 13px;font-size:13px;line-height:1.55;color:#23273f}" +
    ".yw-book{display:inline-block;margin-top:10px;padding:10px 14px;border-radius:8px;background:#14F195;" +
    "color:#04221a;font-weight:700;font-size:13px;text-decoration:none}" +
    ".yw-foot{padding:8px 16px 12px;font-size:10px;color:#9aa0b8}" +
    ".yw-hp{position:absolute;left:-9999px;opacity:0;height:0;width:0}";
  document.head.appendChild(css);

  var btn = document.createElement("button");
  btn.className = "yw-btn"; btn.setAttribute("aria-label", "Open enquiry widget");
  btn.innerHTML = "&#9993;";
  var panel = document.createElement("div");
  panel.className = "yw-panel"; panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", BIZ);
  panel.innerHTML =
    '<div class="yw-head">' + BIZ + "<small>Usually replies in seconds</small></div>" +
    '<div class="yw-body">' +
    '<form class="yw-form">' +
    '<input class="yw-name" placeholder="Your name" autocomplete="name">' +
    '<input class="yw-contact" placeholder="Email or mobile" autocomplete="email">' +
    '<textarea class="yw-msg" placeholder="How can we help?" required></textarea>' +
    '<input class="yw-hp" name="website" tabindex="-1" autocomplete="off">' +
    '<label class="yw-check"><input type="checkbox" class="yw-book-chk"> I\u2019d like to book</label>' +
    '<button class="yw-send" type="submit">Send</button>' +
    "</form></div>" +
    '<div class="yw-foot">Instant reply by AI \u00b7 a real person follows up \u00b7 powered by YABBAI</div>';

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  btn.addEventListener("click", function () {
    panel.classList.toggle("on");
    if (panel.classList.contains("on")) panel.querySelector(".yw-msg").focus();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") panel.classList.remove("on");
  });

  panel.querySelector(".yw-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var send = panel.querySelector(".yw-send");
    var body = {
      key: KEY,
      name: panel.querySelector(".yw-name").value,
      contact: panel.querySelector(".yw-contact").value,
      message: panel.querySelector(".yw-msg").value,
      want_booking: panel.querySelector(".yw-book-chk").checked,
      website: panel.querySelector(".yw-hp").value, // honeypot
    };
    if (!body.message || body.message.trim().length < 3) return;
    send.disabled = true; send.textContent = "Sending\u2026";
    fetch(EP, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var bodyEl = panel.querySelector(".yw-body");
        var book = d.booking_url
          ? '<a class="yw-book" href="' + d.booking_url + '" target="_blank" rel="noopener">Book a time \u2192</a>'
          : "";
        bodyEl.innerHTML = '<div class="yw-reply">' +
          (d.reply ? d.reply.replace(/&/g,"&amp;").replace(/</g,"&lt;") :
            "Thanks \u2014 your message has landed. A real person will be in touch shortly.") +
          "</div>" + book;
      })
      .catch(function () {
        send.disabled = false; send.textContent = "Send";
        alert("Couldn't send just now \u2014 please try again in a moment.");
      });
  });
})();
