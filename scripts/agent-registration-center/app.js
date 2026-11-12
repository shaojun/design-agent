"use strict";
const $ = (id) => document.getElementById(id);
const states = [
  "SUBMITTED",
  "WORKING",
  "INPUT_REQUIRED",
  "AUTH_REQUIRED",
  "COMPLETED",
  "FAILED",
  "CANCELED",
  "REJECTED",
];
const terminal = new Set([
  "TASK_STATE_COMPLETED",
  "TASK_STATE_FAILED",
  "TASK_STATE_CANCELED",
  "TASK_STATE_REJECTED",
]);
let operations = [],
  protocolOperations = [];
let items = [],
  chosen = null,
  detail = null,
  card = {},
  selection = 0,
  busy = false,
  aborter = null;
let attachments = [],
  activeTurn = null,
  currentTask = null,
  viewedTask = null,
  nextPage = "",
  events = 0;
const PROGRESS_KEY = "urn:design-agent:a2a:progress:v1";
const interrupted = new Set([
  "TASK_STATE_INPUT_REQUIRED",
  "TASK_STATE_AUTH_REQUIRED",
]);
const text = (node, value) => {
  node.textContent = value ?? "";
  return node;
};
function el(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) text(node, content);
  if (className) node.className = className;
  return node;
}
function notice(message = "") {
  text($("notice"), message);
  $("notice").hidden = !message;
  for (const id of [
    "chat-notice",
    "tasks-notice",
    "push-notice",
    "protocol-notice",
  ]) {
    text($(id), message);
    $(id).hidden = !message;
  }
}
function jsonValue(id, fallback = {}) {
  return $(id).value.trim() ? JSON.parse($(id).value) : fallback;
}
function errorMessage(data, status) {
  return typeof data?.detail === "string"
    ? data.detail
    : `请求失败 (${status})`;
}
async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    throw new Error(
      `HTTP ${response.status}：本地应用返回非 JSON 响应，请检查 VS Code 调试控制台，并重启应用后重试。`,
    );
  }
}
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-ARC-Request": "1",
          },
          body: JSON.stringify(body),
        },
  );
  const data = await responseJson(response);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return data;
}
function reportActionError(error) {
  const message = error.message || "操作失败，请重试";
  notice(message);
  if (error.code?.startsWith("agent_token_") || error.code === "agent_auth") {
    text($("token-error"), message);
    $("token-error").hidden = false;
    $("agent-token").setAttribute("aria-invalid", "true");
    text(
      $("token-status"),
      error.code === "agent_token_missing"
        ? "凭证缺失 · 任务未提交"
        : error.code === "agent_token_format"
          ? "凭证格式错误 · 任务未提交"
          : `目标服务拒绝凭证 / 权限 · HTTP ${error.status}`,
    );
    text($("chat-auth-status"), $("token-status").textContent);
  }
}
function tokenInputChanged() {
  $("agent-token").removeAttribute("aria-invalid");
  $("token-error").hidden = true;
  const local = $("saved-agent").checked;
  const filled = !!$("agent-token").value.trim();
  const status = local
    ? "使用本地凭证文件 · 尚未验证"
    : filled
      ? "已填写 · 尚未验证"
      : requiresBearer()
        ? "缺少 token · 此 Agent 要求独立调用凭证"
        : "未填写 · Card 未要求 Bearer token，可留空尝试调用";
  text($("token-status"), status);
  text($("chat-auth-status"), status);
}
$("agent-token").addEventListener("input", tokenInputChanged);
$("show-token-help").addEventListener("click", () => {
  $("token-help").open = true;
  $("token-help").scrollIntoView({ behavior: "smooth", block: "center" });
  if (!$("agent-token").disabled)
    $("agent-token").focus({ preventScroll: true });
});
function target() {
  if (!chosen || !$("endpoint").value)
    throw new Error("请先选择已发现的可用端点");
  if (
    requiresBearer() &&
    !$("saved-agent").checked &&
    !$("agent-token").value.trim()
  )
    throw Object.assign(
      new Error(
        "未发送：此 Agent 要求 Bearer token。请点击输入框旁的“填写 / 获取 Agent token”，向目标 Agent 部署者申请凭证；Nacos 用户名 / 密码不能代替它。",
      ),
      { code: "agent_token_missing" },
    );
  if (/\s/.test($("agent-token").value.trim()) && !$("saved-agent").checked)
    throw Object.assign(
      new Error(
        "未发送：只填写 token 值，不要包含 Bearer 前缀、caller-id 或空格。请参考“Agent token 如何获取”。",
      ),
      { code: "agent_token_format" },
    );
  return {
    agent: chosen.agentName,
    version: $("version").value,
    endpoint: $("endpoint").value,
    token: $("agent-token").value.trim(),
    saved: $("saved-agent").checked,
  };
}
function logEvent(value) {
  // Mask push secrets in the protocol inspector, including credentials returned by a server.
  const encoded = JSON.stringify(
    value,
    (key, value) =>
      /^(token|credentials|authorization|password)$/i.test(key)
        ? "••••••"
        : value,
    2,
  );
  $("events").textContent += (events ? "\n" : "") + encoded;
  events++;
  text($("event-count"), events);
  $("events").scrollTop = $("events").scrollHeight;
}
async function perform(action, params, receive = () => {}, signal) {
  const response = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ARC-Request": "1" },
    body: JSON.stringify({
      ...target(),
      action,
      params,
      force: $("force-test").checked,
    }),
    signal,
  });
  if (!response.ok) {
    const data = await responseJson(response);
    throw Object.assign(new Error(errorMessage(data, response.status)), {
      status: response.status,
    });
  }
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    finished = false;
  function process(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "error") {
      logEvent(event);
      throw Object.assign(new Error(event.message), {
        code: event.code,
        status: event.http_status,
      });
    }
    if (event.type === "done") {
      finished = true;
      return;
    }
    logEvent({ action, ...event });
    if (event.type === "warning") {
      notice(event.message);
      return;
    }
    $("agent-token").removeAttribute("aria-invalid");
    $("token-error").hidden = true;
    text(
      $("token-status"),
      "目标服务已接受本次操作 · 凭证未显示或保存到浏览器存储",
    );
    text($("chat-auth-status"), "目标服务已接受本次操作");
    receive(event);
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        process(buffer.slice(0, end));
        buffer = buffer.slice(end + 1);
      }
      if (done) break;
    }
    if (buffer.trim()) process(buffer);
    if (!finished)
      throw new Error("连接提前结束，任务可能仍在远端运行，请查询任务。");
  } finally {
    await reader.cancel().catch(() => {});
  }
}
function versions(item) {
  return (item.versionInfo?.onlineVersions || []).map((v) => v.version);
}
function setTab(name) {
  for (const button of document.querySelectorAll("[data-tab]")) {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    $("panel-" + button.dataset.tab).hidden = !selected;
  }
}
for (const button of document.querySelectorAll("[data-tab]"))
  button.addEventListener("click", () => setTab(button.dataset.tab));
