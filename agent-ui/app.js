import {
  agentUrl,
  bindApiBaseChange,
  checkHealth,
  formatInt,
  loadApiBase,
  saveApiBase,
} from "./api.js";

const STORAGE_CONV = "dijkfood-agent-ui-conversation-id";

const healthPill = document.getElementById("health-pill");
const messagesEl = document.getElementById("messages");
const chatForm = document.getElementById("chat-form");
const messageInput = document.getElementById("message-input");
const btnSend = document.getElementById("btn-send");
const btnNewChat = document.getElementById("btn-new-chat");
const tplMessage = document.getElementById("tpl-message");

let conversationId = sessionStorage.getItem(STORAGE_CONV) || null;
let sending = false;

function setConversationId(id) {
  conversationId = id;
  if (id) {
    sessionStorage.setItem(STORAGE_CONV, id);
  } else {
    sessionStorage.removeItem(STORAGE_CONV);
  }
}

function appendMessage({ role, text, toolsUsed = null, usage = null, pending = false }) {
  const empty = messagesEl.querySelector(".messages-empty");
  if (empty) empty.remove();

  const node = tplMessage.content.cloneNode(true);
  const article = node.querySelector(".message");
  article.classList.add(role);
  if (pending) article.classList.add("pending");

  node.querySelector(".message-role").textContent =
    role === "user" ? "You" : role === "assistant" ? "Agent" : "Error";
  node.querySelector(".message-body").textContent = text;

  const usageEl = node.querySelector(".message-usage");
  if (usage && usage.total_tokens > 0 && usageEl) {
    usageEl.classList.remove("hidden");
    usageEl.textContent = `This reply: ${formatInt(usage.total_tokens)} tokens (${formatInt(usage.input_tokens)} in / ${formatInt(usage.output_tokens)} out), ${usage.bedrock_rounds} Bedrock call(s), ${usage.tool_calls} tool(s)`;
  }

  const toolsBlock = node.querySelector(".message-tools");
  if (toolsUsed && toolsUsed.length > 0) {
    toolsBlock.classList.remove("hidden");
    node.querySelector(".tools-json").textContent = JSON.stringify(toolsUsed, null, 2);
  }

  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return messagesEl.lastElementChild;
}

function showWelcome() {
  messagesEl.innerHTML = "";
  const p = document.createElement("p");
  p.className = "messages-empty";
  p.textContent =
    "Start a conversation. The agent uses tools to read order state and history from your running services.";
  messagesEl.appendChild(p);
}

function setBusy(busy) {
  sending = busy;
  messageInput.disabled = busy;
  btnSend.disabled = busy;
  btnSend.textContent = busy ? "…" : "Send";
}

async function sendMessage(text) {
  const body = { message: text };
  if (conversationId) {
    body.conversation_id = conversationId;
  }

  const res = await fetch(agentUrl("/v1/chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

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

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = messageInput.value.trim();
  if (!text || sending) return;

  saveApiBase();
  appendMessage({ role: "user", text });
  messageInput.value = "";
  setBusy(true);

  const pendingEl = appendMessage({
    role: "assistant",
    text: "Thinking…",
    pending: true,
  });

  try {
    const data = await sendMessage(text);
    pendingEl.remove();
    setConversationId(data.conversation_id);
    appendMessage({
      role: "assistant",
      text: data.reply || "(empty reply)",
      toolsUsed: data.tools_used,
      usage: data.usage,
    });
  } catch (err) {
    pendingEl.remove();
    appendMessage({
      role: "error",
      text: err.message || String(err),
    });
  } finally {
    setBusy(false);
    messageInput.focus();
  }
});

btnNewChat.addEventListener("click", async () => {
  if (conversationId) {
    try {
      await fetch(agentUrl(`/v1/conversations/${conversationId}`), {
        method: "DELETE",
      });
    } catch {
      /* ignore — session may already be gone */
    }
  }
  setConversationId(null);
  showWelcome();
  messageInput.focus();
});

messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

loadApiBase();
bindApiBaseChange(healthPill);
showWelcome();
checkHealth();
