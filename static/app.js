document.addEventListener("DOMContentLoaded", () => {
  const add = document.getElementById("add-requirement");
  if (add)
    add.addEventListener("click", () => {
      const total = document.getElementById("id_requirements-TOTAL_FORMS");
      const html = document
        .getElementById("requirement-template")
        .innerHTML.replaceAll("__prefix__", total.value);
      document
        .getElementById("requirements")
        .insertAdjacentHTML("beforeend", html);
      total.value = Number(total.value) + 1;
    });
  const dirty = new Set();
  document
    .querySelectorAll(".dirty-form")
    .forEach((f) => f.addEventListener("input", () => dirty.add(f)));
  window.addEventListener("beforeunload", (e) => {
    if (dirty.size) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  document.querySelectorAll("form").forEach((f) => {
    f.addEventListener("submit", (e) => {
      if (!f.checkValidity()) return;
      const b = e.submitter;
      if (b?.dataset.confirm && !confirm(b.dataset.confirm)) {
        e.preventDefault();
        return;
      }
      dirty.clear();
      if (b) {
        b.classList.add("busy");
        b.setAttribute("aria-busy", "true");
        setTimeout(() => {
          b.disabled = true;
        }, 0);
      }
    });
    // Browsers restore forms after Back; restore submission controls too.
  });
  window.addEventListener("pageshow", () =>
    document.querySelectorAll(".busy").forEach((b) => {
      b.disabled = false;
      b.classList.remove("busy");
      b.removeAttribute("aria-busy");
    }),
  );
  const err = document.querySelector(".errorlist");
  if (err) {
    err.setAttribute("role", "alert");
    const input = err.parentElement.querySelector(
      "input:not([type=hidden]),select,textarea",
    );
    input?.focus();
  }
});
