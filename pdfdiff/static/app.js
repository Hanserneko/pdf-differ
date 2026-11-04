"use strict";

const byId = (id) => document.getElementById(id);
const sources = ["reference", "candidate"];
const regionFields = ["region-x0", "region-y0", "region-x1", "region-y1"].map(byId);
const canvases = [byId("first-canvas"), byId("second-canvas")];
const frames = [byId("first-scroll"), byId("second-scroll")];
const state = {
  documents: { reference: null, candidate: null },
  uploadSequence: { reference: 0, candidate: 0 },
  uploads: 0,
  comparing: false,
  revision: 0,
  resultRevision: -1,
  result: null,
  source: null,
  images: {},
  mode: "overlay",
  tool: "pan",
  zoom: 0.5,
  fit: true,
  blinkPhase: false,
  blinkTimer: null,
  selection: null,
  pointer: null,
  drawScheduled: false,
  scope: "single",
  resultKind: "single",
  resultBatchId: null,
  batchSession: null,
  detailSequence: 0,
  detailLoading: false,
};

const format = (value, digits = 3, signed = false) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "不可测";
  const number = Number(value);
  const rounded = Math.abs(number) < 0.5 * 10 ** -digits ? 0 : number;
  return `${signed && rounded > 0 ? "+" : ""}${rounded.toFixed(digits)}`;
};

function showMessage(id, text) {
  const element = byId(id);
  element.textContent = text || "";
  element.hidden = !text;
}

function status(text) {
  showMessage("status-message", text);
}