function resetChat() {
  attachments = [];
  renderAttachments();
  $("chat").replaceChildren(
    el("p", "发送消息，与这个 Agent 开始对话。", "empty"),
  );
  text($("watch-status"), "");
  $("context").value = "";
  $("continue-task").value = "";
  activeTurn = null;
  currentTask = null;
  setTaskState("尚未发送任务");
  $("task-id").value = "";
  $("push-task").value = "";
}
function resetSelection() {
  $("force-test").checked = false;
  selection++;
  chosen = null;
  detail = null;
  card = {};
  nextPage = "";
  viewedTask = null;
  $("task-list").replaceChildren();
  $("task-view").replaceChildren();
  $("push-result").hidden = true;
  $("push-page").value = "";
  $("push-token").value = "";
  $("push-credentials").value = "";
  $("operation-result").hidden = true;
  $("events").textContent = "";
  events = 0;
  text($("event-count"), 0);
  $("selected").hidden = true;
  $("selection-empty").hidden = false;
  text($("selection-empty"), "从左侧选择一个 Agent。");
  $("agent-token").value = "";
  $("data-part").value = "";
  $("file-url").value = "";
  $("send-options").value = "";
  resetChat();
}
function renderList() {
  $("agents").replaceChildren();
  text($("count"), items.length);
  const query = $("filter").value.toLowerCase();
  for (const item of items.filter((x) =>
    (x.agentName + " " + (x.displayName || "")).toLowerCase().includes(query),
  )) {
    const button = el(
      "button",
      undefined,
      "agent-item" + (chosen?.agentName === item.agentName ? " active" : ""),
    );
    button.type = "button";
    button.disabled = busy;
    button.append(
      el("strong", item.displayName || item.agentName),
      el(
        "span",
        `${item.agentName} · ${versions(item).join(", ") || "无已发布版本"}`,
      ),
    );
    button.addEventListener("click", () => selectAgent(item));
    $("agents").append(button);
  }
  if (!$("agents").children.length)
    $("agents").append(el("p", "没有匹配的 Agent。", "empty"));
}
async function selectAgent(item) {
  resetSelection();
  chosen = item;
  renderList();
  notice();
  text($("agent-title"), item.displayName || item.agentName);
  $("version").replaceChildren();
  for (const version of versions(item))
    $("version").add(new Option(version, version));
  if (!$("version").value) {
    notice("此 Agent 没有已发布版本。");
    return;
  }
  await loadDetail();
}
function briefCell(label, value) {
  const box = el("dl", undefined, "brief-cell");
  box.append(el("dt", label), el("dd", value));
  return box;
}
function inputModes() {
  return [
    ...new Set([
      ...(card.defaultInputModes || []),
      ...(card.skills || []).flatMap((skill) => skill.inputModes || []),
    ]),
  ];
}
function mediaAllowed(media) {
  return inputModes().some(
    (mode) =>
      mode === "*/*" ||
      mode === media ||
      (mode.endsWith("/*") && media.startsWith(mode.slice(0, -1))),
  );
}
function supportInfo(action) {
  const operation = protocolOperations.find((item) => item.name === action);
  if (!operation?.capability)
    return {
      supported: true,
      label: "标准操作 · 无逐项开关",
      field: "—（由 A2A 定义）",
    };
  const value = card.capabilities?.[operation.capability];
  return {
    supported: value === true,
    label:
      value === true
        ? "声明支持（true）"
        : value === false
          ? "声明不支持（false）"
          : "未声明（按不支持处理）",
    field: "capabilities." + operation.capability,
  };
}
function actionAllowed(action) {
  return $("force-test").checked || supportInfo(action).supported;
}
function renderOperationGuide() {
  $("operation-map").replaceChildren();
  for (const operation of protocolOperations) {
    const support = supportInfo(operation.name),
      row = el("tr"),
      description = el("td");
    description.append(
      el("code", operation.name),
      el("p", operation.description, "hint"),
    );
    const entry = el("td"),
      button = el("button", "查看 / 测试", "secondary compact");
    button.type = "button";
    button.addEventListener("click", () => {
      setTab("protocol");
      $("operation").value = operation.name;
      operationTemplate();
      $("operation").scrollIntoView({ block: "center", behavior: "smooth" });
    });
    entry.append(button);
    row.append(
      description,
      el("td", support.field),
      el("td", support.label, support.supported ? "support-yes" : "support-no"),
      entry,
    );
    $("operation-map").append(row);
  }
  for (const option of $("operation").options) {
    option.disabled = false;
    option.textContent = option.value + " · " + supportInfo(option.value).label;
  }
  $("send-mode").options[0].disabled = !actionAllowed("SendStreamingMessage");
  $("subscribe-task").disabled = !actionAllowed("SubscribeToTask");
  $("extended-card").disabled = !actionAllowed("GetExtendedAgentCard");
  for (const name of ["create", "get", "list", "delete"])
    $("push-" + name).disabled = !actionAllowed(
      "CreateTaskPushNotificationConfig",
    );
  const push = supportInfo("CreateTaskPushNotificationConfig");
  text(
    $("push-hint"),
    push.field +
      "：" +
      push.label +
      (push.supported
        ? "。"
        : $("force-test").checked
          ? "；已允许强制测试，远端可能拒绝。"
          : "；可勾选上方选项后仍然测试。"),
  );
  $("upload").disabled = !inputModes().length && !$("force-test").checked;
  operationSupport();
  sendProtocolHint();
}
function operationSupport() {
  const action = $("operation").value,
    support = supportInfo(action),
    operation = protocolOperations.find((item) => item.name === action);
  text(
    $("operation-support"),
    `${action}：${operation?.description || ""}。依据 ${support.field}，${support.label}。请求类型 ${operation?.requestType || ""}；字段 ${(operation?.fields || []).join(", ")}。`,
  );
}
function sendProtocolHint() {
  const action =
      $("send-mode").value === "stream"
        ? "SendStreamingMessage"
        : "SendMessage",
    support = supportInfo(action);
  text(
    $("send-protocol-hint"),
    `本次操作：${action} · ${support.field} · ${support.label}${$("send-mode").value === "background" ? "；configuration.returnImmediately=true" : ""}`,
  );
}
function requiresBearer() {
  const schemes = card.securitySchemes || {},
    requirements = card.securityRequirements || [];
  return (
    requirements.length > 0 &&
    requirements.every((requirement) =>
      Object.keys(requirement.schemes || {}).some(
        (name) =>
          schemes[name]?.httpAuthSecurityScheme?.scheme?.toLowerCase() ===
          "bearer",
      ),
    )
  );
}
$("force-test").addEventListener("change", () => {
  renderOperationGuide();
});
$("send-mode").addEventListener("change", sendProtocolHint);

