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

// Parameter buttons submit through the regular CSRF-protected server form.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-parameter]");
  if (button)
    document.getElementById("report-parameter").value =
      button.dataset.parameter;
});
document.addEventListener("DOMContentLoaded", () => {
  const add = document.getElementById("add-custom");
  if (add)
    add.addEventListener("click", () => {
      const total = document.getElementById("id_custom-TOTAL_FORMS");
      document
        .getElementById("custom-requirements")
        .insertAdjacentHTML(
          "beforeend",
          document
            .getElementById("custom-template")
            .innerHTML.replaceAll("__prefix__", total.value),
        );
      total.value = Number(total.value) + 1;
    });
});
document.addEventListener("DOMContentLoaded", () => {
  const select = document.querySelector("#evidence-form #id_characteristic");
  if (!select) return;
  const sync = () => {
    const option = select.selectedOptions[0],
      kind = option?.dataset.kind;
    for (const [id, type] of [
      ["value", "number"],
      ["boolean", "boolean"],
      ["text", "text"],
    ]) {
      const input = document.querySelector("#evidence-form #id_" + id);
      if (input) {
        input.disabled = kind !== type;
        input.closest(".field").hidden = kind !== type;
      }
    }
    const input = document.querySelector("#evidence-form #id_value");
    if (input) {
      const label = input.closest(".field").querySelector("label");
      label.textContent =
        "Значение" + (option?.dataset.unit ? ", " + option.dataset.unit : "");
    }
  };
  select.addEventListener("change", sync);
  sync();
});
