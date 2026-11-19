/* Управление ключами владельца. В списке храним только метаданные;
   значение только что выпущенного ключа живёт в открытом диалоге. */
const ownerAccess = (() => {
  let rows = [], listState = "", notice = "", revokeError = "", logoutError = "";
  let selected = null, generation = 0, listRequest = 0, creating = false, loggingOut = false, revoking = false;

  function date(value) {
    return new Intl.DateTimeFormat(currentLang(), {dateStyle:"medium", timeStyle:"short"}).format(new Date(value));
  }
  function render() {
    setText("key-list-status", listState ? t(listState) : "");
    setText("key-notice", notice ? t(notice) : "");
    setText("logout-error", logoutError ? t(logoutError) : "");
    setText("revoke-error", revokeError ? t(revokeError) : "");
    setText("revoke-description", selected ? t("keys.revokeHint", {name:selected.name, id:selected.id}) : "");
    $("key-retry").hidden = listState !== "keys.listFailed";
    $("key-create").disabled = creating || loggingOut;
    $("owner-logout").disabled = loggingOut;
    $("revoke-confirm").disabled = revoking;
    $("key-notice").classList.toggle("error", notice === "keys.createFailed" || notice === "keys.revokeFailed");
    $("key-list").replaceChildren(...rows.map(row => {
      const li = el("li"), top = el("div", null, "titleline"), name = el("strong", row.name);
      top.append(name); li.append(top, code("ID: " + row.id), el("p", t("keys.created", {date:date(row.created_at)}), "help"));
      if (row.revoked_at) li.append(el("p", t("keys.revoked", {date:date(row.revoked_at)}), "help"));
      else {
        const button = el("button", t("keys.revoke"), "soft"); button.type = "button";
        button.setAttribute("aria-label", t("keys.revokeNamed", {name:row.name}));
        button.onclick = () => {
          selected = row; revokeError = ""; setText("revoke-error", "");
          setText("revoke-description", t("keys.revokeHint", {name:row.name, id:row.id}));
          $("revoke-dialog").showModal();
        };
        top.append(button);
      }
      return li;
    }));
  }
  async function load() {
    const request = ++listRequest;
    listState = "state.loading"; rows = []; render();
    try {
      const result = await api("/api/keys");
      if (request !== listRequest) return;
      rows = result.keys.map(({id,name,created_at,revoked_at}) => ({id,name,created_at,revoked_at}));
      listState = rows.length ? "" : "keys.empty";
    } catch { if (request !== listRequest) return; rows = []; listState = "keys.listFailed"; }
    render();
  }
  function erase() {
    setText("key-secret", ""); setText("key-copy-status", "");
    $("secret-dialog").querySelector(".manual")?.remove();
  }
  let sessionGeneration = 0;
  function clear() { ++sessionGeneration; changeSection(); }
  function changeSection() {
    ++generation; ++listRequest;
    erase(); $("secret-dialog").close(); $("revoke-dialog").close(); selected = null;
    notice = ""; revokeError = "";
  }
  async function create(event) {
    event.preventDefault(); if (creating || loggingOut) return;
    const input = $("key-name"), name = input.value.trim();
    if (!name) { input.value = ""; input.reportValidity(); return; }
    const current = sessionGeneration; creating = true; notice = ""; render();
    try {
      const result = await api("/api/keys", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name})});
      if (current !== sessionGeneration || state.leaving) return;
      if (typeof result.key !== "string" || !result.key) throw new Error();
      input.value = ""; setText("key-secret", result.key); $("secret-dialog").showModal();
    } catch { if (current === sessionGeneration) notice = "keys.createFailed"; }
    finally {
      creating = false; render();
      if (page === "api" && !state.leaving && !loggingOut) load();
    }
  }
  async function revoke() {
    if (!selected || revoking) return;
    const current = generation, row = selected;
    revoking = true; revokeError = ""; $("revoke-confirm").disabled = true; setText("revoke-error", "");
    setText("revoke-cancel", t("keys.close"));
    try {
      await api("/api/keys/" + encodeURIComponent(row.id) + "/revoke", {method:"POST"});
      if (current !== generation || state.leaving) return;
      const stillOpen = $("revoke-dialog").open && selected === row;
      if (stillOpen) $("revoke-dialog").close();
      notice = "keys.revokedNotice"; await load();
      if (stillOpen && current === generation) $("key-name").focus();
    } catch {
      if (current !== generation) return;
      if ($("revoke-dialog").open && selected === row) { revokeError = "keys.revokeFailed"; setText("revoke-error", t(revokeError)); }
      else { notice = "keys.revokeFailed"; render(); }
    } finally { revoking = false; $("revoke-confirm").disabled = false; setText("revoke-cancel", t("keys.cancel")); }
  }
  async function logout() {
    if (loggingOut) return;
    loggingOut = true; clear(); logoutError = ""; render();
    try { await api("/auth/logout", {method:"POST"}); returnToLogin(); }
    catch { if (!state.leaving) { loggingOut = false; logoutError = "auth.logoutFailed"; render(); if (page === "api") load(); } }
  }
  function bind() {
    $("owner-keys").hidden = false; $("owner-logout").hidden = false;
    $("link-docs").dataset.i18n = "auth.docs"; $("address-help").dataset.i18n = "auth.addressHelp";
    $("example-key").checked = true; $("example-key").disabled = true;
    $("example-key-option").hidden = true; $("owner-example-hint").hidden = false;
    $("key-form").onsubmit = create; $("key-retry").onclick = load; $("owner-logout").onclick = logout;
    const secret = $("secret-dialog"), confirm = $("revoke-dialog");
    $("key-dismiss").onclick = () => { erase(); secret.close(); };
    secret.addEventListener("cancel", erase);
    secret.addEventListener("close", () => { if (!secret.open) { erase(); if (page === "api" && !state.leaving) $("key-name").focus(); } });
    $("revoke-cancel").onclick = () => confirm.close(); $("revoke-confirm").onclick = revoke;
    confirm.addEventListener("close", () => { if (!confirm.open) selected = null; });
    let lastActivity = -Infinity;
    const activity = event => {
      const now = performance.now();
      if (!event.isTrusted || document.hidden || state.leaving || loggingOut || now - lastActivity < 60000) return;
      lastActivity = now;
      api("/auth/activity", {method:"POST"}).catch(() => {});
    };
    for (const event of ["pointerdown", "keydown", "input"]) document.addEventListener(event, activity, {capture:true, passive:true});
    window.addEventListener("pagehide", clear);
    window.addEventListener("pageshow", e => { if (e.persisted) location.reload(); });
  }
  return {bind, clear, changeSection, load, render};
})();