function renderBrief() {
  text(
    $("description"),
    card.description || detail.detail.description || "暂无描述",
  );
  const capabilities = card.capabilities || {};
  const provider = card.provider?.organization || "未声明";
  const security =
    Object.keys(card.securitySchemes || {})
      .map((name) => {
        const scheme = card.securitySchemes[name];
        return (
          scheme.httpAuthSecurityScheme?.scheme ||
          Object.keys(scheme)[0] ||
          name
        );
      })
      .join(", ") || "未声明";
  $("card-brief").replaceChildren(
    briefCell(
      "版本 / 提供方",
      `${card.version || $("version").value} · ${provider}`,
    ),
    briefCell("协议 / 鉴权", `A2A 1.0 · JSONRPC · ${security}`),
    briefCell(
      "输入类型",
      (card.defaultInputModes || []).join(", ") || "未声明",
    ),
    briefCell(
      "输出类型",
      (card.defaultOutputModes || []).join(", ") || "未声明",
    ),
    briefCell(
      "可选能力",
      `流式 ${capabilities.streaming ? "✓" : "—"} · Push ${capabilities.pushNotifications ? "✓" : "—"} · 扩展 Card ${capabilities.extendedAgentCard ? "✓" : "—"}`,
    ),
    briefCell(
      "健康端点",
      `${detail.endpoints.filter((ep) => ep.callable).length} / ${detail.endpoints.length} 个可调用`,
    ),
  );
  if (card.documentationUrl) {
    try {
      const url = new URL(card.documentationUrl);
      if (
        ["https:", "http:"].includes(url.protocol) &&
        !url.username &&
        !url.password
      ) {
        const box = briefCell("部署与接入说明", "");
        const link = el("a", "查看 Agent 文档");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        box.append(link);
        $("card-brief").append(box);
      }
    } catch {
      /* An invalid optional link does not prevent Card rendering. */
    }
  }
  $("skills").replaceChildren();
  for (const skill of card.skills || []) {
    const box = el("div", undefined, "skill-card");
    box.append(
      el("strong", skill.name || skill.id),
      el("p", skill.description || ""),
    );
    if (skill.tags?.length) box.append(el("p", skill.tags.join(" · "), "hint"));
    for (const example of skill.examples || []) {
      const b = el("button", example);
      b.type = "button";
      b.addEventListener("click", () => {
        setTab("chat");
        $("prompt").value = example;
        $("prompt").focus();
      });
      box.append(b);
    }
    $("skills").append(box);
  }
  if (!$("skills").children.length)
    $("skills").append(el("p", "此 Card 未声明技能。", "hint"));
  $("raw-detail").textContent = JSON.stringify(detail.detail, null, 2);
  $("endpoint-list").replaceChildren();
  $("endpoint").replaceChildren();
  for (const ep of detail.endpoints) {
    $("endpoint-list").append(
      el(
        "div",
        `${ep.healthy ? "健康" : "不健康"} / ${ep.enabled ? "启用" : "禁用"} · ${ep.source} · ${ep.transport} · A2A ${ep.protocolVersion}\n${ep.uri}`,
        "endpoint-row" + (ep.callable ? "" : " unhealthy"),
      ),
    );
    if (ep.callable) $("endpoint").add(new Option(ep.uri, ep.uri));
  }
  if (!detail.endpoints.length) text($("endpoint-list"), "暂无运行端点");
  $("send-mode").value = capabilities.streaming ? "stream" : "sync";
  renderOperationGuide();
  text(
    $("agent-auth-hint"),
    requiresBearer()
      ? "AgentCard.securityRequirements 要求 Bearer token。Nacos 登录只负责目录发现；发送消息前需另填目标 Agent token，或使用本地凭证文件。"
      : "当前 Card 未要求 Bearer token，可留空调用。本仓库 Agent 默认匿名；其它鉴权方式仍以目标服务声明为准。",
  );
  text(
    $("caller-isolation-hint"),
    "任务与文件可见性（本仓库 Agent）：" +
      (requiresBearer()
        ? "token 对应一个调用身份 caller。不同身份的任务和文件互相隔离；共用同一身份的客户端共享访问权限，与浏览器或 IP 无关。"
        : "匿名模式下所有客户端共享任务列表与结果文件。不同 contextId 只区分会话，不隔离访问权限；相同 contextId 会共享会话。") +
      "其它 harness 的权限模型需向其部署者确认，Card 不保证 caller 隔离。",
  );
  $("upload").disabled = !inputModes().length && !$("force-test").checked;
  text(
    $("input-hint"),
    `输入媒体类型：${inputModes().join(", ") || "未声明"}。上传 / URL / JSON 对应 message.parts[].raw / url / data，上限 8 MiB；媒体类型依据 defaultInputModes / skills[].inputModes。匹配媒体类型不保证目标支持文件 Part。`,
  );
  $("send").disabled = !$("endpoint").value;
  tokenInputChanged();
}
async function loadDetail() {
  const generation = ++selection;
  $("selected").hidden = true;
  $("selection-empty").hidden = false;
  text($("selection-empty"), "正在发现服务端点…");
  resetChat();
  try {
    const data = await api(
      "/api/detail?" +
        new URLSearchParams({
          agent: chosen.agentName,
          version: $("version").value,
        }),
    );
    if (generation !== selection) return;
    detail = data;
    card =
      data.detail.callInterfaces?.find(
        (x) => x.protocol === "a2a" && x.protocolVersion === "1.0",
      )?.nativeDescriptor || {};
    renderBrief();
    $("selected").hidden = false;
    $("selection-empty").hidden = true;
  } catch (error) {
    if (generation === selection) {
      text($("selection-empty"), "详情加载失败，请刷新重试。");
      notice(error.message);
    }
  }
}
function savedFields() {
  const saved = $("saved-registry").checked;
  $("username").disabled = saved;
  $("password").disabled = saved;
}
$("saved-registry").addEventListener("change", savedFields);
$("saved-agent").addEventListener("change", () => {
  $("agent-token").disabled = $("saved-agent").checked;
  tokenInputChanged();
});
$("connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  notice();
  $("connect").disabled = true;
  try {
    const data = await api("/api/connect", {
      url: $("registry-url").value,
      namespace: $("namespace").value,
      username: $("username").value,
      password: $("password").value,
      saved: $("saved-registry").checked,
    });
    $("password").value = "";
    items = data.items;
    resetSelection();
    renderList();
    text($("connection"), `已连接 · ${data.url} / ${data.namespace}`);
    $("refresh").disabled = false;
    $("disconnect").disabled = false;
  } catch (error) {
    notice(error.message);
  } finally {
    $("connect").disabled = false;
  }
});
$("disconnect").addEventListener("click", async () => {
  try {
    await api("/api/disconnect", {});
    items = [];
    resetSelection();
    renderList();
    text($("connection"), "已断开");
    $("refresh").disabled = true;
    $("disconnect").disabled = true;
  } catch (error) {
    notice(error.message);
  }
});
$("refresh").addEventListener("click", async () => {
  notice();
  try {
    items = (await api("/api/agents")).items;
    resetSelection();
    renderList();
  } catch (error) {
    notice(error.message);
  }
});
$("filter").addEventListener("input", renderList);
$("version").addEventListener("change", () => {
  nextPage = "";
  $("task-list").replaceChildren();
  $("task-view").replaceChildren();
  loadDetail();
});
$("endpoint").addEventListener("change", () => {
  $("agent-token").value = "";
  resetChat();
});
$("new-context").addEventListener("click", resetChat);
function scrollChat() {
  $("chat").scrollTop = $("chat").scrollHeight;
}
function bubble(role) {
  $("chat").querySelector(".empty")?.remove();
  const node = el("article", undefined, "bubble " + role);
  node.append(
    el(
      "div",
      role === "user"
        ? "YOU"
        : role === "agent"
          ? card.name || "AGENT"
          : "INFO",
      "role-label",
    ),
  );
  const history = el("details", undefined, "progress-history");
  const summary = el("summary", "执行过程"),
    steps = el("ol");
  history.hidden = true;
  history.append(summary, steps);
  const timing = el("div", undefined, "hint");
  const body = el("div", undefined, "body"),
    files = el("div"),
    status = el("div", undefined, "status-label");
  node.append(timing, history, body, files, status);
  $("chat").append(node);
  scrollChat();
  return {
    node,
    timing,
    history,
    summary,
    steps,
    startedAt: Date.now(),
    lastAt: Date.now(),
    stepRows: new Map(),
    statusKeys: new Set(),
    body,
    files,
    status,
    artifacts: new Map(),
    messageParts: [],
    progress: "",
    taskId: "",
  };
}
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = name || "artifact";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function decodeRaw(raw) {
  const value = atob(raw);
  return Uint8Array.from(value, (c) => c.charCodeAt(0));
}
async function downloadUrl(part, taskId) {
  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-ARC-Request": "1",
      },
      body: JSON.stringify({ ...target(), task_id: taskId, url: part.url }),
    });
    if (!response.ok) {
      const data = await responseJson(response);
      throw new Error(errorMessage(data, response.status));
    }
    saveBlob(await response.blob(), part.filename);
  } catch (error) {
    notice(error.message);
  }
}
function renderParts(parts, body, files, taskId) {
  text(
    body,
    parts
      .filter((p) => typeof p.text === "string")
      .map((p) => p.text)
      .join(""),
  );
  files.replaceChildren();
  for (const part of parts) {
    if (part.data !== undefined)
      files.append(el("pre", JSON.stringify(part.data, null, 2)));
    if (part.raw !== undefined) {
      const button = el(
        "button",
        `下载 ${part.filename || "附件"} · ${part.mediaType || "application/octet-stream"}`,
        "secondary compact file-result",
      );
      button.addEventListener("click", () => {
        try {
          saveBlob(
            new Blob([decodeRaw(part.raw)], {
              type: part.mediaType || "application/octet-stream",
            }),
            part.filename,
          );
        } catch {
          notice("附件 raw 数据无效。");
        }
      });
      files.append(button);
    }
    if (part.url) {
      const line = el("div", undefined, "file-result");
      line.append(el("span", part.filename || part.url, "hint"));
      const button = el("button", "下载", "secondary compact");
      button.disabled = !taskId;
      button.addEventListener("click", () => downloadUrl(part, taskId));
      line.append(button);
      files.append(line);
    }
  }
}
function recordProgress(turn, status) {
  if (!status) return;
  const message = status.message;
  const progress = message?.metadata?.[PROGRESS_KEY];
  const content = (message?.parts || []).map((p) => p.text || "").join("");
  if (!content) return;
  turn.history.hidden = false;
  if (progress?.stepId) {
    let row = turn.stepRows.get(progress.stepId);
    if (!row) {
      row = el("li");
      turn.stepRows.set(progress.stepId, row);
      turn.steps.append(row);
    }
    const state =
      { running: "进行中", completed: "已完成", failed: "失败" }[
        progress.state
      ] || progress.state;
    text(
      row,
      `${progress.tool} · ${state} · ${Number(progress.elapsedSeconds || 0).toFixed(1)}s`,
    );
    row.className = progress.state === "failed" ? "step-failed" : "";
  } else {
    if (terminal.has(status.state)) return;
    const key = `${status.timestamp || ""}|${status.state}|${content}`;
    if (!turn.statusKeys.has(key)) {
      turn.statusKeys.add(key);
      const timestamp = status.timestamp
        ? new Date(status.timestamp).toLocaleTimeString()
        : "";
      turn.steps.append(
        el(
          "li",
          `${timestamp} ${content.slice(0, 400)}${content.length > 400 ? "…" : ""}`,
        ),
      );
    }
  }
  text(
    turn.summary,
    `执行过程 · ${turn.steps.childElementCount} 项（展开查看）`,
  );
}
function updateTiming(turn) {
  const end = turn.endedAt || Date.now();
  text(
    turn.timing,
    `任务 ${turn.taskId || "提交中"} · ${turn.endedAt ? "本次接收用时" : "本次已等待"} ${Math.floor((end - turn.startedAt) / 1000)}s · 最后更新 ${new Date(turn.lastAt).toLocaleTimeString()}`,
  );
}
setInterval(() => {
  if (activeTurn && busy) updateTiming(activeTurn);
}, 1000);
function renderTurn(turn) {
  const parts = turn.artifacts.size
    ? [...turn.artifacts.values()].flatMap((x) => x.parts || [])
    : turn.messageParts;
  renderParts(parts, turn.body, turn.files, turn.taskId);
  if (!parts.length) text(turn.body, turn.progress || "等待 Agent 回复…");
  updateTiming(turn);
  scrollChat();
}
function setTaskState(state) {
  text($("call-state"), state);
  $("recover-current").disabled = busy || !currentTask?.id;
  $("cancel-current").disabled =
    !currentTask?.id || terminal.has(currentTask.status?.state);
}
function rememberTask(task) {
  currentTask = { ...currentTask, ...task };
  if (task.contextId) $("context").value = task.contextId;
  if (task.id) {
    $("task-id").value = task.id;
    $("push-task").value = task.id;
  }
  const state = task.status?.state || "";
  setTaskState(`${state.replace("TASK_STATE_", "")} ${task.id || ""}`);
  if (interrupted.has(state)) $("continue-task").value = task.id;
  else if (terminal.has(state)) $("continue-task").value = "";
}
function consumeChat(turn, event) {
  turn.lastAt = Date.now();
  if (event.type === "task" || event.data?.task) {
    const task = event.type === "task" ? event.data : event.data.task;
    rememberTask(task);
    turn.taskId = task.id;
    for (const artifact of task.artifacts || [])
      turn.artifacts.set(artifact.artifactId, artifact);
    recordProgress(turn, task.status);
    if (task.status?.message && !task.status.message.metadata?.[PROGRESS_KEY])
      turn.messageParts = task.status.message.parts || [];
    if (terminal.has(task.status?.state)) turn.endedAt = Date.now();
    text(turn.status, task.status?.state?.replace("TASK_STATE_", "") || "");
  }
  const update = event.data?.statusUpdate;
  if (update) {
    rememberTask({
      id: update.taskId,
      contextId: update.contextId,
      status: update.status,
    });
    turn.taskId = update.taskId;
    recordProgress(turn, update.status);
    if (terminal.has(update.status?.state)) turn.endedAt = Date.now();
    if (
      update.status?.message &&
      !update.status.message.metadata?.[PROGRESS_KEY]
    ) {
      turn.progress = (update.status.message.parts || [])
        .map((p) => p.text || "")
        .join("");
      turn.messageParts = update.status.message.parts || [];
    }
    text(turn.status, update.status?.state?.replace("TASK_STATE_", "") || "");
  }
  const artifact = event.data?.artifactUpdate;
  if (artifact) {
    turn.taskId = artifact.taskId;
    const previous = turn.artifacts.get(artifact.artifact.artifactId);
    turn.artifacts.set(artifact.artifact.artifactId, {
      ...artifact.artifact,
      parts:
        artifact.append && previous
          ? [...(previous.parts || []), ...(artifact.artifact.parts || [])]
          : artifact.artifact.parts || [],
    });
  }
  if (event.data?.message) {
    turn.messageParts = event.data.message.parts || [];
    if (event.data.message.contextId)
      $("context").value = event.data.message.contextId;
    text(turn.status, "已回复");
    setTaskState("已收到消息");
  }
  renderTurn(turn);
}
function setBusy(value) {
  busy = value;
  renderList();
  for (const id of [
    "send",
    "version",
    "endpoint",
    "connect",
    "disconnect",
    "refresh",
    "new-context",
  ])
    $(id).disabled = value;
  $("send").disabled = value || !$("endpoint").value;
  $("stop-stream").disabled = !value;
  $("recover-current").disabled = value || !currentTask?.id;
}
function renderAttachments() {
  $("attachments").replaceChildren();
  attachments.forEach((part, index) => {
    const chip = el("span", undefined, "attachment");
    chip.append(el("span", part.filename || part.url || "附件"));
    const button = el("button", "×");
    button.type = "button";
    button.setAttribute("aria-label", "移除附件");
    button.addEventListener("click", () => {
      attachments.splice(index, 1);
      renderAttachments();
    });
    chip.append(button);
    $("attachments").append(chip);
  });
}
$("upload").addEventListener("change", async () => {
  try {
    const files = [...$("upload").files];
    const existing = attachments.reduce(
      (n, p) => n + (p.raw ? Math.floor((p.raw.length * 3) / 4) : 0),
      0,
    );
    if (existing + files.reduce((n, f) => n + f.size, 0) > 8 * 1024 * 1024)
      throw new Error("文件总量不能超过 8 MiB");
    for (const file of files) {
      const media = file.type || "application/octet-stream";
      if (!mediaAllowed(media) && !$("force-test").checked)
        throw new Error(`Agent 未声明支持 ${media}（${file.name}）`);
    }
    const parts = await Promise.all(
      files.map(
        (file) =>
          new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () =>
              resolve({
                raw: reader.result.split(",")[1],
                filename: file.name,
                mediaType: file.type || "application/octet-stream",
              });
            reader.onerror = () => reject(new Error("文件读取失败"));
            reader.readAsDataURL(file);
          }),
      ),
    );
    attachments.push(...parts);
    renderAttachments();
  } catch (error) {
    notice(error.message);
  } finally {
    $("upload").value = "";
  }
});
$("add-url").addEventListener("click", () => {
  try {
    const url = new URL($("file-url").value);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      throw new Error("请输入不包含凭证的 http(s) URL");
    const media = $("file-media").value;
    if (!mediaAllowed(media) && !$("force-test").checked)
      throw new Error(`Agent 未声明支持 ${media}`);
    attachments.push({
      url: url.href,
      mediaType: media,
      filename: url.pathname.split("/").pop() || "attachment",
    });
    renderAttachments();
    $("file-url").value = "";
  } catch (error) {
    notice(error.message);
  }
});
function waitForPoll(signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Stopped", "AbortError"));
      return;
    }
    const stopped = () => {
      clearTimeout(timer);
      reject(new DOMException("Stopped", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stopped);
      resolve();
    }, 3000);
    signal.addEventListener("abort", stopped, { once: true });
  });
}
function taskFinished(task) {
  return (
    terminal.has(task?.status?.state) || interrupted.has(task?.status?.state)
  );
}
async function watchTask(turn, signal) {
  const id = turn.taskId;
  let streamFailures = 0,
    queryFailures = 0;
  while (!signal.aborted) {
    text($("watch-status"), `正在查询任务 ${id}；不会重新发送消息。`);
    let task;
    try {
      await perform(
        "GetTask",
        { id },
        (event) => {
          if (event.type === "result") {
            task = event.data;
            consumeChat(turn, { type: "task", data: task });
          }
        },
        signal,
      );
      queryFailures = 0;
    } catch (error) {
      if (
        error.name === "AbortError" ||
        error.code === "agent_auth" ||
        error.status === 401 ||
        error.status === 403 ||
        ++queryFailures >= 3
      )
        throw error;
      text(
        $("watch-status"),
        `查询中断，正在重试 ${queryFailures}/3；不会重新提交任务。`,
      );
      await waitForPoll(signal);
      continue;
    }
    if (!task)
      throw new Error("GetTask 未返回任务，请手动查询；消息不会重新提交。");
    if (taskFinished(task)) {
      text(
        $("watch-status"),
        interrupted.has(task.status?.state)
          ? "任务需要输入或认证；已停止自动接收。"
          : "任务已结束。",
      );
      return;
    }
    if (card.capabilities?.streaming && streamFailures < 3) {
      text($("watch-status"), `正在接收任务 ${id} 的更新；断线后重新查询。`);
      try {
        await perform(
          "SubscribeToTask",
          { id },
          (event) => consumeChat(turn, event),
          signal,
        );
      } catch (error) {
        if (
          error.name === "AbortError" ||
          error.code === "agent_auth" ||
          error.status === 401 ||
          error.status === 403
        )
          throw error;
        streamFailures++;
        text(
          $("watch-status"),
          `订阅中断，正在恢复；${streamFailures >= 3 ? "改为每 3 秒查询状态" : "稍后重新订阅"}。`,
        );
      }
    } else {
      text($("watch-status"), `每 3 秒查询任务 ${id}；停止接收不取消任务。`);
    }
    await waitForPoll(signal);
  }
}
async function sendAndWatch(turn, action, params, signal) {
  try {
    await perform(action, params, (event) => consumeChat(turn, event), signal);
  } catch (error) {
    // Only recover an accepted task; never retry SendMessage after an uncertain submission.
    if (
      !turn.taskId ||
      !$("auto-watch").checked ||
      error.name === "AbortError" ||
      error.code === "agent_auth" ||
      error.status === 401 ||
      error.status === 403
    )
      throw error;
    text($("watch-status"), "接收中断；正在查询已提交的任务，不会重复发送。");
    await watchTask(turn, signal);
    return;
  }
  if (turn.taskId && $("auto-watch").checked && !taskFinished(currentTask))
    await watchTask(turn, signal);
}
$("call-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  notice();
  if (
    !actionAllowed(
      $("send-mode").value === "stream"
        ? "SendStreamingMessage"
        : "SendMessage",
    )
  ) {
    notice("此操作未被 Card 声明支持，请先勾选允许测试选项");
    return;
  }
  let params, parts;
  try {
    parts = [...attachments];
    if ($("prompt").value.trim()) parts.unshift({ text: $("prompt").value });
    if ($("data-part").value.trim()) {
      if (!mediaAllowed("application/json") && !$("force-test").checked)
        throw new Error("Agent 未声明支持 application/json");
      parts.push({
        data: jsonValue("data-part"),
        mediaType: "application/json",
      });
    }
    if (!parts.length) throw new Error("请填写消息或添加附件");
    const extra = jsonValue("send-options");
    params = {
      ...extra,
      message: {
        messageId: crypto.randomUUID(),
        role: "ROLE_USER",
        parts,
        ...(extra.message || {}),
      },
      configuration: {
        ...(extra.configuration || {}),
        returnImmediately: $("send-mode").value === "background",
      },
    };
    if ($("context").value) params.message.contextId = $("context").value;
    if ($("continue-task").value)
      params.message.taskId = $("continue-task").value;
    target();
  } catch (error) {
    reportActionError(error);
    setTaskState("未发送 · 请查看输入框旁的提示");
    return;
  }
  const user = bubble("user");
  renderParts(params.message.parts, user.body, user.files, "");
  const turn = bubble("agent");
  activeTurn = turn;
  renderTurn(turn);
  setBusy(true);
  aborter = new AbortController();
  setTaskState("正在重新发现并提交…");
  try {
    await sendAndWatch(
      turn,
      $("send-mode").value === "stream"
        ? "SendStreamingMessage"
        : "SendMessage",
      params,
      aborter.signal,
    );
    attachments = [];
    renderAttachments();
    $("prompt").value = "";
    $("data-part").value = "";
    if (!turn.taskId) text(turn.status, "已回复");
  } catch (error) {
    if (error.name === "AbortError") {
      if (!turn.artifacts.size && !turn.messageParts.length)
        text(turn.body, "已停止接收；请通过任务管理查询远端任务。");
      text(turn.status, "已停止接收；远端任务可能仍在运行");
      setTaskState("已停止接收");
    } else {
      reportActionError(error);
      if (!turn.artifacts.size && !turn.messageParts.length)
        text(turn.body, "未能取得正常响应。请按下方提示处理后重试。");
      text(turn.status, "调用失败 · " + error.message);
      setTaskState("调用失败 · 请查看输入框旁的提示");
    }
  } finally {
    setBusy(false);
    aborter = null;
  }
});
$("call-form").addEventListener(
  "invalid",
  (event) => {
    event.preventDefault();
    notice(
      "未发送：消息表单中有无效字段，请检查附加文件 URL 或消息长度。未添加的 URL 可清空后重试。",
    );
    setTaskState("未发送 · 表单校验失败");
  },
  true,
);
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $("call-form").requestSubmit();
  }
});
$("stop-stream").addEventListener("click", () => aborter?.abort());
async function buttonAction(button, action, params, receive) {
  notice();
  if (!actionAllowed(action)) {
    notice("此操作未被 Card 声明支持，请先勾选允许测试选项");
    return;
  }
  button.disabled = true;
  try {
    await perform(action, params, receive);
  } catch (error) {
    reportActionError(error);
  } finally {
    button.disabled = !actionAllowed(action);
  }
}
function showTask(task) {
  viewedTask = task;
  if (!busy || activeTurn?.taskId === task.id) rememberTask(task);
  const root = $("task-view");
  root.replaceChildren();
  root.append(el("p", `${task.status?.state || ""} · ${task.id}`, "hint"));
  for (const message of task.history || []) {
    const box = el(
      "div",
      undefined,
      "bubble " + (message.role === "ROLE_USER" ? "user" : "agent"),
    );
    box.append(el("div", message.role, "role-label"));
    const body = el("div", undefined, "body"),
      files = el("div");
    box.append(body, files);
    renderParts(message.parts || [], body, files, task.id);
    root.append(box);
  }
  const result = el("div");
  for (const artifact of task.artifacts || []) {
    result.append(el("h4", artifact.name || artifact.artifactId));
    const body = el("div"),
      files = el("div");
    renderParts(artifact.parts || [], body, files, task.id);
    result.append(body, files);
  }
  root.append(result);
  const raw = el("details");
  raw.append(
    el("summary", "完整任务 JSON"),
    el("pre", JSON.stringify(task, null, 2)),
  );
  root.append(raw);
}
function listResult(data) {
  nextPage = data.nextPageToken || "";
  $("next-tasks").disabled = !nextPage;
  $("task-list").replaceChildren();
  for (const task of data.tasks || []) {
    const row = el("div", undefined, "task-row"),
      label = el("div");
    label.append(
      el("code", task.id),
      el(
        "small",
        `${task.status?.state?.replace("TASK_STATE_", "") || ""} · ${task.contextId || ""}`,
      ),
    );
    const button = el("button", "查看", "secondary compact");
    button.addEventListener("click", () => {
      $("task-id").value = task.id;
      fetchTask();
    });
    row.append(label, button);
    $("task-list").append(row);
  }
  if (!$("task-list").children.length)
    $("task-list").append(el("p", "没有符合条件的任务。", "hint"));
}
function taskParams() {
  const id = $("task-id").value.trim();
  if (!id) throw new Error("请填写 Task ID");
  const params = { id };
  if ($("history-length").value !== "")
    params.historyLength = Number($("history-length").value);
  return params;
}
async function fetchTask() {
  try {
    const params = taskParams();
    await buttonAction($("get-task"), "GetTask", params, (event) => {
      if (event.type === "result") showTask(event.data);
    });
  } catch (error) {
    notice(error.message);
  }
}
async function fetchTasks(page = "") {
  const params = {
    pageSize: Number($("task-size").value),
    includeArtifacts: $("include-artifacts").checked,
  };
  if ($("task-context").value) params.contextId = $("task-context").value;
  if ($("task-status").value) params.status = $("task-status").value;
  if (page) params.pageToken = page;
  await buttonAction($("next-tasks"), "ListTasks", params, (event) => {
    if (event.type === "result") listResult(event.data);
  });
  $("next-tasks").disabled = !nextPage;
}
$("tasks-form").addEventListener("submit", (event) => {
  event.preventDefault();
  fetchTasks().catch((error) => notice(error.message));
});
$("next-tasks").addEventListener("click", () =>
  fetchTasks(nextPage).catch((error) => notice(error.message)),
);
$("get-task").addEventListener("click", fetchTask);
async function cancelTask(id, button) {
  if (!id) {
    notice("请先选择任务");
    return;
  }
  await buttonAction(button, "CancelTask", { id }, (event) => {
    if (event.type === "result") {
      showTask(event.data);
      if (activeTurn && activeTurn.taskId === id)
        consumeChat(activeTurn, { type: "task", data: event.data });
    }
  });
}
$("cancel-task").addEventListener("click", () =>
  cancelTask($("task-id").value, $("cancel-task")),
);
$("cancel-current").addEventListener("click", () =>
  cancelTask(currentTask?.id, $("cancel-current")),
);
$("resume-task").addEventListener("click", () => {
  if (!viewedTask || viewedTask.id !== $("task-id").value) {
    notice("请先查看这个任务");
    return;
  }
  if (busy) {
    notice("请先停止当前接收，再切换任务会话");
    return;
  }
  $("context").value = viewedTask.contextId || "";
  $("continue-task").value = terminal.has(viewedTask.status?.state)
    ? ""
    : viewedTask.id;
  setTab("chat");
  $("prompt").focus();
});
async function resumeTask(id) {
  if (busy || !id) return;
  notice();
  try {
    setTab("chat");
    const turn = activeTurn?.taskId === id ? activeTurn : bubble("agent");
    turn.taskId = id;
    activeTurn = turn;
    setBusy(true);
    aborter = new AbortController();
    await watchTask(turn, aborter.signal);
  } catch (error) {
    if (error.name === "AbortError")
      text(
        $("watch-status"),
        "已停止接收；远端任务可能仍在运行，可查询 / 恢复接收。",
      );
    else reportActionError(error);
  } finally {
    setBusy(false);
    aborter = null;
  }
}
$("subscribe-task").addEventListener("click", () =>
  resumeTask($("task-id").value),
);
$("recover-current").addEventListener("click", () =>
  resumeTask(currentTask?.id),
);
for (const state of states)
  $("task-status").add(new Option(state, "TASK_STATE_" + state));
