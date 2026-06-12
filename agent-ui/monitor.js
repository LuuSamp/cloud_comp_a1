import {
  agentUrl,
  apiBase,
  bindApiBaseChange,
  checkHealth,
  formatInt,
  loadApiBase,
  saveApiBase,
} from "./api.js";

const healthPill = document.getElementById("health-pill");
const btnRefresh = document.getElementById("btn-refresh");
const alertBanner = document.getElementById("alert-banner");
const monitorMeta = document.getElementById("monitor-meta");
const statsGrid = document.getElementById("stats-grid");
const budgetSection = document.getElementById("budget-section");
const budgetBars = document.getElementById("budget-bars");
const dailyBody = document.getElementById("daily-body");
const dailyEmpty = document.getElementById("daily-empty");

let refreshTimer = null;

function showAlert(message, level = "warn") {
  alertBanner.textContent = message;
  alertBanner.className = `alert-banner ${level}`;
  alertBanner.classList.remove("hidden");
}

function hideAlert() {
  alertBanner.classList.add("hidden");
}

function budgetClass(pct) {
  if (pct == null) return "";
  if (pct >= 90) return "critical";
  if (pct >= 75) return "warn";
  return "ok";
}

function renderBudgetBar(label, used, budget, pct) {
  const width = pct != null ? Math.min(100, pct) : 0;
  const cls = budgetClass(pct);
  return `
    <div class="budget-row">
      <div class="budget-label">
        <span>${label}</span>
        <span class="budget-numbers">
          ${formatInt(used)} / ${budget != null ? formatInt(budget) : "—"} tokens
          ${pct != null ? `(${pct}%)` : ""}
        </span>
      </div>
      <div class="budget-track" aria-hidden="true">
        <div class="budget-fill ${cls}" style="width: ${width}%"></div>
      </div>
    </div>
  `;
}

function statCard(title, value, detail = "") {
  return `
    <article class="stat-card">
      <h3 class="stat-title">${title}</h3>
      <p class="stat-value">${value}</p>
      ${detail ? `<p class="stat-detail">${detail}</p>` : ""}
    </article>
  `;
}

function renderSummary(data) {
  const t = data.totals || {};
  const b = data.budget || {};
  const historyDays = data.usage_history_days || 7;
  const historyDaysEl = document.getElementById("history-days");
  if (historyDaysEl) historyDaysEl.textContent = String(historyDays);

  monitorMeta.innerHTML = `
    <span>Model: <strong>${data.model_id || "—"}</strong></span>
    <span>Max output tokens / request: <strong>${formatInt(data.max_output_tokens)}</strong></span>
    <span>Max tool rounds / request: <strong>${formatInt(data.max_tool_rounds)}</strong></span>
    <span>Metrics refreshed: <strong>${data.updated_at || "—"}</strong></span>
  `;

  statsGrid.innerHTML = [
    statCard(
      "Tokens (CloudWatch)",
      formatInt(t.total_tokens),
      `Last ${historyDays} days, Bedrock account`
    ),
    statCard(
      "Input / output",
      `${formatInt(t.input_tokens)} / ${formatInt(t.output_tokens)}`,
      "Prompt vs completion"
    ),
    statCard("Chat requests", formatInt(t.request_count), "DynamoDB (lab session)"),
    statCard("Bedrock API calls", formatInt(t.bedrock_rounds), "CloudWatch Invocations"),
    statCard("Tool invocations", formatInt(t.tool_calls), "DynamoDB (lab session)"),
    statCard("Today (UTC)", formatInt(b.today_tokens), "Tokens from CloudWatch"),
  ].join("");

  const hasBudget =
    b.total_tokens != null || b.daily_tokens != null;
  if (hasBudget) {
    budgetSection.classList.remove("hidden");
    budgetBars.innerHTML = [
      b.total_tokens != null
        ? renderBudgetBar(
            "Lifetime budget",
            t.total_tokens,
            b.total_tokens,
            b.total_used_pct
          )
        : "",
      b.daily_tokens != null
        ? renderBudgetBar(
            "Today (UTC)",
            b.today_tokens,
            b.daily_tokens,
            b.daily_used_pct
          )
        : "",
    ].join("");

    if (b.total_used_pct >= 90 || b.daily_used_pct >= 90) {
      showAlert(
        "Usage is above 90% of a configured budget. Pause testing or raise budgets in .env.agent.",
        "critical"
      );
    } else if (b.total_used_pct >= 75 || b.daily_used_pct >= 75) {
      showAlert("Usage is above 75% of a configured budget.", "warn");
    } else {
      hideAlert();
    }
  } else {
    budgetSection.classList.add("hidden");
    hideAlert();
  }

  const daily = data.daily || [];
  if (daily.length === 0) {
    dailyBody.innerHTML = "";
    dailyEmpty.classList.remove("hidden");
  } else {
    dailyEmpty.classList.add("hidden");
    dailyBody.innerHTML = daily
      .map(
        (row) => `
      <tr>
        <td>${row.date}</td>
        <td>${formatInt(row.request_count)}</td>
        <td>${formatInt(row.total_tokens)}</td>
        <td>${formatInt(row.input_tokens)} / ${formatInt(row.output_tokens)}</td>
        <td>${formatInt(row.bedrock_rounds)}</td>
        <td>${formatInt(row.tool_calls)}</td>
      </tr>
    `
      )
      .join("");
  }
}

async function fetchUsage() {
  const res = await fetch(agentUrl("/v1/usage"), { method: "GET" });
  let payload;
  try {
    payload = await res.json();
  } catch {
    payload = { detail: await res.text() };
  }
  if (!res.ok) {
    const detail =
      typeof payload.detail === "string"
        ? payload.detail
        : JSON.stringify(payload.detail ?? payload, null, 2);
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return payload;
}

async function refresh() {
  btnRefresh.disabled = true;
  btnRefresh.textContent = "…";
  try {
    saveApiBase();
    await checkHealth(healthPill);
    const data = await fetchUsage();
    renderSummary(data);
  } catch (err) {
    showAlert(err.message || String(err), "critical");
  } finally {
    btnRefresh.disabled = false;
    btnRefresh.textContent = "Refresh";
  }
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refresh, 30000);
}

btnRefresh.addEventListener("click", () => refresh());

loadApiBase();
bindApiBaseChange(healthPill);
refresh();
startAutoRefresh();
