// Login page controller. External file (not inline) for strict CSP. Also handles the
// pre-auth language toggle: stores choice in the hearth_lang cookie so the app picks it
// up after login too (server falls back to this cookie when no user session exists).
(function () {
  function setLangCookie(lang) {
    document.cookie = "hearth_lang=" + lang + "; path=/; max-age=31536000; SameSite=Lax";
  }

  async function applyLang(lang) {
    if (lang) setLangCookie(lang);
    await I18N.load();
    I18N.apply();
    const sel = document.getElementById("loginLang");
    if (sel) sel.value = I18N.lang;
  }

  const form = document.getElementById("loginForm");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = document.getElementById("err");
      err.classList.add("hidden");
      const body = new URLSearchParams({
        username: form.username.value,
        password: form.password.value,
        totp: form.totp.value,
      });
      const r = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      const data = await r.json();
      if (r.ok) {
        // Persist a pre-auth language choice into the new account's preference.
        const m = document.cookie.match(/(?:^| )hearth_lang=([^;]+)/);
        if (m) { try { await fetch("/api/language", { method:"POST", credentials:"same-origin", headers:{ "X-CSRF-Token": getCookie("hearth_csrf") }, body: new URLSearchParams({ language: m[1] }) }); } catch(e){} }
        location.href = data.redirect || "/chat"; return;
      }
      if (data.need_totp) document.getElementById("totpRow").classList.remove("hidden");
      err.textContent = data.error || I18N.t("login.failed");
      err.classList.remove("hidden");
    });
  }

  const sel = document.getElementById("loginLang");
  if (sel) sel.addEventListener("change", (e) => applyLang(e.target.value));

  // Initial render in the stored/default language.
  applyLang();
})();