for (const [name, action] of [
  ["create", "CreateTaskPushNotificationConfig"],
  ["get", "GetTaskPushNotificationConfig"],
  ["list", "ListTaskPushNotificationConfigs"],
  ["delete", "DeleteTaskPushNotificationConfig"],
])
  $("push-" + name).addEventListener("click", () => {
    try {
      const taskId = $("push-task").value,
        id = $("push-id").value;
      if (!taskId) throw new Error("请填写 Task ID");
      const params = { taskId };
      if (id && name !== "list") params.id = id;
      if (["get", "delete"].includes(name) && !id)
        throw new Error("请填写配置 ID");
      if (name === "create") {
        params.url = $("push-url").value;
        if (!params.url) throw new Error("请填写回调 URL");
        if ($("push-token").value) params.token = $("push-token").value;
        if ($("push-scheme").value)
          params.authentication = {
            scheme: $("push-scheme").value,
            credentials: $("push-credentials").value,
          };
      }
      if (name === "list" && $("push-page").value)
        params.pageToken = $("push-page").value;
      buttonAction($("push-" + name), action, params, (event) => {
        if (event.type === "result") {
          $("push-result").hidden = false;
          text(
            $("push-result"),
            JSON.stringify(
              event.data,
              (k, v) => (/^(token|credentials)$/i.test(k) ? "••••••" : v),
              2,
            ),
          );
          if (event.data.id) $("push-id").value = event.data.id;
          if (name === "list")
            $("push-page").value = event.data.nextPageToken || "";
        }
      });
    } catch (error) {
      notice(error.message);
    }
  });
