/* Diagnostics page. */
(function () {
  "use strict";

  const button = document.querySelector("[data-test-run]");
  const results = document.querySelector("[data-test-results]");
  const summary = document.querySelector("[data-test-summary]");
  if (!button || !results) return;

  const meta = document.querySelector('meta[name="csrf-token"]');
  const csrf = meta ? meta.getAttribute("content") : "";

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function render(payload) {
    clear(results);
    const checks = payload.checks || [];
    checks.forEach(function (check) {
      const item = document.createElement("div");
      item.className = "test-item " + (check.ok ? "ok" : (check.required ? "fail" : "optional"));
      const status = document.createElement("span");
      status.className = "test-status";
      status.textContent = check.ok ? "✓" : (check.required ? "!" : "·");
      const body = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = check.name;
      const detail = document.createElement("small");
      detail.textContent = check.detail || "";
      body.appendChild(title);
      body.appendChild(detail);
      item.appendChild(status);
      item.appendChild(body);
      results.appendChild(item);
    });

    const failed = checks.filter(function (check) { return !check.ok && check.required; }).length;
    if (summary) {
      summary.textContent = failed === 0 ? "Все обязательные проверки пройдены" : "Ошибок: " + failed;
      summary.className = "badge " + (failed === 0 ? "badge-success" : "badge-error");
    }
  }

  button.addEventListener("click", async function () {
    button.disabled = true;
    const original = button.innerHTML;
    button.textContent = "Проверка…";
    clear(results);
    const loading = document.createElement("div");
    loading.className = "empty-state";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    loading.appendChild(spinner);
    results.appendChild(loading);

    try {
      const response = await fetch(button.getAttribute("data-test-run"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-CSRF-Token": csrf
        },
        body: JSON.stringify({ csrf_token: csrf })
      });
      const data = await response.json();
      if (!response.ok && !data.checks) throw new Error(data.error || "Ошибка проверки");
      render(data);
    } catch (error) {
      clear(results);
      const item = document.createElement("div");
      item.className = "flash flash-error";
      item.textContent = error.message || "Не удалось выполнить проверки";
      results.appendChild(item);
      if (summary) {
        summary.textContent = "Ошибка";
        summary.className = "badge badge-error";
      }
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  });
})();
