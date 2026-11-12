// Exercise the actual UI validation/feedback functions with the page's real element IDs.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
function page() {
  const nodes = new Map(
    [...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [
      id,
      {
        textContent: "",
        hidden: true,
        value: "",
        checked: false,
        disabled: false,
        attributes: {},
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        removeAttribute(name) {
          delete this.attributes[name];
        },
        addEventListener() {},
        scrollIntoView() {},
        focus() {},
      },
    ]),
  );
  const $ = (id) => {
    assert.ok(nodes.has(id), `Missing page element: ${id}`);
    return nodes.get(id);
  };
  $("endpoint").value = "https://agent.example/a2a";
  $("version").value = "1";
  const context = vm.createContext({
    $,
    text: (node, value) => {
      node.textContent = value;
    },
    requiresBearer: () => true,
    chosen: { agentName: "example" },
  });
  vm.runInContext(
    source.slice(
      source.indexOf("function notice("),
      source.indexOf("function jsonValue("),
    ) +
      source.slice(
        source.indexOf("function reportActionError("),
        source.indexOf("function logEvent("),
      ),
    context,
  );
  return { $, context };
}
test("missing token is shown beside composer and prevents a request target", () => {
  const { $, context } = page();
  assert.throws(
    () => vm.runInContext("target()", context),
    (error) => {
      assert.equal(error.code, "agent_token_missing");
      context.failure = error;
      return true;
    },
  );
  vm.runInContext("reportActionError(failure)", context);
  assert.equal($("chat-notice").hidden, false);
  assert.match($("chat-notice").textContent, /Agent token/);
  assert.match($("token-status").textContent, /任务未提交/);
  assert.equal($("agent-token").attributes["aria-invalid"], "true");
});
test("anonymous Card allows an empty token and explains that no Bearer token is required", () => {
  const { $, context } = page();
  context.requiresBearer = () => false;
  const target = vm.runInContext("target()", context);
  assert.equal(target.token, "");
  vm.runInContext("tokenInputChanged()", context);
  assert.match($("chat-auth-status").textContent, /未要求 Bearer token/);
  assert.equal($("agent-token").attributes["aria-invalid"], undefined);
});
for (const status of [401, 403])
  test(`HTTP ${status} explains the failure inside current panels`, () => {
    const { $, context } = page();
    context.failure = {
      code: "agent_auth",
      status,
      message: `目标 Agent 拒绝调用（HTTP ${status}），请联系部署者`,
    };
    vm.runInContext("reportActionError(failure)", context);
    for (const id of [
      "chat-notice",
      "tasks-notice",
      "push-notice",
      "protocol-notice",
    ]) {
      assert.equal($(id).hidden, false);
      assert.match($(id).textContent, new RegExp(String(status)));
    }
    assert.match($("chat-auth-status").textContent, new RegExp(String(status)));
  });
test("editing a token clears rejected field state but does not claim it is valid", () => {
  const { $, context } = page();
  $("agent-token").value = "test-value";
  $("agent-token").setAttribute("aria-invalid", "true");
  vm.runInContext("tokenInputChanged()", context);
  assert.equal($("agent-token").attributes["aria-invalid"], undefined);
  assert.match($("token-status").textContent, /尚未验证/);
});
test("Bearer prefix is rejected with a useful format explanation", () => {
  const { $, context } = page();
  $("agent-token").value = "Bearer test-value";
  assert.throws(
    () => vm.runInContext("target()", context),
    (error) => {
      assert.equal(error.code, "agent_token_format");
      assert.match(error.message, /Bearer 前缀/);
      return true;
    },
  );
});

