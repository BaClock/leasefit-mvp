document.addEventListener("DOMContentLoaded", () => {
  const commercialType = document.getElementById("id_commercial_source_type");
  if (commercialType) {
    const fields = commercialType.form.elements;
    const status = fields.namedItem("commercial_status");
    const listingUrl = fields.namedItem("listing_url");
    const syncCommercialSource = () => {
      const listing = commercialType.value === "listing";
      for (const name of ["commercial_source", "commercial_date", "commercial_confirmation"]) {
        const field = fields.namedItem(name);
        field.closest(".field").hidden = listing;
        field.disabled = listing;
      }
      status.querySelector('option[value="confirmed"]').disabled = listing;
      if (listing && status.value === "confirmed") status.value = "unverified";
      listingUrl.required = listing;
    };
    commercialType.addEventListener("change", syncCommercialSource);
    syncCommercialSource();
  }

  for (const gallery of document.querySelectorAll("[data-listing-gallery]")) {
    const photos = [...gallery.querySelectorAll(".listing-photo")];
    const viewer = gallery.querySelector("dialog");
    const image = viewer.querySelector("img");
    const unavailable = viewer.querySelector("[data-listing-unavailable]");
    const count = viewer.querySelector("[data-listing-count]");
    let index = 0;

    for (const [position, link] of photos.entries()) {
      const thumb = link.querySelector("img");
      const showMissing = () => {
        thumb.hidden = true;
        link.querySelector("span").hidden = false;
      };
      thumb.addEventListener("error", showMissing);
      if (thumb.complete && !thumb.naturalWidth) showMissing();
      link.addEventListener("click", (event) => {
        if (!viewer.showModal || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        index = position;
        showPhoto();
        viewer.showModal();
      });
    }

    function showPhoto() {
      const link = photos[index];
      image.hidden = false;
      unavailable.hidden = true;
      image.alt = link.querySelector("img").alt;
      image.src = link.href;
      count.textContent = `${index + 1} из ${photos.length}`;
    }
    const move = (step) => {
      index = (index + step + photos.length) % photos.length;
      showPhoto();
    };
    for (const button of viewer.querySelectorAll("[data-listing-step]")) {
      button.addEventListener("click", () => move(Number(button.dataset.listingStep)));
    }
    viewer.querySelector("[data-listing-close]").addEventListener("click", () => viewer.close());
    viewer.addEventListener("keydown", (event) => {
      if (photos.length < 2 || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      move(event.key === "ArrowLeft" ? -1 : 1);
    });
    image.addEventListener("error", () => {
      image.hidden = true;
      unavailable.hidden = false;
    });
  }

  const reportType = document.getElementById("id_report_type");
  if (reportType) {
    const form = reportType.form;
    const preview = document.querySelector(".report-preview");
    const updateFormat = () => {
      form.dataset.reportType = reportType.value;
      for (const description of form.querySelectorAll("[data-report-description]")) {
        description.hidden = description.dataset.reportDescription !== reportType.value;
      }
      form.querySelector("[data-broker-report-options]").hidden =
        reportType.value !== "broker_detailed";
    };
    for (const input of [reportType, form.elements.conclusion, form.elements.comment]) {
      input.addEventListener("change", () => {
        updateFormat();
        if (preview) preview.hidden = true;
      });
    }
    updateFormat();
  }

  for (const picker of document.querySelectorAll("[data-district-picker]")) {
    const field = picker.querySelector("[data-district-field]");
    const unavailable = picker.querySelector("[data-district-unavailable]");
    const menu = picker.querySelector("details");
    const summary = menu.querySelector("summary");
    const tags = picker.querySelector("[data-district-tags]");
    const [any, ...districts] = field.querySelectorAll('input[type="checkbox"]');
    const city = picker.closest("form").elements.namedItem("city");
    const cityKey = (name) => name.trim().toLowerCase().replaceAll("ё", "е");

    const render = () => {
      const selected = districts.filter((option) => option.checked);
      any.checked = !selected.length;
      tags.replaceChildren();
      for (const choice of selected.length ? selected : [any]) {
        const label = choice.closest("label").dataset.districtName;
        const tag = document.createElement(choice === any ? "span" : "button");
        tag.className = "district-tag";
        tag.textContent = label;
        if (choice !== any) {
          tag.type = "button";
          tag.setAttribute("aria-label", "Убрать район " + label);
          const icon = document.createElement("span");
          icon.setAttribute("aria-hidden", "true");
          icon.textContent = "×";
          tag.append(icon);
          tag.addEventListener("click", () => {
            choice.checked = false;
            choice.dispatchEvent(new Event("input", { bubbles: true }));
            render();
            summary.focus();
          });
        }
        tags.append(tag);
      }
    };

    field.addEventListener("change", (event) => {
      if (event.target === any && any.checked) {
        for (const district of districts) district.checked = false;
      }
      render();
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        summary.focus();
      }
    });
    if (city) {
      const syncCity = () => {
        const supported = cityKey(city.value) === cityKey(picker.dataset.districtCity);
        field.hidden = field.disabled = !supported;
        unavailable.hidden = supported;
      };
      city.addEventListener("input", syncCity);
      syncCity();
    }
    render();
  }

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
  const form = select.form;
  const status = form.elements.namedItem("status");
  const confirmation = form.elements.namedItem("confirmation");
  const sourceType = form.elements.namedItem("source_type");
  const sourceUrl = form.elements.namedItem("source_url");
  const sync = () => {
    const option = select.selectedOptions[0],
      kind = option?.dataset.kind;
    const valueRequired = status.value !== "" && status.value !== "unknown";
    for (const [id, type] of [
      ["value", "number"],
      ["boolean", "boolean"],
      ["text", "text"],
    ]) {
      const input = form.elements.namedItem(id);
      const active = kind === type;
      input.disabled = !active;
      input.required = active && valueRequired;
      input.closest(".field").hidden = !active;
    }
    for (const field of form.querySelectorAll(".field")) {
      const inputs = [...field.querySelectorAll('[name^="observed_"]')].filter(
        (input) => input.name !== "observed_on",
      );
      if (!inputs.length) continue;
      const observed = "observed_" + option?.dataset.code;
      const active = inputs.some(
        (input) => input.name === observed || input.name.startsWith(observed + "_"),
      );
      field.hidden = !active;
      for (const input of inputs) input.disabled = !active;
    }
    const listing = sourceType.value === "listing";
    status.querySelector('option[value="confirmed"]').disabled = listing;
    if (listing && status.value === "confirmed") status.value = "unverified";
    for (const name of ["source", "locator", "confirmation", "observed_on"]) {
      const field = form.elements.namedItem(name);
      field.closest(".field").hidden = listing;
      field.disabled = listing;
    }
    form.elements.namedItem("source").required = !listing;
    form.elements.namedItem("observed_on").required = !listing;
    sourceUrl.required = listing;
    sourceUrl.closest(".field").querySelector("label").textContent =
      listing ? "Ссылка на объявление" : "Ссылка на источник";
    confirmation.required = !listing && status.value === "confirmed";
    const label = form.elements
      .namedItem("value")
      .closest(".field")
      .querySelector("label");
    label.textContent =
      "Значение" + (option?.dataset.unit ? ", " + option.dataset.unit : "");
  };
  select.addEventListener("change", sync);
  status.addEventListener("change", sync);
  sourceType.addEventListener("change", sync);
  sync();
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-parser-continue]");
  if (!form) return;
  const progress = form.querySelector("[data-parser-progress]");
  const pause = form.querySelector("[data-parser-pause]");
  const timer = window.setTimeout(() => form.requestSubmit(), 2100);
  progress.textContent = "Загружается следующая страница…";
  pause.hidden = false;
  pause.addEventListener("click", () => {
    window.clearTimeout(timer);
    progress.textContent = "Поиск приостановлен. Можно продолжить с этой страницы.";
    pause.hidden = true;
  });
  document.addEventListener("submit", () => window.clearTimeout(timer), {
    once: true,
    capture: true,
  });
});