function showError(error) {
  showMessage("error-message", error instanceof Error ? error.message : String(error));
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, cache: "no-store" });
  } catch {
    throw new Error("无法连接本地服务。请确认程序仍在运行，再重试。");
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const error = new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}），请检查文件和参数。`);
    error.status = response.status;
    throw error;
  }
  if (!payload) throw new Error("本地服务返回了无法识别的结果，请重试。");
  return payload;
}

function currentPage(source) {
  return Number(byId(`${source}-page`).value);
}

function validPages() {
  return sources.every((source) => {
    const document = state.documents[source];
    const page = currentPage(source);
    return document && Number.isInteger(page) && page >= 1 && page <= document.page_count;
  });
}

function matchingPageCounts() {
  const reference = state.documents.reference;
  const candidate = state.documents.candidate;
  return Boolean(reference && candidate && reference.page_count === candidate.page_count);
}

function batchActive() {
  return Boolean(state.batchSession?.active);
}

function batchFresh(session = state.batchSession) {
  return Boolean(session && session.revision === state.revision && state.scope === "all");
}

function pageResultFresh() {
  return Boolean(state.result && state.resultRevision === state.revision && !state.detailLoading
    && (state.scope === "single" ? state.resultKind === "single"
      : state.resultKind === "batch" && batchFresh() && !batchActive()
        && state.resultBatchId === state.batchSession.id));
}

function readRegion(strict = true) {
  const values = regionFields.map((field) => field.value.trim());
  if (values.every((value) => value === "")) return null;
  const region = values.map(Number);
  if (values.some((value) => value === "") || region.some((value) => !Number.isFinite(value))) {
    if (strict) throw new Error("请填写完整的区域坐标，或点“恢复整页”。");
    return null;
  }
  if (region[0] < 0 || region[1] < 0 || region[2] <= region[0] || region[3] <= region[1]) {
    if (strict) throw new Error("区域坐标需满足：0 ≤ 左 X < 右 X，0 ≤ 上 Y < 下 Y。");
    return null;
  }
  return region;
}

function setRegion(region) {
  const size = dimensions();
  regionFields.forEach((field, index) => {
    if (!region) {
      field.value = "";
      return;
    }
    // 栅格图像宽高会向上取整，写回时避免四舍五入越过实际页面边界。
    const limit = size ? (index % 2 === 0 ? size.width : size.height) : Infinity;
    field.value = Math.min(Math.round(region[index] * 100) / 100, Math.floor(limit * 100) / 100).toFixed(2);
  });
  updateRegionDescription();
}

function updateRegionDescription() {
  if (state.scope === "all") {
    byId("clear-region").disabled = true;
    byId("region-description").textContent = "全页比较逐页测量整个可见页。切换到单页比较后，可框选区域或输入坐标进行局部测量。";
    return;
  }
  const region = readRegion(false);
  const hasInput = regionFields.some((field) => field.value !== "");
  byId("clear-region").disabled = !hasInput;
  byId("region-description").textContent = region
    ? `所选区域 ${format(region[2] - region[0], 2)} × ${format(region[3] - region[1], 2)} mm。只测量该区域，保留完整页面视图。`
    : hasInput
      ? "填写完整且有效的四个坐标后，重新比较所选区域。"
      : "默认测量整页。可框选标题、正文或页码，也可输入坐标。";
}

function setExports() {
  const all = state.scope === "all";
  const session = state.batchSession;
  const batchEnabled = batchFresh() && !session.active && (session.snapshot?.completed_pages || 0) > 0;
  const detailEnabled = pageResultFresh();
  const reportEnabled = all ? batchEnabled : detailEnabled;
  const exports = all ? session?.snapshot?.exports : state.result?.exports;
  const paths = {
    "export-html": [exports?.html, reportEnabled],
    "export-json": [exports?.json, reportEnabled],
    "export-zip": [exports?.zip, reportEnabled],
    "export-diff": [state.result?.images?.diff, detailEnabled],
    "batch-export-html": [session?.snapshot?.exports?.html, batchEnabled],
    "batch-export-json": [session?.snapshot?.exports?.json, batchEnabled],
    "batch-export-zip": [session?.snapshot?.exports?.zip, batchEnabled],
  };
  for (const [id, [url, enabled]] of Object.entries(paths)) {
    const element = byId(id);
    const active = Boolean(enabled && url);
    element.classList.toggle("disabled-link", !active);
    element.setAttribute("aria-disabled", String(!active));
    if (active) {
      element.href = url;
      element.setAttribute("download", "");
    } else {
      element.removeAttribute("href");
      element.removeAttribute("download");
    }
  }
  byId("export-html").textContent = all ? "整本 HTML" : "HTML 报告";
  byId("export-json").textContent = all ? "整本 JSON" : "JSON 数据";
  byId("export-zip").textContent = all ? "整本 ZIP" : "完整 ZIP";
  byId("export-diff").textContent = all ? "当前详情 PNG" : "差异 PNG";
  byId("export-scope").textContent = all
    ? "HTML / JSON / ZIP 导出整本摘要与逐页数据，不包含全部图片；PNG 仅导出当前详情页。取消后的报告仅包含已处理页。"
    : "当前单页的报告与图片。";
}

function updateControls() {
  const all = state.scope === "all";
  const active = batchActive();
  const busy = state.comparing || active;
  byId("compare-button").disabled = busy || state.detailLoading || state.uploads > 0 || (all ? !matchingPageCounts() : !validPages());
  byId("compare-button").innerHTML = active
    ? state.batchSession.cancelRequested ? '<span aria-hidden="true">◌</span> 正在取消' : '<span aria-hidden="true">◌</span> 全页比较中'
    : state.comparing ? '<span aria-hidden="true">◌</span> 正在比较'
      : all ? '<span aria-hidden="true">⇄</span> 比较全部页面' : '<span aria-hidden="true">⇄</span> 开始比较';
  document.body.classList.toggle("busy", busy);
  document.body.classList.toggle("batch-scope", all);
  byId("demo-button").disabled = busy || state.uploads > 0;
  byId("empty-demo-button").disabled = busy || state.uploads > 0;
  byId("scope-all").disabled = !matchingPageCounts();
  byId("scope-single").checked = !all;
  byId("scope-all").checked = all;
  sources.forEach((source) => {
    byId(`${source}-page`).disabled = all || !state.documents[source];
    byId(`${source}-page-total`).textContent = `${all ? "共" : "/"} ${state.documents[source]?.page_count ?? "—"} 页`;
  });
  const reference = state.documents.reference;
  const candidate = state.documents.candidate;
  byId("scope-help").textContent = !reference || !candidate
    ? "载入两份页数相同的 PDF 后，可一次比较全部页面。"
    : !matchingPageCounts() ? `两份 PDF 页数不同（${reference.page_count} / ${candidate.page_count} 页），全页比较不可用，请选择单页比较。`
      : all ? `将按相同页码自动比较全部 ${reference.page_count} 页，完成后可从摘要查看单页详情。`
        : `两份 PDF 均为 ${reference.page_count} 页，可切换全页比较。`;
  byId("scope-help").classList.toggle("scope-unavailable", Boolean(reference && candidate && !matchingPageCounts()));
  regionFields.forEach((field) => { field.disabled = all; });
  byId("region-tool").disabled = all;
  updateRegionDescription();
  const fresh = pageResultFresh();
  const resultState = byId("result-state");
  resultState.hidden = !state.result && !state.detailLoading;
  resultState.textContent = state.detailLoading ? "正在载入详情" : fresh ? "已完成测量" : "待重新比较";
  resultState.classList.toggle("stale", !fresh);
  byId("metrics-section").classList.toggle("stale", Boolean(state.result && !fresh));
  byId("line-section").classList.toggle("stale", Boolean(state.result && !fresh));
  byId("diagnostics-section").classList.toggle("stale", Boolean(state.result && !fresh));
  if (state.result) byId("diagnostics-count").textContent = `${fresh ? "" : "旧结果 · "}${state.result.report.diagnostics?.findings?.length ?? 0} 条诊断`;
  if (state.result) byId("line-count").textContent = `${fresh ? "" : "旧结果 · "}${state.result.report.line_differences?.length ?? 0} 条匹配行`;
  byId("metric-scope").textContent = state.detailLoading ? "正在载入 · 旧结果" : !state.result ? "等待比较" : !fresh ? "参数已变更 · 旧结果" : all ? `第 ${state.result.report.reference.page} 页详情` : state.result.report.settings.region_mm ? "所选区域" : "整页";
  byId("retry-detail").hidden = !(all && state.batchSession?.selectedPage && batchFresh() && !active);
  byId("retry-detail").disabled = state.detailLoading;
  setExports();
}

function invalidate(reason) {
  state.revision += 1;
  state.detailSequence += 1;
  state.detailLoading = false;
  if (batchActive()) cancelBatch(state.batchSession);
  if (state.result) status(`${reason}。当前显示上一次结果，请点击“开始比较”更新测量。`);
  else if (state.batchSession) status(`${reason}。全页结果已失效，${batchActive() ? "请等待活动任务取消后重新比较" : "请重新比较"}。`);
  renderBatch();
  updateControls();
}

function updatePageMetadata(source) {
  const document = state.documents[source];
  const page = document?.pages?.[currentPage(source) - 1];
  byId(`${source}-size`).textContent = page
    ? `${format(page.width_mm, 1)} × ${format(page.height_mm, 1)} mm`
    : "";
}

function setDocument(source, document) {
  state.documents[source] = document;
  byId(`${source}-name`).textContent = document.filename;
  byId(`${source}-name`).title = document.filename;
  byId(`${source}-meta`).textContent = `${document.page_count} 页 · 已载入本地内存`;
  byId(`${source}-page-total`).textContent = `/ ${document.page_count} 页`;
  const pageInput = byId(`${source}-page`);
  pageInput.disabled = false;
  pageInput.max = String(document.page_count);
  pageInput.value = "1";
  updatePageMetadata(source);
}

async function upload(source, file) {
  if (!file) return;
  if (!file.size) {
    showError(new Error("文件为空，请选择有效的 PDF。"));
    return;
  }
  const sequence = ++state.uploadSequence[source];
  state.uploads += 1;
  showMessage("error-message", "");
  invalidate("文件选择已变更");
  byId(`${source}-meta`).textContent = `正在读取 ${file.name}…`;
  status("正在检查 PDF 页数和页面尺寸…");
  updateControls();
  try {
    const result = await request(`/api/documents?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    if (sequence !== state.uploadSequence[source]) return;
    setDocument(source, result);
    setRegion(null);
    invalidate("比较文件已更新");
    if (!state.result) status(validPages() ? "两份 PDF 已就绪，点击“开始比较”。" : "PDF 已载入，请选择另一份文件。");
  } catch (error) {
    if (sequence !== state.uploadSequence[source]) return;
    const previous = state.documents[source];
    byId(`${source}-meta`).textContent = previous ? `${previous.page_count} 页 · 保留之前载入的文件` : "读取失败，请重新选择 PDF";
    status("");
    showError(error);
  } finally {
    state.uploads -= 1;
    updateControls();
  }
}

async function loadDemo() {
  if (state.uploads || state.comparing || batchActive()) return;
  state.uploads += 1;
  sources.forEach((source) => { state.uploadSequence[source] += 1; });
  showMessage("error-message", "");
  status("正在生成含已知排版偏差的示例页面…");
  invalidate("正在载入示例");
  updateControls();
  try {
    const result = await request("/api/demo", { method: "POST" });
    setDocument("reference", result.reference);
    setDocument("candidate", result.candidate);
    setRegion(null);
    invalidate("示例文件已载入");
    state.uploads -= 1;
    updateControls();
    await compare();
    return;
  } catch (error) {
    status("");
    showError(error);
  }
  state.uploads -= 1;
  updateControls();
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("比较图像读取失败，结果可能已过期。请重新比较。"));
    image.src = url;
  });
}