function taskClient(perform, streaming = true) {
  const state = { taskId: "task-1" };
  const status = { textContent: "" };
  const context = vm.createContext({
    perform,
    card: { capabilities: { streaming } },
    currentTask: { id: "task-1", status: { state: "TASK_STATE_WORKING" } },
    terminal: new Set(["TASK_STATE_COMPLETED", "TASK_STATE_FAILED"]),
    interrupted: new Set([
      "TASK_STATE_INPUT_REQUIRED",
      "TASK_STATE_AUTH_REQUIRED",
    ]),
    $: (id) => (id === "auto-watch" ? { checked: true } : status),
    text: (node, value) => {
      node.textContent = value;
    },
    consumeChat: (turn, event) => {
      const task =
        event.data?.task || (event.type === "task" ? event.data : null);
      if (task) {
        turn.taskId = task.id;
        context.currentTask = task;
      }
    },
    setTimeout,
    clearTimeout,
    DOMException,
    state,
    signal: new AbortController().signal,
  });
  vm.runInContext(
    source.slice(
      source.indexOf("function waitForPoll("),
      source.indexOf('$("call-form").addEventListener("submit", async'),
    ),
    context,
  );
  context.waitForPoll = async () => {};
  return context;
}
test("background submission queries then subscribes without sending another message", async () => {
  const calls = [];
  const context = taskClient(async (action, params, receive) => {
    calls.push(action);
    if (action === "GetTask")
      receive({
        type: "result",
        data: {
          id: "task-1",
          status: {
            state:
              calls.length > 3 ? "TASK_STATE_COMPLETED" : "TASK_STATE_WORKING",
          },
        },
      });
    if (action === "SubscribeToTask") throw new Error("connection lost");
  });
  await vm.runInContext(
    'sendAndWatch(state, "SendMessage", {}, signal)',
    context,
  );
  assert.equal(calls.filter((x) => x === "SendMessage").length, 1);
  assert.ok(calls.includes("GetTask"));
  assert.ok(calls.includes("SubscribeToTask"));
});
test("accepted streaming task recovers by id; uncertain submission is never retried", async () => {
  const calls = [];
  const context = taskClient(async (action, params, receive) => {
    calls.push(action);
    if (action === "SendStreamingMessage") throw new Error("connection lost");
    receive({
      type: "result",
      data: { id: "task-1", status: { state: "TASK_STATE_COMPLETED" } },
    });
  });
  await vm.runInContext(
    'sendAndWatch(state, "SendStreamingMessage", {}, signal)',
    context,
  );
  assert.deepEqual(calls, ["SendStreamingMessage", "GetTask"]);
  calls.length = 0;
  context.state.taskId = "";
  await assert.rejects(
    vm.runInContext(
      'sendAndWatch(state, "SendStreamingMessage", {}, signal)',
      context,
    ),
  );
  assert.deepEqual(calls, ["SendStreamingMessage"]);
});
test("authentication errors stop recovery immediately", async () => {
  const calls = [];
  const context = taskClient(async (action) => {
    calls.push(action);
    throw Object.assign(new Error("denied"), {
      code: "agent_auth",
      status: 401,
    });
  });
  await assert.rejects(
    vm.runInContext('sendAndWatch(state, "SendMessage", {}, signal)', context),
  );
  assert.deepEqual(calls, ["SendMessage"]);
});
test("three failed subscriptions fall back to polling and stop for required input", async () => {
  let reads = 0,
    subscriptions = 0;
  const context = taskClient(async (action, params, receive) => {
    if (action === "GetTask")
      receive({
        type: "result",
        data: {
          id: "task-1",
          status: {
            state:
              ++reads === 5
                ? "TASK_STATE_INPUT_REQUIRED"
                : "TASK_STATE_WORKING",
          },
        },
      });
    if (action === "SubscribeToTask") {
      subscriptions++;
      throw new Error("unsupported");
    }
  });
  await vm.runInContext("watchTask(state, signal)", context);
  assert.equal(subscriptions, 3);
  assert.equal(reads, 5);
});
test("stopping observation does not issue a cancel or send", async () => {
  const calls = [];
  const context = taskClient(async (action) => calls.push(action));
  const controller = new AbortController();
  controller.abort();
  context.signal = controller.signal;
  await vm.runInContext("watchTask(state, signal)", context);
  assert.deepEqual(calls, []);
});
test("tool progress updates one retained row per step without replacing the final answer", () => {
  function element(tag, content = "") {
    return {
      tag,
      textContent: content,
      children: [],
      append(...nodes) {
        this.children.push(...nodes);
      },
      get childElementCount() {
        return this.children.length;
      },
    };
  }
  const turn = {
    history: { hidden: true },
    stepRows: new Map(),
    statusKeys: new Set(),
    steps: element("ol"),
    summary: element("summary"),
    messageParts: [{ text: "final answer" }],
  };
  const context = vm.createContext({
    PROGRESS_KEY: "urn:design-agent:a2a:progress:v1",
    el: element,
    text: (n, v) => (n.textContent = v),
    Date,
    turn,
  });
  vm.runInContext(
    source.slice(
      source.indexOf("function recordProgress("),
      source.indexOf("function updateTiming("),
    ),
    context,
  );
  for (const state of ["running", "completed"]) {
    context.status = {
      message: {
        parts: [{ text: "tool" }],
        metadata: {
          "urn:design-agent:a2a:progress:v1": {
            stepId: "1",
            tool: "render",
            state,
            elapsedSeconds: 1,
          },
        },
      },
    };
    vm.runInContext("recordProgress(turn, status)", context);
  }
  assert.equal(turn.steps.children.length, 1);
  assert.match(turn.steps.children[0].textContent, /已完成/);
  assert.equal(turn.history.hidden, false);
  assert.equal(turn.messageParts[0].text, "final answer");
});

test("completed follow-up clears the old task id while preserving the conversation", () => {
  const { $, context } = page();
  context.currentTask = null;
  context.terminal = new Set([
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
  ]);
  context.interrupted = new Set([
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
  ]);
  context.setTaskState = () => {};
  vm.runInContext(
    source.slice(
      source.indexOf("function rememberTask("),
      source.indexOf("function consumeChat("),
    ),
    context,
  );
  for (const state of [
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
  ]) {
    context.task = { id: "task-1", contextId: "context-1", status: { state } };
    vm.runInContext("rememberTask(task)", context);
    assert.equal($("continue-task").value, "task-1");
    context.task.status.state = "TASK_STATE_WORKING";
    vm.runInContext("rememberTask(task)", context);
    assert.equal($("continue-task").value, "task-1");
    context.task.status.state = "TASK_STATE_COMPLETED";
    vm.runInContext("rememberTask(task)", context);
    assert.equal($("continue-task").value, "");
    assert.equal($("context").value, "context-1");
  }
});