function operationTemplate() {
  const action = $("operation").value,
    id = $("task-id").value || "<task-id>";
  let params = {};
  if (action.startsWith("Send"))
    params = {
      message: {
        messageId: crypto.randomUUID(),
        role: "ROLE_USER",
        parts: [{ text: "Hello" }],
      },
    };
  else if (["GetTask", "CancelTask", "SubscribeToTask"].includes(action))
    params = { id };
  else if (action === "ListTasks")
    params = { pageSize: 20, includeArtifacts: true };
  else if (action.includes("PushNotification")) {
    params = { taskId: id };
    if (action === "CreateTaskPushNotificationConfig")
      params.url = "https://your-application.example/a2a/events";
    else if (action !== "ListTaskPushNotificationConfigs")
      params.id = "<config-id>";
  }
  $("operation-params").value = JSON.stringify(params, null, 2);
  operationSupport();
}

$("operation").addEventListener("change", operationTemplate);
operationTemplate();
let protocolAborter = null;
$("stop-operation").addEventListener("click", () => protocolAborter?.abort());
$("run-operation").addEventListener("click", async () => {
  notice();
  try {
    if (!actionAllowed($("operation").value))
      throw new Error("此操作未被 Card 声明支持，请勾选允许测试选项后重试");
    const params = jsonValue("operation-params");
    $("operation-result").hidden = false;
    text($("operation-result"), "");
    $("run-operation").disabled = true;
    $("stop-operation").disabled = false;
    protocolAborter = new AbortController();
    await perform(
      $("operation").value,
      params,
      (event) => {
        $("operation-result").textContent +=
          JSON.stringify(
            event.data,
            (k, v) => (/^(token|credentials)$/i.test(k) ? "••••••" : v),
            2,
          ) + "\n";
      },
      protocolAborter.signal,
    );
  } catch (error) {
    notice(
      error.name === "AbortError"
        ? "已停止接收；远端任务不受影响"
        : error.message,
    );
  } finally {
    protocolAborter = null;
    $("run-operation").disabled = false;
    $("stop-operation").disabled = true;
  }
});
$("extended-card").addEventListener("click", () =>
  buttonAction($("extended-card"), "GetExtendedAgentCard", {}, (event) => {
    if (event.type === "result") {
      $("operation-result").hidden = false;
      text($("operation-result"), JSON.stringify(event.data, null, 2));
    }
  }),
);
api("/api/config")
  .then((config) => {
    if (!config.protocol)
      throw new Error(
        "后端仍在运行旧版本：请停止 VS Code 调试后重新 F5 启动，再刷新页面。",
      );
    protocolOperations = config.protocol.operations;
    operations = protocolOperations.map((item) => item.name);
    $("operation").replaceChildren();
    for (const action of operations)
      $("operation").add(new Option(action, action));
    operationTemplate();
    text(
      $("protocol-source"),
      `协议来源：${config.protocol.source} · A2A ${config.protocol.version} / ${config.protocol.binding}；能力来源：Nacos 返回的当前 AgentCard。`,
    );
    $("registry-url").value = config.url;
    $("saved-registry-label").hidden = !config.saved_available;
    $("saved-registry").checked = config.saved_available;
    savedFields();
    $("saved-agent-label").hidden = !config.saved_agent_token;
    $("saved-agent").checked = config.saved_agent_token;
    $("agent-token").disabled = config.saved_agent_token;
  })
  .catch((error) => notice(error.message));
