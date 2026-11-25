(() => {
  "use strict";

  const TOPICS = {
    role_title: "Должность",
    responsibilities: "Обязанности",
    requirements: "Требования",
    compensation: "Оплата",
    schedule: "График",
    location: "Место работы",
    employment_type: "Занятость",
  };

  const TYPES = {
    missing: "Пропуск",
    ambiguity: "Неоднозначность",
    contradiction: "Противоречие",
  };

  const TYPE_ORDER = { contradiction: 0, missing: 1, ambiguity: 2 };
  const GRADE_ORDER = ["A", "B", "C", "D"];

  const state = {
    report: [],
    summary: null,
    query: "",
    folder: "all",
    sort: "score_desc",
    selected: null,
    goodIds: new Set(),
    pollTimer: null,
    toastTimer: null,
    runBusy: false,
    logOpen: false,
  };

  const dom = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    [
      "search", "folders", "count-all", "count-good", "sort", "list", "list-count",
      "detail", "summary-line", "status-dot", "data-status-text",
      "run-limit", "btn-check", "btn-live", "btn-refresh",
      "toast", "toast-text", "toast-spinner", "toast-log-btn", "toast-log",
    ].forEach((id) => {
      dom[id] = document.getElementById(id);
    });

    dom["btn-refresh"].addEventListener("click", refreshData);
    dom["btn-check"].addEventListener("click", () => startRun("check"));
    dom["btn-live"].addEventListener("click", () => startRun("live"));

    dom.search.addEventListener("input", (event) => {
      state.query = event.target.value.trim().toLowerCase();
      renderList();
    });

    dom.folders.addEventListener("click", (event) => {
      const button = event.target.closest("[data-folder]");
      if (!button) return;
      state.folder = button.dataset.folder;
      dom.folders.querySelectorAll(".filter").forEach((filter) => {
        filter.classList.toggle("active", filter === button);
      });
      renderList();
    });

    dom.sort.addEventListener("change", (event) => {
      state.sort = event.target.value;
      renderList();
    });

    dom["toast-log-btn"].addEventListener("click", () => {
      state.logOpen = !state.logOpen;
      dom["toast-log"].classList.toggle("hidden", !state.logOpen);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (state.logOpen) {
          state.logOpen = false;
          dom["toast-log"].classList.add("hidden");
        } else if (document.body.classList.contains("detail-open")) {
          closeDetail();
        }
      }
      if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        event.preventDefault();
        dom.search.focus();
      }
    });

    refreshData();
  }

  // ------------------------------------------------------------- данные

  async function fetchJSON(url, options) {
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error((payload && payload.error) || `HTTP ${response.status}`);
    }
    return payload;
  }

  async function refreshData() {
    try {
      const [summary, report] = await Promise.all([
        fetchJSON("/api/summary"),
        fetchJSON("/api/report"),
      ]);
      state.summary = summary;
      state.report = Array.isArray(report) ? report : [];
      state.goodIds = new Set(
        state.report.filter(isGood).map((item) => item.vacancy_id)
      );
      if (state.selected && !state.report.some((item) => item.vacancy_id === state.selected)) {
        state.selected = null;
        document.body.classList.remove("detail-open");
      }

      renderAll();

      if (summary && summary.run && summary.run.running) {
        updateToast(summary.run);
        startPolling();
      }
    } catch (error) {
      setStatus(`ошибка загрузки: ${error.message}`, "warn");
      dom["summary-line"].textContent = "Не удалось загрузить данные отчёта.";
    }
  }

  function isGood(item) {
    const limit = state.summary ? state.summary.good_max_issues : 1;
    return !item.analysis_error && (item.issues || []).length <= limit;
  }

  function setStatus(text, mode) {
    dom["data-status-text"].textContent = text;
    dom["status-dot"].className = "dot" + (mode ? " " + mode : "");
  }

  function renderAll() {
    const summary = state.summary;

    if (!summary || summary.total === 0) {
      setStatus("нет данных", "warn");
      dom["summary-line"].textContent = "Отчёт ещё не сформирован — запустите проверочный или живой прогон.";
    } else {
      setStatus(`данные от ${formatDateTime(summary.generated_at)}`, "ok");
      const good = summary.good_count;
      dom["summary-line"].textContent =
        `${summary.total} ${plural(summary.total, ["вакансия", "вакансии", "вакансий"])}` +
        ` · средний балл ${summary.avg_score}` +
        ` · ${good} ${plural(good, ["готова", "готовы", "готовы"])} к публикации` +
        ` · ${summary.issues_total} ${plural(summary.issues_total, ["замечание", "замечания", "замечаний"])}`;
    }

    dom["count-all"].textContent = summary && summary.total ? String(summary.total) : "";
    dom["count-good"].textContent = summary && summary.good_count ? String(summary.good_count) : "";

    renderList();
    renderDetail();
  }

  // ------------------------------------------------------------- список

  function filteredRows() {
    let rows = state.report.filter((item) => {
      if (state.folder === "good" && !state.goodIds.has(item.vacancy_id)) return false;
      if (state.folder !== "all" && state.folder !== "good" && item.grade !== state.folder) return false;
      if (!state.query) return true;

      const card = item.vacancy || {};
      const haystack = [
        card.job_name,
        card.company,
        card.region,
        card.schedule,
        item.vacancy_id,
        item.salary_analysis?.label,
        item.schedule_analysis?.type_label,
        ...(card.locations || []),
        ...(item.score_components || []).map((component) => component.label),
        ...(item.issues || []).map((issue) => `${issue.problem} ${issue.quote} ${issue.recommendation}`),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(state.query);
    });

    const sorters = {
      score_desc: (a, b) => (b.score ?? -1) - (a.score ?? -1),
      score_asc: (a, b) => (a.score ?? 101) - (b.score ?? 101),
      issues_desc: (a, b) => (b.issues || []).length - (a.issues || []).length,
      name_asc: (a, b) => (a.vacancy?.job_name || "").localeCompare(b.vacancy?.job_name || "", "ru"),
    };
    return rows.slice().sort(sorters[state.sort] || sorters.score_desc);
  }

  function renderList() {
    const list = dom.list;
    list.replaceChildren();

    if (state.report.length === 0) {
      list.appendChild(el("div", "empty", "Данных пока нет — запустите прогон в шапке страницы."));
      dom["list-count"].textContent = "";
      return;
    }

    const rows = filteredRows();
    if (rows.length === 0) {
      list.appendChild(el("div", "empty", "Ничего не найдено."));
    } else {
      rows.forEach((item) => list.appendChild(listRow(item)));
    }
    dom["list-count"].textContent = `Показано ${rows.length} из ${state.report.length}`;
  }

  function listRow(item) {
    const card = item.vacancy || {};
    const issues = item.issues || [];
    const name = card.job_name || `Вакансия ${String(item.vacancy_id).slice(0, 8)}`;

    const row = el("button", "row");
    row.type = "button";
    row.dataset.id = item.vacancy_id;
    row.setAttribute("role", "listitem");
    if (item.vacancy_id === state.selected) row.classList.add("active");
    row.addEventListener("click", () => openDetail(item.vacancy_id));

    row.appendChild(el("span", "row-score", String(item.score ?? "—")));

    const main = el("span", "row-main");
    main.appendChild(el("span", "row-title", name));
    main.appendChild(
      el("span", "row-sub", card.company || card.region || item.vacancy_id)
    );
    row.appendChild(main);

    const side = el("span", "row-side");
    side.appendChild(
      el("span", `dot dot-${item.grade || "D"}`)
    );
    side.appendChild(el("span", `grade-${item.grade || "D"}`, item.grade || "?"));
    if (issues.length > 0) {
      side.appendChild(el("span", "row-count", String(issues.length)));
    }
    row.appendChild(side);
    return row;
  }

  function openDetail(id) {
    state.selected = id;
    document.body.classList.add("detail-open");
    renderList();
    renderDetail();
    dom.detail.scrollTop = 0;
    window.scrollTo(0, 0);
  }

  function closeDetail() {
    state.selected = null;
    document.body.classList.remove("detail-open");
    renderList();
    renderDetail();
  }

  // ------------------------------------------------------------- панель

  function renderDetail() {
    const pane = dom.detail;
    pane.replaceChildren();

    const item = state.report.find((entry) => entry.vacancy_id === state.selected);
    if (!item) {
      renderSummaryPane(pane);
      return;
    }
    renderVacancyPane(pane, item);
  }

  function renderSummaryPane(pane) {
    const summary = state.summary;

    const back = el("button", "back", "← Все вакансии");
    back.type = "button";
    back.addEventListener("click", closeDetail);
    pane.appendChild(back);

    const head = el("div", "pane-head");
    head.appendChild(el("h2", "", "Сводка"));
    head.appendChild(
      el(
        "p",
        "pane-sub",
        `регион 39${summary && summary.generated_at ? " · данные от " + formatDateTime(summary.generated_at) : ""}`
      )
    );
    pane.appendChild(head);

    if (!summary || summary.total === 0) {
      pane.appendChild(el("p", "muted-note", "Запустите прогон — сводка появится здесь."));
      pane.appendChild(
        el("code", "code", "python main.py --mode check --input data/kantkod_sample_vacancies.json")
      );
      return;
    }

    const stats = el("div", "stats");
    [
      { value: summary.total, label: "вакансий", hint: "обработано" },
      { value: summary.avg_score, label: "средний балл", hint: "из 100" },
      { value: summary.good_count, label: "готовы", hint: `≤ ${summary.good_max_issues} замеч.` },
      { value: summary.issues_total, label: "замечаний", hint: "правила и ИИ" },
      { value: summary.avg_schedule_score || "—", label: "средний балл графика", hint: "из 100" },
      { value: summary.avg_salary_score || "—", label: "средний балл оплаты", hint: "к средней по региону" },
    ].forEach((stat) => {
      const box = el("div", "stat");
      box.appendChild(el("span", "stat-value", String(stat.value)));
      box.appendChild(el("span", "stat-label", stat.label));
      box.appendChild(el("span", "stat-hint", stat.hint));
      stats.appendChild(box);
    });
    pane.appendChild(stats);

    const market = summary.market || {};
    if (market.average_salary) {
      const marketBlock = el("section", "block");
      marketBlock.appendChild(el("h3", "", "Средняя зарплата в регионе"));
      const marketKv = el("dl", "kv");
      marketKv.appendChild(kvRow("Средняя", `${formatMoney(market.average_salary)} ₽`));
      marketKv.appendChild(kvRow("Медиана", `${formatMoney(market.median_salary)} ₽`));
      marketBlock.appendChild(marketKv);

      if (summary.market_compared > 0) {
        const marketRows = [
          ["Ниже средней", summary.market_below || 0],
          ["На уровне средней", summary.market_at || 0],
          ["Выше средней", summary.market_above || 0],
        ];
        const maxMarket = Math.max(1, ...marketRows.map((row) => row[1]));
        marketRows.forEach(([label, count]) => {
          marketBlock.appendChild(distRow(label, count, (count / maxMarket) * 100, "", false));
        });
      } else {
        marketBlock.appendChild(
          el("p", "muted-note", "В отчёте нет вакансий с указанной зарплатой для сравнения.")
        );
      }
      marketBlock.appendChild(
        el(
          "p",
          "hint",
          `Источник: ${market.source || "—"}${market.updated ? " · " + market.updated : ""}`
        )
      );
      pane.appendChild(marketBlock);
    }

    const scheduleTypes = summary.by_schedule_type || {};
    const scheduleKeys = Object.keys(scheduleTypes);
    if (scheduleKeys.length > 0) {
      const maxSchedule = Math.max(1, ...scheduleKeys.map((key) => scheduleTypes[key]));
      const scheduleBlock = el("section", "block");
      scheduleBlock.appendChild(el("h3", "", "Типы графика"));
      scheduleKeys.forEach((key) => {
        const count = scheduleTypes[key] || 0;
        scheduleBlock.appendChild(
          distRow(key, count, (count / maxSchedule) * 100, "", false)
        );
      });
      pane.appendChild(scheduleBlock);
    }

    const grades = summary.by_grade || {};
    const gradesBlock = el("section", "block");
    gradesBlock.appendChild(el("h3", "", "Готовность к публикации"));
    GRADE_ORDER.forEach((grade) => {
      const count = grades[grade] || 0;
      const share = summary.total ? (count / summary.total) * 100 : 0;
      gradesBlock.appendChild(distRow(grade, count, share, `g-${grade}`, true));
    });
    pane.appendChild(gradesBlock);

    const topics = summary.by_topic || {};
    const topicKeys = Object.keys(TOPICS);
    const maxTopic = Math.max(1, ...topicKeys.map((key) => topics[key] || 0));
    const topicsBlock = el("section", "block");
    topicsBlock.appendChild(el("h3", "", "Замечания по темам"));
    topicKeys.forEach((key) => {
      const count = topics[key] || 0;
      topicsBlock.appendChild(
        distRow(TOPICS[key], count, (count / maxTopic) * 100, "", false)
      );
    });
    pane.appendChild(topicsBlock);

    pane.appendChild(el("p", "hint", "Выберите вакансию слева, чтобы увидеть замечания и разбор оценки."));
  }

  function distRow(label, count, percent, className, withDot) {
    const row = el("div", "dist-row" + (className ? " " + className : ""));
    if (withDot) {
      row.appendChild(el("span", `dot dot-${className.replace("g-", "")}`));
    } else {
      row.appendChild(el("span", ""));
    }
    row.appendChild(el("span", "", label));
    row.appendChild(el("span", "dist-count", String(count)));
    const track = el("span", "dist-track");
    const fill = el("i");
    fill.style.width = `${Math.max(percent, count > 0 ? 2 : 0)}%`;
    track.appendChild(fill);
    row.appendChild(track);
    return row;
  }

  function renderVacancyPane(pane, item) {
    const card = item.vacancy || {};
    const issues = (item.issues || [])
      .slice()
      .sort((a, b) => (TYPE_ORDER[a.issue_type] ?? 9) - (TYPE_ORDER[b.issue_type] ?? 9));
    const name = card.job_name || `Вакансия ${String(item.vacancy_id).slice(0, 8)}`;

    const back = el("button", "back", "← Все вакансии");
    back.type = "button";
    back.addEventListener("click", closeDetail);
    pane.appendChild(back);

    const head = el("div", "pane-head");
    head.appendChild(el("h2", "", name));
    head.appendChild(
      el("p", "pane-sub", [card.company, card.region].filter(Boolean).join(" · ") || item.vacancy_id)
    );
    pane.appendChild(head);

    const scoreRow = el("div", "score-row");
    scoreRow.appendChild(el("span", "score-big", String(item.score ?? "—")));
    scoreRow.appendChild(
      el("span", `grade-letter grade-${item.grade || "D"}`, item.grade || "?")
    );
    scoreRow.appendChild(el("span", "score-hint", "понятность и привлекательность"));
    pane.appendChild(scoreRow);

    if (item.url) {
      const link = el("a", "link", "Открыть карточку на trudvsem.ru ↗");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      pane.appendChild(link);
    }

    if (item.analysis_error) {
      pane.appendChild(
        el("div", "error-note", `ИИ не смогла обработать вакансию: ${item.analysis_error}`)
      );
    }

    const conditions = el("section", "block");
    conditions.appendChild(el("h3", "", "Условия"));
    const kv = el("dl", "kv");
    kv.appendChild(kvRow("Оплата", salaryText(card)));
    kv.appendChild(kvRow("График", card.schedule || "—"));
    kv.appendChild(kvRow("Занятость", card.employment || "—"));
    kv.appendChild(kvRow("Адрес", (card.locations || []).join("; ") || "—"));
    conditions.appendChild(kv);
    pane.appendChild(conditions);

    const components = item.score_components || [];
    if (components.length > 0) {
      const componentsBlock = el("section", "block");
      componentsBlock.appendChild(el("h3", "", "Из чего сложилась оценка"));
      components.forEach((component) => {
        componentsBlock.appendChild(componentRow(component));
      });
      componentsBlock.appendChild(
        el("p", "hint", "Итог = 50% понятность + 25% график + 25% оплата к средней по региону.")
      );
      pane.appendChild(componentsBlock);
    }

    const scheduleInfo = item.schedule_analysis;
    if (scheduleInfo) {
      const scheduleBlock = el("section", "block");
      scheduleBlock.appendChild(el("h3", "", "График работы"));
      const scheduleKv = el("dl", "kv");
      scheduleKv.appendChild(kvRow("Тип", scheduleInfo.type_label || "—"));
      scheduleKv.appendChild(kvRow("Ясность", `${scheduleInfo.clarity}/100`));
      scheduleKv.appendChild(kvRow("Комфортность", `${scheduleInfo.comfort}/100`));
      scheduleKv.appendChild(
        kvRow(
          "Дни и часы",
          scheduleInfo.days_specified && scheduleInfo.hours_specified
            ? "указаны"
            : scheduleInfo.days_specified
              ? "только дни"
              : scheduleInfo.hours_specified
                ? "только часы"
                : "не указаны"
        )
      );
      scheduleBlock.appendChild(scheduleKv);
      notesList(scheduleBlock, scheduleInfo.notes);
      pane.appendChild(scheduleBlock);
    }

    const salaryInfo = item.salary_analysis;
    if (salaryInfo) {
      const salaryBlock = el("section", "block");
      salaryBlock.appendChild(el("h3", "", "Оплата и рынок"));
      const salaryKv = el("dl", "kv");
      salaryKv.appendChild(kvRow("Зарплата", salaryText(card)));
      salaryKv.appendChild(
        kvRow("Средняя по региону", `${formatMoney(salaryInfo.benchmark)} ₽`)
      );
      salaryKv.appendChild(kvRow("Медиана", `${formatMoney(salaryInfo.median)} ₽`));
      if (salaryInfo.ratio !== null && salaryInfo.ratio !== undefined) {
        salaryKv.appendChild(kvRow("Отношение", `${Math.round(salaryInfo.ratio * 100)}%`));
      }
      salaryKv.appendChild(
        kvRow("Оценка оплаты", `${salaryInfo.score}/100 · ${salaryInfo.label}`)
      );
      salaryBlock.appendChild(salaryKv);
      notesList(salaryBlock, salaryInfo.notes);
      pane.appendChild(salaryBlock);
    }

    const scoreBlock = el("section", "block");
    scoreBlock.appendChild(el("h3", "", "Штрафы за понятность описания"));
    const penalties = item.penalties || [];
    if (penalties.length === 0) {
      scoreBlock.appendChild(el("p", "muted-note", "Штрафов нет — обязательные поля заполнены."));
    } else {
      penalties.forEach((penalty) => {
        const row = el("div", "penalty");
        row.appendChild(el("span", "", penalty.reason));
        row.appendChild(el("span", "minus", `−${penalty.points}`));
        scoreBlock.appendChild(row);
      });
    }
    pane.appendChild(scoreBlock);

    const notes = el("section", "block");
    notes.appendChild(
      el("h3", "", `Замечания · ${issues.length}`)
    );
    if (issues.length === 0) {
      notes.appendChild(el("p", "muted-note", "Замечаний нет."));
    }
    issues.forEach((issue) => notes.appendChild(noteBlock(issue)));
    pane.appendChild(notes);
  }

  function noteBlock(issue) {
    const note = el("article", "note");
    const meta = el("div", "note-meta");
    [
      TOPICS[issue.topic] || issue.topic,
      TYPES[issue.issue_type] || issue.issue_type,
      issue.detected_by === "rule" ? "правило" : "ИИ",
    ].forEach((value) => meta.appendChild(el("span", "", value)));
    note.appendChild(meta);

    if (issue.quote) {
      note.appendChild(el("blockquote", "", issue.quote));
    }
    note.appendChild(el("p", "note-problem", issue.problem));

    const rec = el("p", "note-rec");
    rec.appendChild(el("span", "", "Уточнить. "));
    rec.appendChild(document.createTextNode(issue.recommendation));
    note.appendChild(rec);
    return note;
  }

  function kvRow(label, value) {
    const row = el("div");
    row.appendChild(el("dt", "", label));
    row.appendChild(el("dd", "", value));
    return row;
  }

  function componentRow(component) {
    const row = el("div", "component");
    const label = el("span", "component-label", component.label);
    label.appendChild(el("span", "component-weight", ` × ${component.weight}`));
    row.appendChild(label);

    const track = el("span", "component-track");
    const fill = el("i");
    fill.style.width = `${Math.max(0, Math.min(100, component.score))}%`;
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("span", "component-score", String(component.score)));
    return row;
  }

  function notesList(parent, notes) {
    if (!notes || notes.length === 0) return;
    const list = el("ul", "notes-list");
    notes.forEach((note) => list.appendChild(el("li", "", note)));
    parent.appendChild(list);
  }

  function formatMoney(value) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat("ru-RU").format(value);
  }

  function salaryText(card) {
    if (!card) return "—";
    const currency = card.currency ? ` ${card.currency}` : "";
    const format = new Intl.NumberFormat("ru-RU");
    if (card.salary_min && card.salary_max) {
      return `${format.format(card.salary_min)} – ${format.format(card.salary_max)}${currency}`;
    }
    if (card.salary_min) return `от ${format.format(card.salary_min)}${currency}`;
    if (card.salary_max) return `до ${format.format(card.salary_max)}${currency}`;
    if (card.salary_raw) return `${card.salary_raw}${currency}`;
    return "не указана";
  }

  // ------------------------------------------------------------- прогон

  async function startRun(mode) {
    if (state.runBusy) return;
    const limitInput = dom["run-limit"];
    const limit = Math.min(100, Math.max(1, parseInt(limitInput.value, 10) || 10));
    limitInput.value = String(limit);

    state.runBusy = true;
    setButtonsDisabled(true);
    try {
      const result = await fetchJSON("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, limit }),
      });
      if (!result || !result.started) {
        throw new Error((result && result.message) || "не удалось запустить");
      }
      showToast(`Идёт ${mode === "live" ? "живой" : "проверочный"} прогон…`, "run");
      dom["toast-log-btn"].classList.remove("hidden");
      startPolling();
    } catch (error) {
      state.runBusy = false;
      setButtonsDisabled(false);
      showToast(`Не удалось запустить прогон: ${error.message}`, "error");
    }
  }

  function setButtonsDisabled(disabled) {
    dom["btn-check"].disabled = disabled;
    dom["btn-live"].disabled = disabled;
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(pollStatus, 2000);
    pollStatus();
  }

  function stopPolling() {
    if (!state.pollTimer) return;
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  async function pollStatus() {
    let status = null;
    try {
      status = await fetchJSON("/api/status");
    } catch (error) {
      return;
    }
    updateToast(status);

    if (!status.running) {
      stopPolling();
      state.runBusy = false;
      setButtonsDisabled(false);
      await refreshData();
      if (status.exit_code === 0) {
        scheduleToastHide(6000);
      }
    }
  }

  function updateToast(status) {
    if (!status || !status.started_at) return;

    dom["toast"].classList.remove("hidden", "success", "error");
    dom["toast-spinner"].classList.toggle("done", !status.running);

    const elapsed = status.started_at
      ? Math.max(0, Math.round((Date.now() - new Date(status.started_at).getTime()) / 1000))
      : 0;
    const modeLabel = status.mode === "live" ? "живой" : "проверочный";

    if (status.running) {
      dom["toast-text"].textContent = `Идёт ${modeLabel} прогон · ${elapsed} с`;
    } else if (status.exit_code === 0) {
      dom["toast"].classList.add("success");
      dom["toast-text"].textContent = "Прогон завершён, данные обновлены";
    } else {
      dom["toast"].classList.add("error");
      dom["toast-text"].textContent = `Прогон завершился с ошибкой (код ${status.exit_code})`;
    }

    if (status.log) {
      dom["toast-log"].textContent = status.log.split("\n").slice(-80).join("\n");
      dom["toast-log-btn"].classList.remove("hidden");
    }
  }

  function showToast(text, variant) {
    clearToastTimer();
    dom["toast"].classList.remove("hidden", "success", "error");
    dom["toast-spinner"].classList.toggle("done", variant !== "run");
    if (variant === "error") dom["toast"].classList.add("error");
    if (variant === "success") dom["toast"].classList.add("success");
    dom["toast-text"].textContent = text;
  }

  function scheduleToastHide(delay) {
    clearToastTimer();
    state.toastTimer = setTimeout(() => {
      state.toastTimer = null;
      hideToast();
    }, delay);
  }

  function clearToastTimer() {
    if (state.toastTimer) {
      clearTimeout(state.toastTimer);
      state.toastTimer = null;
    }
  }

  function hideToast() {
    dom["toast"].classList.add("hidden");
    state.logOpen = false;
    dom["toast-log"].classList.add("hidden");
  }

  // ------------------------------------------------------------- утилиты

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function plural(number, forms) {
    const [one, few, many] = forms;
    const mod10 = number % 10;
    const mod100 = number % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function formatDateTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
})();