async function compare() {
  if (state.scope === "all") return startBatch();
  if (state.comparing || state.uploads || batchActive()) return;
  showMessage("error-message", "");
  let region;
  try {
    if (!validPages()) throw new Error("请先选择两份 PDF，并输入有效页码。");
    region = readRegion();
    if (!byId("ink-threshold").checkValidity()) throw new Error("墨迹阈值需为 1 到 254 的整数。");
  } catch (error) {
    showError(error);
    return;
  }
  const payload = {
    reference_id: state.documents.reference.id,
    candidate_id: state.documents.candidate.id,
    reference_page: currentPage("reference"),
    candidate_page: currentPage("candidate"),
    dpi: Number(byId("dpi").value),
    ink_threshold: Number(byId("ink-threshold").value),
    region_mm: region,
  };
  const revision = state.revision;
  const names = sources.map((source) => state.documents[source].filename);
  state.comparing = true;
  status(payload.dpi >= 600 ? "正在以 600 DPI 渲染和测量，精细对比需要更多时间…" : "正在渲染页面、匹配文字并测量排版差异…");
  updateControls();
  const notice = setTimeout(() => {
    status("仍在渲染和测量，请稍候。高 DPI 或复杂页面可能耗时较长。");
  }, 10000);
  try {
    const result = await request("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const images = Object.fromEntries(await Promise.all(Object.entries(result.images).map(async ([key, url]) => [key, await loadImage(url)])));
    if (revision !== state.revision) {
      status("比较期间文件或参数发生了变化。请重新比较，当前结果未用于新的设置。");
      return;
    }
    state.result = result;
    state.resultKind = "single";
    state.resultBatchId = null;
    state.source = payload;
    state.resultRevision = revision;
    state.images = images;
    state.fit = true;
    state.pointer = null;
    state.selection = null;
    byId("empty-state").hidden = true;
    byId("viewer").hidden = false;
    byId("comparison-caption").textContent = `${names[0]} · 第 ${payload.reference_page} 页  ⇄  ${names[1]} · 第 ${payload.candidate_page} 页`;
    renderMetrics(result.report);
    setMode(state.mode, false);
    requestAnimationFrame(() => {
      fitView();
      frames.forEach((frame) => { frame.scrollTop = 0; frame.scrollLeft = 0; });
    });
    status("比较完成。可切换视图检查细节，或框选区域后重新测量。");
  } catch (error) {
    status("");
    showError(error);
  } finally {
    clearTimeout(notice);
    state.comparing = false;
    updateControls();
  }
}

function batchCompletionText(session) {
  const snapshot = session.snapshot;
  if (!snapshot) return session.failure || "正在创建全页任务…";
  const summary = snapshot.summary || {};
  if (snapshot.status === "cancelled") return `比较已取消，已处理 ${snapshot.completed_pages} / ${snapshot.total_pages} 页；其余页面未比较。${snapshot.completed_pages ? "已有结果仍可查看和导出。" : "当前没有已处理页，可重新比较。"}`;
  if (snapshot.status === "failed") return `全页任务失败，已处理 ${snapshot.completed_pages} / ${snapshot.total_pages} 页：${snapshot.error || "请检查逐页结果后重试。"}`;
  return `全页比较结束：${summary.changed_pages || 0} 页有视觉差异，${summary.unchanged_pages || 0} 页视觉一致，${summary.error_pages || 0} 页失败。${summary.error_pages ? "失败页需单独检查，不能视为一致。" : "选择某页可查看详情。"}`;
}

function renderBatch() {
  const session = state.batchSession;
  byId("batch-section").hidden = !session || (state.scope !== "all" && !session.active);
  if (!session) return;
  const snapshot = session.snapshot;
  const fresh = batchFresh(session);
  const summary = snapshot?.summary || {};
  const total = snapshot?.total_pages ?? session.total;
  const completed = snapshot?.completed_pages ?? 0;
  const badge = byId("batch-state");
  const labels = { queued: "排队中", running: "比较中", completed: "已结束", cancelled: "已取消 · 部分结果", failed: "任务失败" };
  badge.textContent = !fresh ? "设置已变更" : session.cancelRequested && session.active ? "正在取消" : session.failure ? "请求失败" : snapshot ? labels[snapshot.status] || snapshot.status : "准备中";
  badge.className = `batch-state ${!fresh ? "stale" : session.failure || snapshot?.status === "failed" || summary.error_pages ? "has-errors" : snapshot?.status === "completed" ? "complete" : ""}`;
  byId("batch-caption").textContent = `${session.names[0]} ⇄ ${session.names[1]} · 按相同页码比较 · ${session.source.dpi} DPI`;
  byId("batch-total").textContent = total;
  byId("batch-compared").textContent = summary.compared_pages ?? 0;
  byId("batch-changed").textContent = summary.changed_pages ?? 0;
  byId("batch-unchanged").textContent = summary.unchanged_pages ?? 0;
  byId("batch-errors").textContent = summary.error_pages ?? 0;
  byId("batch-progress").max = Math.max(total, 1);
  byId("batch-progress").value = completed;
  byId("batch-progress-text").textContent = session.active
    ? session.cancelRequested ? `已处理 ${completed} / ${total} 页 · 等待当前页结束后取消`
      : `已处理 ${completed} / ${total} 页${snapshot?.current_page ? ` · 正在比较第 ${snapshot.current_page} 页` : " · 正在准备"}`
    : `${completed} / ${total} 页已处理${snapshot?.status === "cancelled" ? " · 未全部完成" : ""}`;
  const warning = !fresh ? `文件、范围或参数已变更。此处为旧任务结果，导出和详情已禁用。${session.active ? "活动任务正在取消。" : "请按当前设置重新比较。"}`
    : session.pollError || session.failure || snapshot?.error || (summary.error_pages ? `${summary.error_pages} 页比较失败，请查看下方失败信息。失败页始终保留在筛选结果中。` : "");
  byId("batch-warning").textContent = warning;
  byId("batch-warning").hidden = !warning;
  const cancel = byId("cancel-batch");
  cancel.hidden = !session.active;
  cancel.disabled = session.cancelInFlight || (session.cancelRequested && (session.cancelSent || !session.id));
  cancel.textContent = session.cancelRequested ? session.cancelSent || !session.id ? "等待取消…" : "重试取消" : "取消比较";
  const onlyDifferences = byId("batch-only-differences").checked;
  const pages = new Map((snapshot?.pages || []).map((page) => [page.page, page]));
  const body = byId("batch-page-body");
  const fragment = document.createDocumentFragment();
  let visible = 0;
  for (let number = 1; number <= total; number += 1) {
    const item = pages.get(number);
    if (onlyDifferences && item?.status !== "error" && !item?.has_visual_difference) continue;
    visible += 1;
    const row = document.createElement("tr");
    row.classList.toggle("selected-page", session.selectedPage === number);
    const appendCell = (value, className = "") => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (className) cell.className = className;
      row.append(cell);
      return cell;
    };
    appendCell(String(number));
    const label = !item ? session.active ? snapshot?.current_page === number ? "比较中" : "等待" : "未比较"
      : item.status === "error" ? "失败" : item.has_visual_difference === true ? "有差异" : item.has_visual_difference === false ? "视觉一致" : "已比较";
    const statusCell = appendCell(label, item?.status === "error" ? "page-error" : item?.has_visual_difference ? "page-changed" : "");
    if (item?.error) {
      const description = document.createElement("small");
      description.textContent = item.error;
      statusCell.append(description);
    }
    const visual = item?.report?.visual;
    const text = item?.report?.text;
    appendCell(visual?.foreground_mismatch_ratio === null || visual?.foreground_mismatch_ratio === undefined ? "—" : `${format(visual.foreground_mismatch_ratio * 100, 2)}%`);
    appendCell(text ? format(text.median_dx_mm, 3, true) : "—");
    appendCell(text ? format(text.median_dy_mm, 3, true) : "—");
    const actionCell = appendCell("");
    if (item?.status === "done") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "text-button batch-detail-button";
      button.dataset.page = String(number);
      button.textContent = session.selectedPage === number && state.detailLoading ? "正在载入…" : "查看详情";
      button.disabled = !fresh || session.active || (session.selectedPage === number && state.detailLoading);
      if (session.active) button.title = "全页任务结束或取消后，可载入单页详情。";
      button.addEventListener("click", () => loadBatchPage(number));
      actionCell.append(button);
    } else actionCell.textContent = "—";
    fragment.append(row);
  }
  if (!visible) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = session.active ? "暂未发现差异或失败页，正在继续比较。" : "筛选范围内没有差异或失败页。";
    row.append(cell);
    fragment.append(row);
  }
  body.replaceChildren(fragment);
  byId("batch-row-count").textContent = `显示 ${visible} / ${total} 页`;
}

