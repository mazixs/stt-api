(() => {
  const { t, applyStaticTexts, currentLang, setLang } = window.i18n;
  const form = document.getElementById("login-form"), submit = document.getElementById("login-submit");
  const password = form.elements.password, feedback = document.getElementById("login-error");
  let error = "", pending = false;
  try { localStorage.removeItem("stt_console_key"); } catch { /* Хранилище отключено. */ }

  function render() {
    applyStaticTexts();
    document.title = t("auth.title") + " — " + t("brand.name");
    document.querySelectorAll(".lang-option").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.lang === currentLang())));
    feedback.textContent = error ? t(error) : "";
    submit.disabled = pending;
    submit.textContent = t(pending ? "auth.signingIn" : "auth.signIn");
  }
  document.querySelectorAll(".lang-option").forEach(b => b.onclick = () => { setLang(b.dataset.lang); render(); });
  window.addEventListener("pagehide", () => { password.value = ""; });
  window.addEventListener("pageshow", e => { if (e.persisted) location.reload(); });
  form.onsubmit = async event => {
    event.preventDefault(); if (pending) return;
    pending = true; error = ""; render();
    try {
      const r = await fetch("/auth/login", {method:"POST", mode:"same-origin", credentials:"same-origin", redirect:"error", cache:"no-store",
        headers:{"Content-Type":"application/json", "X-Kamerton-Login":"1"},
        body:JSON.stringify({username:form.elements.username.value, password:password.value})});
      if (!r.ok) error = r.status === 401 ? "auth.denied" : r.status === 429 ? "auth.limited" : "auth.unavailable";
      else {
        const session = await r.json();
        if (session.mode === "owner" && session.authenticated === true && session.csrf_token) {
          password.value = ""; location.replace("/"); return;
        }
        error = "auth.unavailable";
      }
    } catch { error = "auth.unavailable"; }
    pending = false; render();
    if (error === "auth.denied") { password.value = ""; password.focus(); }
  };
  render();
})();
