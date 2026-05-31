export const STORAGE_API = "dijkfood-agent-ui-api-base";

export function defaultApiBase() {
  const { hostname, port, protocol } = window.location;
  const isLocal = hostname === "localhost" || hostname === "127.0.0.1";
  if (isLocal && port === "8080") {
    return "http://localhost:8003";
  }
  if (protocol === "http:" || protocol === "https:") {
    return window.location.origin;
  }
  return "http://localhost:8003";
}

export function getApiBaseInput() {
  return document.getElementById("api-base");
}

export function apiBase() {
  const input = getApiBaseInput();
  const raw = ((input && input.value) || defaultApiBase()).trim();
  return raw.replace(/\/+$/, "");
}

export function saveApiBase() {
  sessionStorage.setItem(STORAGE_API, apiBase());
}

export function loadApiBase() {
  const input = getApiBaseInput();
  if (!input) return;
  const saved = sessionStorage.getItem(STORAGE_API);
  input.value = saved || defaultApiBase();
}

export function agentUrl(path) {
  return `${apiBase()}/agent${path}`;
}

export function formatInt(n) {
  return new Intl.NumberFormat().format(Number(n) || 0);
}

export async function checkHealth(healthPill) {
  if (!healthPill) return;
  healthPill.textContent = "…";
  healthPill.className = "health-pill";
  try {
    const res = await fetch(agentUrl("/health"), { method: "GET" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.status === "ok") {
      healthPill.textContent = "API ok";
      healthPill.className = "health-pill ok";
    } else {
      throw new Error("unexpected response");
    }
  } catch {
    healthPill.textContent = "API unreachable";
    healthPill.className = "health-pill err";
    healthPill.title =
      "Check that the agent ECS service is running and the API base URL points at the ALB";
  }
}

export function bindApiBaseChange(healthPill) {
  const input = getApiBaseInput();
  if (!input) return;
  input.addEventListener("change", () => {
    saveApiBase();
    checkHealth(healthPill);
  });
}
