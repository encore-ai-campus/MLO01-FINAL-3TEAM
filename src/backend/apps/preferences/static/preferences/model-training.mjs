export async function refreshStatus(url, {request, update, delayed, unavailable, schedule}) {
  try {
    const response = await request(url);
    if (response.redirected || [401, 403, 404].includes(response.status)) {
      unavailable();
      return;
    }
    if (!response.ok) throw new Error("Status unavailable");
    const snapshot = await response.json();
    update(snapshot);
    if (snapshot.terminal) return;
  } catch {
    delayed();
  }
  schedule(() => refreshStatus(url, {request, update, delayed, unavailable, schedule}), 5000);
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function steps(points) {
  const line = element("ol", "training-steps");
  for (const {label, state} of points) {
    const point = element("li", `training-step is-${state}`, label);
    if (state === "active" || state === "failed") point.setAttribute("aria-current", "step");
    line.append(point);
  }
  return line;
}

function summary(values) {
  const node = element("div", "training-summary");
  for (const [label, value] of values) {
    if (value !== null && value !== undefined) node.append(element("p", "", `${label} · ${value}`));
  }
  return node;
}

function renderProgress(root, snapshot) {
  if (!snapshot) return;
  root.querySelector("[data-training-label]").textContent = snapshot.label;
  root.querySelector("[data-training-notice]").textContent = snapshot.notice || "";
  root.querySelector("[data-training-overall]").replaceChildren(steps(snapshot.overall));
  const links = root.querySelector("[data-training-links]");
  links.replaceChildren();
  for (const [label, url] of Object.entries(snapshot.links || {})) {
    const link = element("a", "desk-button", label);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    links.append(link);
  }
  const cards = root.querySelector("[data-training-cards]");
  cards.replaceChildren();
  const result = snapshot.result || {};
  const paths = snapshot.paths || {};
  for (const [index, card] of snapshot.cards.entries()) {
    const section = element("section", "desk-card");
    section.append(element("h2", "", card.title));
    for (const row of card.rows) {
      const operation = element("div", "training-operation");
      operation.append(element("h3", "", row.title), steps(row.points));
      if (row.message) operation.append(element("p", "training-operation-status", row.message));
      section.append(operation);
    }
    const values = index === 0 ? [
      ["학습셋", result.train_version && `${result.train_version} (${result.train_count}건)`],
      ["평가셋", result.eval_version && `${result.eval_version} (${result.eval_count}건)`],
      ["학습셋 저장 경로", paths.train], ["평가셋 저장 경로", paths.eval],
    ] : [
      ["등록 모델", result.registry_version && `${result.model_name}/${result.registry_version}`],
      ["감성 Macro-F1", result.sentiment_macro_f1?.toFixed(3)],
      ["카테고리 Macro-F1", result.category_macro_f1?.toFixed(3)],
      ["모델 저장 경로", paths.model],
    ];
    const resultNode = summary(values);
    if (resultNode.childElementCount) section.append(resultNode);
    cards.append(section);
  }
}

async function loadHistory(root) {
  const notice = root.querySelector("[data-history-notice]");
  try {
    const response = await fetch(root.dataset.trainingHistory);
    if (!response.ok || response.redirected) throw new Error("History unavailable");
    const {runs} = await response.json();
    const list = root.querySelector("[data-history-list]");
    const format = new Intl.DateTimeFormat("ko-KR", {dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Seoul"});
    for (const run of runs) {
      const row = element("li");
      const when = run.submitted_at ? format.format(new Date(run.submitted_at)) : "";
      const link = element("a", "", [when, run.label, "상세 보기"].filter(Boolean).join(" · "));
      link.href = run.url;
      row.append(link, element("p", "", [run.model, run.evaluation].filter(Boolean).join(" · ")));
      list.append(row);
    }
    notice.textContent = runs.length ? "" : "최근 학습 실행이 없습니다.";
  } catch {
    notice.textContent = "최근 실행을 조회하지 못했습니다.";
  }
}

if (typeof document !== "undefined") {
  const root = document.querySelector("[data-training-progress]");
  if (root) {
    const initial = JSON.parse(document.getElementById("training-snapshot").textContent);
    if (root.dataset.statusUrl) {
      const notice = root.querySelector("[data-training-notice]");
      renderProgress(root, initial);
      let last = JSON.stringify(initial);
      if (!initial?.terminal) setTimeout(() => refreshStatus(root.dataset.statusUrl, {
        request: url => fetch(url, {cache: "no-store"}),
        update: snapshot => {
          const next = JSON.stringify(snapshot);
          if (last !== next) renderProgress(root, snapshot);
          else notice.textContent = snapshot.notice || "";
          last = next;
        },
        delayed: () => { notice.textContent = "상태 조회 지연"; },
        unavailable: () => {
          root.querySelector("[data-training-overall]").replaceChildren();
          root.querySelector("[data-training-cards]").replaceChildren();
          root.querySelector("[data-training-links]").replaceChildren();
          root.querySelector("[data-training-label]").textContent = "";
          root.querySelector(".training-run-name")?.remove();
          notice.textContent = "학습 실행을 조회할 수 없습니다.";
        },
        schedule: setTimeout,
      }), initial ? 5000 : 0);
    } else {
      const history = document.querySelector("[data-training-history]");
      if (history) loadHistory(history);
      document.querySelector("[data-training-form]")?.addEventListener("submit", () => {
        renderProgress(root, initial);
        root.hidden = false;
        document.querySelector("[data-training-setup]").hidden = true;
        if (history) history.hidden = true;
      });
    }
  }
}