function acceptBatchSnapshot(session, snapshot) {
  if (session !== state.batchSession || snapshot.id !== session.id) return;
  const terminal = (status) => ["completed", "cancelled", "failed"].includes(status);
  // 取消响应和轮询可能交错到达；不让旧进度覆盖终态或已经处理的页面。
  if (session.snapshot && (snapshot.completed_pages < session.snapshot.completed_pages
    || terminal(session.snapshot.status) && !terminal(snapshot.status))) return;
  session.snapshot = snapshot;
  session.cancelRequested ||= Boolean(snapshot.cancel_requested);
  session.active = !terminal(snapshot.status);
  session.pollError = null;
  session.pollFailures = 0;
  if (!session.active && session.pollTimer) clearTimeout(session.pollTimer);
  renderBatch();
  updateControls();
  if (!session.active && batchFresh(session)) {
    status(batchCompletionText(session));
    if (!session.autoViewed) {
      session.autoViewed = true;
      const done = (snapshot.pages || []).filter((page) => page.status === "done");
      const first = done.find((page) => page.has_visual_difference) || done[0];
      if (first) loadBatchPage(first.page);
    }
  }
}

function scheduleBatchPoll(session, delay = 800) {
  if (session !== state.batchSession || !session.active || !session.id) return;
  if (session.pollTimer) clearTimeout(session.pollTimer);
  session.pollTimer = setTimeout(() => pollBatch(session), delay);
}

async function pollBatch(session) {
  if (session !== state.batchSession || !session.active) return;
  try {
    const snapshot = await request(`/api/batches/${encodeURIComponent(session.id)}`);
    acceptBatchSnapshot(session, snapshot);
    if (session.active) scheduleBatchPoll(session);
  } catch (error) {
    if (session !== state.batchSession || !session.active) return;
    if (error.status === 404) {
      session.active = false;
      session.failure = "任务状态已过期或不存在，请重新进行全页比较。";
      if (batchFresh(session)) showError(new Error(session.failure));
      renderBatch();
      updateControls();
      return;
    }
    session.pollFailures += 1;
    session.pollError = `进度更新失败，正在重试：${error.message}`;
    renderBatch();
    scheduleBatchPoll(session, Math.min(1000 * session.pollFailures, 5000));
  }
}