document.addEventListener("DOMContentLoaded", () => {
  for (const schedule of document.querySelectorAll("[data-weekly-schedule]")) {
    const mode = schedule.querySelector("[data-schedule-mode]");
    const sync = () => {
      schedule.querySelector("[data-weekly-hours]").hidden = mode.value !== "weekly";
    };
    mode.addEventListener("change", sync);
    sync();
  }
  for (const entry of document.querySelectorAll("[data-operating-requirement]")) {
    const priority = entry.querySelector('select[name$="_priority"]');
    const sync = () => {
      for (const field of entry.querySelectorAll(".entry-fields > .field")) {
        if (!field.contains(priority)) field.hidden = priority.value === "na";
      }
    };
    priority.addEventListener("change", sync);
    sync();
  }
  document.querySelector("[data-analysis-profile]")?.addEventListener("change", (event) => {
    if (event.target.value) window.location.assign(event.target.value);
  });
  const openLinkTarget = () => {
    const target = document.getElementById(window.location.hash.slice(1));
    if (target instanceof HTMLDetailsElement) target.open = true;
  };
  window.addEventListener("hashchange", openLinkTarget);
  openLinkTarget();
  for (const form of document.querySelectorAll("[data-business-setup]")) {
    const format = form.elements.namedItem("business_format");
    const sync = () => {
      for (const questions of form.querySelectorAll("[data-business-category]")) {
        questions.hidden = format.value.split(".")[0] !== questions.dataset.businessCategory;
      }
      for (const field of form.querySelectorAll("[data-business-option]")) {
        const answer = form.elements.namedItem(field.dataset.businessOption);
        field.hidden = answer.value !== "yes" && !field.querySelector(".errorlist");
      }
    };
    form.addEventListener("change", sync);
    sync();
  }
});
