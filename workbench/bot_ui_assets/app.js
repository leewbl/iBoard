'use strict';
const byId = (id) => document.getElementById(id);
const input = byId('access');
const actions = byId('actions');
const drain = byId('drain');
const refresh = byId('refresh');
const words = {
  zh: {
    controls: '触发操作', access: '面板访问密钥', outcome: '最近请求', run: '运行', idle: '等待选择操作。',
    waiting: '请求已接收，正在处理。', sent: '外部例程已接收请求。', complete: '操作已完成。',
    retained: '发送失败，请求已保留。', retry: '处理保留的请求', failed: '操作未完成。', incorrect: '访问密钥无效。',
    unavailable: '面板暂时无法完成请求。', interrupted: '操作已中断。请查看任务状态。',
    refresh: '查看结果', task: {active: '任务正在运行。', 'needs-inspection': '任务实际执行状态待核对。',
      paused: '任务已暂停。', held: '任务已被挂起。', complete: '任务已完成。'},
    stages: (n) => `已完成 ${n} 个工作流阶段。`, delivery: (sent, remaining) => `已送达 ${sent} 项，仍保留 ${remaining} 项。`,
  },
  en: {
    controls: 'Actions', access: 'Panel access key', outcome: 'Latest request', run: 'Run', idle: 'Choose an action.',
    waiting: 'Request accepted. Processing it now.', sent: 'The external routine accepted the request.',
    complete: 'Action complete.', retained: 'The request was retained for delivery.', retry: 'Process retained requests',
    failed: 'The action could not complete.', incorrect: 'The access key is invalid.',
    unavailable: 'The panel could not complete the request.', interrupted: 'The action stopped. Check the task state.',
    refresh: 'Check result', task: {active: 'The task is running.', 'needs-inspection': 'The task state needs inspection.',
      paused: 'The task is paused.', held: 'The task is on hold.', complete: 'The task is complete.'},
    stages: (n) => `${n} workflow ${n === 1 ? 'stage' : 'stages'} completed.`,
    delivery: (sent, remaining) => `${sent} delivered. ${remaining} retained.`,
  },
};
let language = words.zh;
let config;
let latest;

function message(state, detail = '') {
  byId('state').textContent = state;
  byId('detail').textContent = detail;
  byId('detail').hidden = !detail;
}

function api(path, key, body) {
  const headers = {Authorization: `Bearer ${key}`};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return fetch(path, {method: body === undefined ? 'GET' : 'POST', headers, credentials: 'omit',
    body: body === undefined ? undefined : JSON.stringify(body)});
}

function requestId() {
  return [...crypto.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function observe(id, key, external = false) {
  refresh.hidden = true;
  for (let step = 0; step < 25; step++) {
    const response = await api('/api/events/' + id, key);
    if (!response.ok) throw new Error('Status unavailable');
    const record = await response.json();
    if (record.wake === 'complete') {
      const result = record.result || {};
      let detail = language.task[result.execution] || '';
      if (Number.isInteger(result.stages)) detail = language.stages(result.stages);
      if (Number.isInteger(result.accepted) && Number.isInteger(result.retained)) {
        detail = language.delivery(result.accepted, result.retained);
        drain.hidden = result.retained === 0;
      }
      message(language.complete, detail);
      return;
    }
    if (record.wake === 'failed' || record.wake === 'interrupted') {
      message(record.wake === 'failed' ? language.failed : language.interrupted);
      return;
    }
    if (record.forwarding === 'retained') {
      message(language.retained);
      drain.hidden = false;
      return;
    }
    if (external && record.forwarding === 'accepted') {
      message(language.sent);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  refresh.hidden = false;
}

async function invoke(path, body) {
  if (!input.reportValidity()) return;
  const allButtons = [...document.querySelectorAll('main button')];
  const key = input.value;
  allButtons.forEach((button) => { button.disabled = true; });
  drain.hidden = true;
  refresh.hidden = true;
  message(language.waiting);
  try {
    const response = await api(path, key, body);
    if (response.status === 401) {
      message(language.incorrect);
      input.focus();
      return;
    }
    if (!response.ok) throw new Error('Request rejected');
    const result = await response.json();
    if (result.state === 'retained') {
      message(language.retained);
      drain.hidden = false;
      return;
    }
    const external = config.provider === 'cursor' && path === '/api/actions';
    if (external && result.state === 'accepted') {
      message(language.sent);
      return;
    }
    latest = {id: result.id, external};
    await observe(latest.id, key, latest.external);
  } catch {
    message(language.unavailable);
  } finally {
    allButtons.forEach((button) => { button.disabled = false; });
  }
}

async function initialize() {
  try {
    const response = await fetch('/api/options', {credentials: 'omit'});
    if (!response.ok) throw new Error('Options unavailable');
    config = await response.json();
    language = words[config.locale] || words.en;
    document.documentElement.lang = config.locale === 'zh' ? 'zh-CN' : 'en';
    document.title = config.title + ' · iBoard';
    for (const [id, label] of Object.entries({title: config.title, controls: language.controls,
      'access-label': language.access, outcome: language.outcome})) byId(id).textContent = label;
    message(language.idle);
    drain.textContent = language.retry;
    drain.addEventListener('click', () => invoke('/api/drain', {}));
    refresh.textContent = language.refresh;
    refresh.addEventListener('click', async () => {
      if (!latest || !input.reportValidity()) return;
      const buttons = [...document.querySelectorAll('main button')];
      buttons.forEach((button) => { button.disabled = true; });
      try {
        await observe(latest.id, input.value, latest.external);
      } catch {
        message(language.unavailable);
        refresh.hidden = false;
      } finally {
        buttons.forEach((button) => { button.disabled = false; });
      }
    });
    for (const action of config.actions) {
      const row = document.createElement('article');
      row.className = 'action';
      const summary = document.createElement('div');
      const title = document.createElement('h3');
      const description = document.createElement('p');
      const button = document.createElement('button');
      title.textContent = action.label;
      description.textContent = action.description;
      button.textContent = language.run;
      button.type = 'button';
      button.setAttribute('aria-label', language.run + ' ' + action.label);
      button.addEventListener('click', () => invoke('/api/actions', {action: action.id, request_id: requestId(), values: {}}));
      summary.append(title, description);
      row.append(summary, button);
      actions.append(row);
    }
  } catch {
    message(language.unavailable);
  }
}

initialize();