async function cancelBatch(session = state.batchSession) {
  if (!session || !session.active || session.cancelInFlight || session.cancelSent) return;
  session.cancelRequested = true;
  renderBatch();
  updateControls();
  if (!session.id) return;
  session.cancelInFlight = true;
  renderBatch();
  try {
    const snapshot = await request(`/api/batches/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    session.cancelSent = true;
    acceptBatchSnapshot(session, snapshot);
  } catch (error) {
    session.pollError = `取消请求未送达，可点击“重试取消”：${error.message}`;
    if (batchFresh(session)) showError(new Error(session.pollError));
  } finally {
    session.cancelInFlight = false;
    if (session === state.batchSession) {
      renderBatch();
      updateControls();
      scheduleBatchPoll(session);
    }
  }
}

async function startBatch() {
  if (state.comparing || state.uploads || batchActive() || state.detailLoading) return;
  showMessage("error-message", "");
  if (!matchingPageCounts()) {
    showError(new Error("全页比较要求两份 PDF 页数相同。请重新选择文件，或切换单页比较。"));
    return;
  }
  if (!byId("ink-threshold").checkValidity()) {
    showError(new Error("墨迹阈值需为 1 到 254 的整数。"));
    return;
  }
  const source = {
    reference_id: state.documents.reference.id,
    candidate_id: state.documents.candidate.id,
    dpi: Number(byId("dpi").value),
    ink_threshold: Number(byId("ink-threshold").value),
  };
  const session = {
    id: null, revision: state.revision, source,
    names: sources.map((name) => state.documents[name].filename),
    total: state.documents.reference.page_count,
    active: true, snapshot: null, failure: null,
    cancelRequested: false, cancelSent: false, cancelInFlight: false,
    pollTimer: null, pollFailures: 0, pollError: null,
    selectedPage: null, autoViewed: false,
  };
  state.batchSession = session;
  state.resultRevision = -1;
  state.detailSequence += 1;
  status(`正在创建全页任务，将自动比较全部 ${session.total} 页…`);
  renderBatch();
  updateControls();
  try {
    const snapshot = await request("/api/batches", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(source),
    });
    session.id = snapshot.id;
    acceptBatchSnapshot(session, snapshot);
    if (session.cancelRequested && session.active) await cancelBatch(session);
    else scheduleBatchPoll(session);
  } catch (error) {
    if (session !== state.batchSession) return;
    session.active = false;
    session.failure = error.message;
    if (batchFresh(session)) {
      status("");
      showError(error);
    }
    renderBatch();
    updateControls();
  }
}

async function loadBatchPage(number) {
  const session = state.batchSession;
  if (!batchFresh(session) || session.active) return;
  const item = session.snapshot?.pages?.find((page) => page.page === number && page.status === "done");
  if (!item) return;
  const sequence = ++state.detailSequence;
  session.selectedPage = number;
  state.detailLoading = true;
  state.resultRevision = -1;
  showMessage("error-message", "");
  status(`正在载入第 ${number} 页详情：重新渲染原始分辨率图像…`);
  renderBatch();
  updateControls();
  try {
    const result = await request(`/api/batches/${encodeURIComponent(session.id)}/pages/${number}/compare`, { method: "POST" });
    const images = Object.fromEntries(await Promise.all(Object.entries(result.images).map(async ([key, url]) => [key, await loadImage(url)])));
    if (sequence !== state.detailSequence || session !== state.batchSession || !batchFresh(session)) return;
    state.result = result;
    state.resultKind = "batch";
    state.resultBatchId = session.id;
    state.source = { ...session.source, reference_page: number, candidate_page: number, region_mm: null };
    state.resultRevision = session.revision;
    state.images = images;
    state.fit = true;
    state.pointer = null;
    state.selection = null;
    byId("empty-state").hidden = true;
    byId("viewer").hidden = false;
    byId("comparison-caption").textContent = `全页比较详情 · 第 ${number} / ${session.total} 页 · ${session.names[0]} ⇄ ${session.names[1]}`;
    renderMetrics(result.report);
    setMode(state.mode, false);
    requestAnimationFrame(() => {
      fitView();
      frames.forEach((frame) => { frame.scrollTop = 0; frame.scrollLeft = 0; });
    });
    status(`${batchCompletionText(session)} 当前显示第 ${number} 页。`);
  } catch (error) {
    if (sequence !== state.detailSequence || session !== state.batchSession) return;
    showError(new Error(`第 ${number} 页详情载入失败：${error.message} 可点击“重新载入详情”重试，整本摘要仍保留。`));
    status("当前详情未更新，旧页面不作为所选页的测量结果。");
  } finally {
    if (sequence === state.detailSequence) {
      state.detailLoading = false;
      renderBatch();
      updateControls();
    }
  }
}

function renderMetrics(report) {
  renderDiagnostics(report);
  const visual = report.visual || {};
  const text = report.text || {};
  const mismatch = visual.foreground_mismatch_ratio;
  byId("metric-mismatch").textContent = mismatch === null || mismatch === undefined ? "不可测" : `${format(mismatch * 100, 2)}%`;
  byId("metric-dx").textContent = format(text.median_dx_mm, 3, true);
  byId("metric-dy").textContent = format(text.median_dy_mm, 3, true);
  byId("metric-spacing").textContent = format(text.line_spacing_delta_mm, 3, true);
  byId("metric-position").textContent = format(text.p95_position_error_mm);
  byId("metric-matches").textContent = `${text.matched_characters ?? 0} 字 / ${text.matched_lines ?? 0} 行`;
  byId("metric-characters").textContent = `${text.reference_characters ?? "—"} / ${text.candidate_characters ?? "—"}`;
  byId("metric-unmatched").textContent = `${text.unmatched_reference_characters ?? "不可测"} / ${text.unmatched_candidate_characters ?? "不可测"}`;
  byId("metric-font-size").textContent = format(text.font_size_delta_pt, 3, true);
  byId("metric-font-names").textContent = text.changed_font_name_characters ?? "不可测";
  byId("metric-line-breaks").textContent = text.line_break_change_count ?? "不可测";
  byId("metric-line-heights").textContent = `${format(text.reference_line_spacing_mm, 2)} / ${format(text.candidate_line_spacing_mm, 2)} mm`;
  byId("metric-edge").textContent = `${format(visual.edge_distance_p95_mm)} mm`;
  byId("metric-rgb-pixels").textContent = visual.rgb_changed_pixels === null || visual.rgb_changed_pixels === undefined ? "不可测" : Number(visual.rgb_changed_pixels).toLocaleString("zh-CN");
  byId("metric-rgb-error").textContent = format(visual.rgb_mean_absolute_error, 5);
  byId("metric-ssim").textContent = format(visual.ssim, 4);

  const warnings = [...new Set([
    ...(state.documents.reference?.warnings || []),
    ...(state.documents.candidate?.warnings || []),
    ...(report.warnings || []),
  ])];
  const warningList = byId("warnings-list");
  warningList.replaceChildren();
  warnings.forEach((warning) => {
    const item = document.createElement("li");
    item.textContent = warning;
    warningList.append(item);
  });
  byId("warnings-section").hidden = !warnings.length;

  const rows = report.line_differences || [];
  byId("line-count").textContent = `${rows.length} 条匹配行`;
  const body = byId("line-body");
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "此区域没有可可靠匹配的文本行。可继续使用图像视图检查差异。";
    row.append(cell);
    body.append(row);
  }
  rows.forEach((line) => {
    const row = document.createElement("tr");
    const values = [line.text, format(line.dx_mm, 3, true), format(line.dy_mm, 3, true), format(line.reference_font_size_pt, 2), format(line.candidate_font_size_pt, 2)];
    values.forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value ?? "";
      row.append(cell);
    });
    body.append(row);
  });
  byId("line-section").hidden = false;
}

const diagnosticLabels = {
  "page.geometry": "页面几何",
  "text.content": "文字内容",
  "text.position": "文字位置",
  "text.font_size": "字号",
  "text.font_name": "字体名称",
  "text.reflow": "换行关系",
  "text.line_spacing": "基线行距",
  "text.line_pattern": "连续行偏移规律",
  "visual.region": "视觉差异区域",
};

const diagnosticMeasurementLabels = {
  dx_mm: "横向偏移", dy_mm: "纵向偏移", font_size_pt: "字号", font_name: "字体名",
  width_mm: "宽度", height_mm: "高度", rotation: "旋转", cropbox_pt: "裁切框", mediabox_pt: "媒体框",
  initial_dx_mm: "初始横向偏移", initial_dy_mm: "初始纵向偏移",
  per_line_dx_mm: "逐行横向增量", per_line_dy_mm: "逐行纵向增量",
  baseline_spacing_mm: "基线行距", changed_pixels: "变化像素数", extracted_text: "提取文字",
  line_break_relations: "换行关系变化",
};

function diagnosticText(parent, tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function diagnosticValue(value) {
  if (value === null || value === undefined) return "不可测";
  if (typeof value === "number") return format(value, 4);
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function diagnosticPreviews(parent, finding, report, images) {
  const boxes = [finding.reference?.bbox_mm ?? finding.reference?.anchor_bbox_mm, finding.candidate?.bbox_mm ?? finding.candidate?.anchor_bbox_mm]
    .filter((box) => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite));
  if (!boxes.length || !images.reference) return;
  const pixelsPerMM = report.settings.dpi / 25.4;
  const x0 = Math.max(0, Math.floor((Math.min(...boxes.map((box) => box[0])) - 2) * pixelsPerMM));
  const y0 = Math.max(0, Math.floor((Math.min(...boxes.map((box) => box[1])) - 2) * pixelsPerMM));
  const x1 = Math.min(images.reference.naturalWidth, Math.ceil((Math.max(...boxes.map((box) => box[2])) + 2) * pixelsPerMM));
  const y1 = Math.min(images.reference.naturalHeight, Math.ceil((Math.max(...boxes.map((box) => box[3])) + 2) * pixelsPerMM));
  const width = x1 - x0;
  const height = y1 - y0;
  if (width <= 0 || height <= 0) return;
  const scale = Math.min(1, 700 / width, 300 / height);
  const previews = document.createElement("div");
  previews.className = "diagnostics-previews";
  for (const [key, label] of [["reference", "参考局部"], ["candidate", "待比较局部"], ["diff", "差异局部"]]) {
    if (!images[key]) continue;
    const figure = document.createElement("figure");
    diagnosticText(figure, "figcaption", label);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    canvas.setAttribute("aria-label", label);
    canvas.getContext("2d").drawImage(images[key], x0, y0, width, height, 0, 0, canvas.width, canvas.height);
    figure.append(canvas);
    previews.append(figure);
  }
  parent.append(previews);
}

function diagnosticCard(finding, report, images) {
  const card = document.createElement("details");
  card.className = "diagnostic-card";
  const heading = document.createElement("summary");
  diagnosticText(heading, "span", diagnosticLabels[finding.type] || finding.type || "差异", "diagnostic-type");
  diagnosticText(heading, "span", finding.observation || "展开查看诊断依据", "diagnostic-observation");
  diagnosticText(heading, "span", finding.reliability?.level === "high" ? "可靠" : "需复核", "diagnostic-reliability");
  card.append(heading);
  // 局部画布按需生成，长报告的折叠条目不占用额外图像内存。
  card.addEventListener("toggle", () => {
    if (!card.open || card.childElementCount > 1) return;
    const body = document.createElement("div");
    body.className = "diagnostic-body";
    for (const [key, label] of [["reference", "参考"], ["candidate", "待比较"]]) {
      const side = finding[key] || {};
      const bounds = side.bbox_mm ?? side.anchor_bbox_mm;
      const location = Array.isArray(bounds) ? `${side.bbox_mm ? "边界" : "相邻文字锚点"} ${bounds.map((value) => format(value, 2)).join(", ")} mm` : "无定位框";
      diagnosticText(body, "p", `${label} · 第 ${side.page ?? "—"} 页 · ${location}`, "small-note");
      if (side.text !== undefined && side.text !== null) diagnosticText(body, "p", side.text === "" ? "（空文本）" : side.text, "diagnostic-source-text");
      if (side.context_before || side.context_after) diagnosticText(body, "p", `上下文 ${side.context_before || ""} ⟦变化位置⟧ ${side.context_after || ""}`, "small-note");
    }
    const measurements = Object.entries(finding.measurements || {});
    if (measurements.length) {
      const wrap = document.createElement("div");
      wrap.className = "table-scroll diagnostic-measurements";
      const table = document.createElement("table");
      const header = document.createElement("tr");
      for (const title of ["测量项", "参考", "待比较", "差值", "单位", "统计方法"]) diagnosticText(header, "th", title);
      const head = document.createElement("thead");
      head.append(header);
      table.append(head);
      const rows = document.createElement("tbody");
      for (const [name, measurement] of measurements) {
        const row = document.createElement("tr");
        for (const value of [diagnosticMeasurementLabels[name] || name, diagnosticValue(measurement?.reference), diagnosticValue(measurement?.candidate), diagnosticValue(measurement?.delta), measurement?.unit || "—", measurement?.method || "—"]) diagnosticText(row, "td", value);
        rows.append(row);
      }
      table.append(rows);
      wrap.append(table);
      body.append(wrap);
    }
    if (finding.target?.description) diagnosticText(body, "p", `调整目标 · ${finding.target.description}`, "diagnostic-target");
    if (finding.target?.candidate_adjustment) diagnosticText(body, "p", `输出调整量 ${diagnosticValue(finding.target.candidate_adjustment)}`, "small-note");
    for (const reason of finding.reliability?.reasons || []) diagnosticText(body, "p", reason, "small-note");
    if (finding.reliability?.evidence) diagnosticText(body, "pre", JSON.stringify(finding.reliability.evidence, null, 2), "diagnostic-evidence");
    if (finding.related_findings?.length) diagnosticText(body, "p", `关联诊断 ${finding.related_findings.join("、")}`, "small-note");
    diagnosticText(body, "p", `诊断 ${finding.id || "—"} · 源码位置 ${finding.source_location ? diagnosticValue(finding.source_location) : "待结合源码定位"}`, "small-note");
    diagnosticPreviews(body, finding, report, images);
    card.append(body);
  });
  return card;
}

function renderDiagnostics(report) {
  const diagnostics = report.diagnostics;
  byId("diagnostics-section").hidden = !diagnostics;
  const list = byId("diagnostics-list");
  list.replaceChildren();
  if (!diagnostics) return;
  const findings = diagnostics.findings || [];
  const images = state.images;
  byId("diagnostics-metadata").textContent = JSON.stringify({
    conventions: diagnostics.conventions, tolerances: diagnostics.tolerances,
    summary: diagnostics.summary, coverage: diagnostics.coverage, truncated: diagnostics.truncated,
  }, null, 2);
  byId("diagnostics-count").textContent = `${findings.length} 条诊断`;
  byId("diagnostics-summary").textContent = findings.length
    ? `位置差采用待比较减参考，向右、向下为正。${diagnostics.truncated ? "诊断达到数量上限，请查看覆盖范围与限制。" : "完整诊断随 JSON、HTML 和 ZIP 导出。"}`
    : "当前条件下未生成差异诊断，请结合测量覆盖范围与提示判断。";
  const limitations = byId("diagnostics-limitations");
  limitations.replaceChildren();
  for (const text of diagnostics.limitations || []) diagnosticText(limitations, "li", text);
  limitations.hidden = !limitations.childElementCount;
  let shown = 0;
  const more = byId("diagnostics-more");
  const append = () => {
    const end = Math.min(findings.length, shown + 50);
    for (; shown < end; shown += 1) list.append(diagnosticCard(findings[shown], report, images));
    more.hidden = shown >= findings.length;
    more.textContent = `显示更多诊断（已显示 ${shown} / ${findings.length}）`;
  };
  more.onclick = append;
  append();
}

function stopBlink() {
  if (state.blinkTimer !== null) clearInterval(state.blinkTimer);
  state.blinkTimer = null;
}

function startBlink() {
  stopBlink();
  if (state.mode !== "blink" || !state.result || document.hidden) return;
  state.blinkPhase = false;
  state.blinkTimer = setInterval(() => {
    state.blinkPhase = !state.blinkPhase;
    draw();
  }, 650);
}

function setMode(mode, autoFit = true) {
  const modeChanged = mode !== state.mode;
  state.mode = mode;
  stopBlink();
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.mode === mode));
  });
  byId("second-pane").hidden = mode !== "side-by-side";
  const labels = { overlay: "红蓝叠加 · 深色为重合", "side-by-side": "参考页", diff: "差异热图 · 红色越强，差异越明显", blink: "闪烁对照 · 每 0.65 秒切换", reference: "参考页", candidate: "待比较页" };
  byId("first-view-label").textContent = labels[mode];
  byId("view-legend").style.visibility = mode === "reference" || mode === "candidate" || mode === "blink" ? "hidden" : "visible";
  byId("overlay-legend").hidden = mode === "diff";
  byId("diff-legend").hidden = mode !== "diff";
  if (state.result) {
    if (modeChanged && autoFit && state.fit) requestAnimationFrame(fitView);
    else draw();
    if (mode === "blink") startBlink();
  }
}

function setTool(tool) {
  if (tool === "region" && state.scope === "all") return;
  state.tool = tool;
  byId("pan-tool").classList.toggle("active", tool === "pan");
  byId("pan-tool").setAttribute("aria-pressed", String(tool === "pan"));
  byId("region-tool").classList.toggle("active", tool === "region");
  byId("region-tool").setAttribute("aria-pressed", String(tool === "region"));
  byId("viewer").classList.toggle("selecting", tool === "region");
}

function dimensions() {
  if (!state.result) return null;
  const report = state.result.report;
  return {
    width: Math.max(report.reference.width_mm, report.candidate.width_mm),
    height: Math.max(report.reference.height_mm, report.candidate.height_mm),
  };
}

function currentSceneMatches() {
  if (state.scope === "all") return state.resultKind === "batch" && batchFresh()
    && state.resultBatchId === state.batchSession.id;
  return state.source && state.source.reference_id === state.documents.reference?.id
    && state.source.candidate_id === state.documents.candidate?.id
    && state.source.reference_page === currentPage("reference")
    && state.source.candidate_page === currentPage("candidate");
}

function drawCanvas(canvas, image) {
  if (!image) return;
  if (canvas.width !== image.naturalWidth || canvas.height !== image.naturalHeight) {
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
  }
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0);
  canvas.style.width = `${canvas.width * state.zoom}px`;
  canvas.style.height = `${canvas.height * state.zoom}px`;
  const region = state.scope === "all" ? null : state.selection || (currentSceneMatches() ? readRegion(false) : state.result.report.settings.region_mm);
  const size = dimensions();
  if (!region || !size) return;
  const pixelsPerMM = state.result.report.settings.dpi / 25.4;
  const x = region[0] * pixelsPerMM;
  const y = region[1] * pixelsPerMM;
  const width = (region[2] - region[0]) * pixelsPerMM;
  const height = (region[3] - region[1]) * pixelsPerMM;
  context.fillStyle = "rgba(36, 53, 80, 0.10)";
  context.fillRect(0, 0, canvas.width, y);
  context.fillRect(0, y + height, canvas.width, canvas.height - y - height);
  context.fillRect(0, y, x, height);
  context.fillRect(x + width, y, canvas.width - x - width, height);
  context.strokeStyle = "#315ff0";
  context.lineWidth = 1.5 / state.zoom;
  context.setLineDash([6 / state.zoom, 4 / state.zoom]);
  context.strokeRect(x, y, width, height);
  context.setLineDash([]);
}

function draw() {
  if (!state.result) return;
  let firstImage = state.images[state.mode];
  if (state.mode === "side-by-side") firstImage = state.images.reference;
  if (state.mode === "blink") firstImage = state.blinkPhase ? state.images.candidate : state.images.reference;
  drawCanvas(canvases[0], firstImage);
  if (state.mode === "side-by-side") drawCanvas(canvases[1], state.images.candidate);
  if (state.mode === "blink") byId("first-view-label").textContent = `${state.blinkPhase ? "待比较页" : "参考页"} · 闪烁对照`;
  byId("zoom-value").textContent = `${Math.round(state.zoom * 100)}%`;
  byId("zoom").value = String(Math.round(state.zoom * 100));
}

function scheduleDraw() {
  if (state.drawScheduled) return;
  state.drawScheduled = true;
  requestAnimationFrame(() => {
    state.drawScheduled = false;
    draw();
  });
}

function fitView() {
  if (!state.result || !state.images.reference) return;
  const padding = window.matchMedia("(max-width: 470px)").matches ? 28 : 44;
  state.zoom = Math.max(0.03, Math.min(2.5, (frames[0].clientWidth - padding) / state.images.reference.naturalWidth));
  state.fit = true;
  draw();
}

function setZoom(value, anchor = null) {
  if (!state.result) return;
  const oldZoom = state.zoom;
  const frame = anchor?.frame || frames[0];
  const canvas = anchor?.canvas || canvases[0];
  const rect = canvas.getBoundingClientRect();
  const frameRect = frame.getBoundingClientRect();
  const x = anchor?.x ?? frameRect.left + frame.clientWidth / 2;
  const y = anchor?.y ?? frameRect.top + frame.clientHeight / 2;
  const imageX = (x - rect.left) / oldZoom;
  const imageY = (y - rect.top) / oldZoom;
  state.zoom = Math.max(0.03, Math.min(4, value));
  state.fit = false;
  draw();
  const nextRect = canvas.getBoundingClientRect();
  frame.scrollLeft += nextRect.left + imageX * state.zoom - x;
  frame.scrollTop += nextRect.top + imageY * state.zoom - y;
  syncScroll(frame);
}

function syncScroll(source) {
  if (state.mode !== "side-by-side") return;
  const other = source === frames[0] ? frames[1] : frames[0];
  // 相同坐标自然终止回环，不锁住后续滚动事件，避免缩放后的真实操作被丢弃。
  if (other.scrollTop !== source.scrollTop) other.scrollTop = source.scrollTop;
  if (other.scrollLeft !== source.scrollLeft) other.scrollLeft = source.scrollLeft;
}

function canvasPoint(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  const size = dimensions();
  const mmPerPixel = 25.4 / state.result.report.settings.dpi;
  return [
    Math.max(0, Math.min(size.width, (event.clientX - rect.left) / rect.width * canvas.width * mmPerPixel)),
    Math.max(0, Math.min(size.height, (event.clientY - rect.top) / rect.height * canvas.height * mmPerPixel)),
  ];
}

canvases.forEach((canvas, index) => {
  const frame = frames[index];
  canvas.addEventListener("pointerdown", (event) => {
    if (!state.result || event.button !== 0) return;
    if (state.tool === "region" && !currentSceneMatches()) {
      showError(new Error("当前视图来自之前的文件或页码。请先比较当前页面，再框选测量区域。"));
      return;
    }
    event.preventDefault();
    canvas.setPointerCapture(event.pointerId);
    state.pointer = { canvas, frame, id: event.pointerId, start: canvasPoint(event, canvas), clientX: event.clientX, clientY: event.clientY, scrollLeft: frame.scrollLeft, scrollTop: frame.scrollTop, tool: state.tool };
    if (state.tool === "pan") byId("viewer").classList.add("panning");
    else stopBlink();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!state.result) return;
    const point = canvasPoint(event, canvas);
    byId("pointer-position").textContent = `X ${point[0].toFixed(2)} · Y ${point[1].toFixed(2)} mm`;
    const pointer = state.pointer;
    if (!pointer || pointer.canvas !== canvas) return;
    if (pointer.tool === "pan") {
      frame.scrollLeft = pointer.scrollLeft - (event.clientX - pointer.clientX);
      frame.scrollTop = pointer.scrollTop - (event.clientY - pointer.clientY);
      syncScroll(frame);
    } else {
      state.selection = [Math.min(pointer.start[0], point[0]), Math.min(pointer.start[1], point[1]), Math.max(pointer.start[0], point[0]), Math.max(pointer.start[1], point[1])];
      scheduleDraw();
    }
  });
  const finishPointer = (event) => {
    if (!state.pointer || state.pointer.canvas !== canvas) return;
    const pointer = state.pointer;
    state.pointer = null;
    byId("viewer").classList.remove("panning");
    if (canvas.hasPointerCapture(pointer.id)) canvas.releasePointerCapture(pointer.id);
    if (pointer.tool === "region") {
      const region = state.selection;
      state.selection = null;
      if (event.type !== "pointercancel" && region && region[2] - region[0] >= 0.2 && region[3] - region[1] >= 0.2) {
        setRegion(region);
        invalidate("测量区域已变更");
      }
      draw();
      startBlink();
    }
  };
  canvas.addEventListener("pointerup", finishPointer);
  canvas.addEventListener("pointercancel", finishPointer);
  canvas.addEventListener("pointerleave", () => {
    if (!state.pointer) byId("pointer-position").textContent = "单位：mm · Ctrl + 滚轮缩放";
  });
  frame.addEventListener("scroll", () => syncScroll(frame), { passive: true });
  frame.addEventListener("wheel", (event) => {
    if (!event.ctrlKey || !state.result) return;
    event.preventDefault();
    setZoom(state.zoom * Math.exp(-event.deltaY * 0.002), { canvas, frame, x: event.clientX, y: event.clientY });
  }, { passive: false });
});

sources.forEach((source) => {
  const input = byId(`${source}-file`);
  const drop = byId(`${source}-drop`);
  input.addEventListener("change", () => {
    upload(source, input.files[0]);
    input.value = "";
  });
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("dragover");
    event.dataTransfer.dropEffect = "copy";
  });
  drop.addEventListener("dragleave", (event) => {
    if (!drop.contains(event.relatedTarget)) drop.classList.remove("dragover");
  });
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("dragover");
    const files = event.dataTransfer.files;
    if (files.length !== 1) showError(new Error("每个位置请拖入一份 PDF 文件。"));
    else upload(source, files[0]);
  });
  byId(`${source}-page`).addEventListener("input", () => {
    if (state.scope !== "single") return;
    setRegion(null);
    updatePageMetadata(source);
    invalidate("比较页码已变更");
    scheduleDraw();
  });
});

byId("dpi").addEventListener("change", () => invalidate("渲染精度已变更"));
byId("ink-threshold").addEventListener("input", () => invalidate("墨迹阈值已变更"));
regionFields.forEach((field) => field.addEventListener("input", () => {
  if (state.scope !== "single") return;
  updateRegionDescription();
  invalidate("测量区域已变更");
  scheduleDraw();
}));
byId("clear-region").addEventListener("click", () => {
  setRegion(null);
  invalidate("已恢复整页测量");
  draw();
});
document.querySelectorAll('input[name="comparison-scope"]').forEach((input) => input.addEventListener("change", () => {
  if (!input.checked || input.value === state.scope) return;
  if (input.value === "all" && !matchingPageCounts()) {
    updateControls();
    return;
  }
  state.scope = input.value;
  state.selection = null;
  state.pointer = null;
  setRegion(null);
  if (state.scope === "all") setTool("pan");
  invalidate("比较范围已变更");
  draw();
}));
byId("cancel-batch").addEventListener("click", () => cancelBatch());
byId("batch-only-differences").addEventListener("change", renderBatch);
byId("retry-detail").addEventListener("click", () => {
  if (state.batchSession?.selectedPage) loadBatchPage(state.batchSession.selectedPage);
});
byId("compare-button").addEventListener("click", compare);
byId("demo-button").addEventListener("click", loadDemo);
byId("empty-demo-button").addEventListener("click", loadDemo);
byId("pan-tool").addEventListener("click", () => setTool("pan"));
byId("region-tool").addEventListener("click", () => setTool("region"));
byId("fit-button").addEventListener("click", fitView);
byId("actual-size-button").addEventListener("click", () => setZoom(1));
byId("zoom").addEventListener("input", (event) => setZoom(Number(event.target.value) / 100));
document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
document.querySelectorAll(".view-tabs [role=tab]").forEach((button, index, buttons) => {
  button.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const next = (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
    setMode(buttons[next].dataset.mode);
  });
});
document.addEventListener("visibilitychange", () => { if (document.hidden) stopBlink(); else startBlink(); });
window.addEventListener("resize", () => { if (state.fit) fitView(); });
window.addEventListener("beforeunload", stopBlink);
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
updateControls();
