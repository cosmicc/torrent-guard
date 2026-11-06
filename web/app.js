"use strict";

// All remote strings enter the page through textContent. No log/API HTML is parsed.
const $ = (id) => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const timeFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "short",
});
const fullParts = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
  timeZoneName: "longOffset",
});
function fullTimestamp(date) {
  const parts = Object.fromEntries(
    fullParts.formatToParts(date).map((part) => [part.type, part.value]),
  );
  const zone = timeFormat
    .formatToParts(date)
    .find((part) => part.type === "timeZoneName").value;
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${zone}${parts.timeZoneName.replace("GMT", "").replace(":", "")}`;
}
let current = null;
let hours = "12";
let busy = false;
let lastFetch = 0;
let connectionError = false;
let selectedRecord = null;
let activeRequestId = null;
let activityPage = 0;
const ACTIVITY_PAGE_SIZE = 10;
const ACTIVITY_LIMIT = 100;
let actionRefreshTimer = null;

function renderRecovery(actions) {
  const activeRequest = actions?.requests?.find(
    (request) => request.id === activeRequestId,
  );
  if (activeRequest) {
    clearTimeout(actionRefreshTimer);
    if (activeRequest.state === "queued")
      actionRefreshTimer = setTimeout(refresh, 2000);
    $("action-notice").textContent =
      `${activeRequest.name || "Record"} · ${activeRequest.state}: ${activeRequest.message}`;
    $("action-notice").className =
      activeRequest.state === "refused" ? "note warn" : "note";
  }
  $("record-actions").replaceChildren();
  $("action-results").replaceChildren();
  $("archived-records").replaceChildren();
  const records = actions?.available ? actions.records : [];
  $("recovery-summary").textContent = actions?.available
    ? `${records.length} active records available for review (up to 100)`
    : "Recovery information unavailable";
  if (!actions?.available)
    empty(
      $("record-actions"),
      "Recovery information is unavailable until a successful poll initializes the journal.",
    );
  for (const record of records) {
    const row = node("article", undefined, "work");
    const head = node("div", undefined, "work-head");
    const button = node("button", "Forget record");
    button.type = "button";
    const queued = actions.requests.some(
      (request) =>
        request.record_id === record.id && request.state === "queued",
    );
    button.disabled = queued;
    if (queued) button.textContent = "Queued";
    button.addEventListener("click", () => {
      selectedRecord = record;
      $("forget-name").textContent = record.name;
      $("forget-dialog").showModal();
    });
    head.append(node("strong", record.name), button);
    row.append(head, node("p", record.reason, "note"));
    if (record.retirement_check) {
      const check = node("p", undefined, "note");
      check.append(document.createTextNode(`Last retirement check: ${record.retirement_check.detail} · `), stamp(record.retirement_check.at));
      row.append(check);
    }
    $("record-actions").append(row);
  }
  if (actions?.available && !records.length)
    empty($("record-actions"), "No active bookkeeping records.");
  for (const request of (actions?.requests || []).slice(0, 8)) {
    $("action-results").append(
      node(
        "p",
        `${request.name || "Record"} · ${request.state}: ${request.message}`,
        request.state === "refused" ? "note warn" : "note",
      ),
    );
  }
  for (const record of actions?.archived || []) {
    const row = node("p", undefined, "note");
    row.append(
      document.createTextNode(
        `${record.name} · ${record.disposition === "verified_absent" ? "Saved absence evidence verified" : "Operator archived; absence not certified"} · `,
      ),
      stamp(record.at),
    );
    $("archived-records").append(row);
  }
  if (!$("archived-records").children.length)
    empty($("archived-records"), "No records archived yet.");
}

async function forgetRecord() {
  if (!selectedRecord) return;
  activeRequestId = null;
  const record = selectedRecord;
  $("forget-confirm").disabled = true;
  try {
    const response = await fetch("api/forget", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": current?.csrf_token || "",
      },
      body: JSON.stringify({ record_id: record.id, revision: record.revision }),
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) {
      const rejection = new Error((await response.text()).trim());
      rejection.status = response.status;
      throw rejection;
    }
    const result = await response.json();
    activeRequestId = result.request_id;
    $("action-notice").textContent =
      "Archive request queued. Polling will check that the torrent is absent; no files or torrents will be deleted.";
    $("forget-dialog").close();
    $("action-notice").scrollIntoView({ block: "nearest" });
    selectedRecord = null;
    clearTimeout(actionRefreshTimer);
    actionRefreshTimer = setTimeout(refresh, 2000);
    await refresh();
  } catch (error) {
    $("action-notice").textContent =
      error.status >= 400 && error.status < 500
        ? `Archive request rejected: ${error.message}`
        : `Archive request not confirmed: ${error.message}. Refresh before trying again; a timed-out request may already be queued.`;
    $("forget-dialog").close();
    $("action-notice").className = "note warn";
    $("action-notice").scrollIntoView({ block: "nearest" });
  } finally {
    $("forget-confirm").disabled = false;
  }
}

function stamp(at) {
  const time = node("time", timeFormat.format(new Date(at * 1000)));
  time.dateTime = new Date(at * 1000).toISOString();
  time.title = fullTimestamp(new Date(at * 1000));
  return time;
}
function duration(seconds) {
  if (seconds == null || seconds < 0) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}
function size(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  return bytes >= 2 ** 40
    ? `${(bytes / 2 ** 40).toFixed(1)} TiB`
    : `${(bytes / 2 ** 30).toFixed(1)} GiB`;
}
function stateText(text, state) {
  const span = node("span", undefined, state);
  span.append(node("span", undefined, "dot"), document.createTextNode(text));
  return span;
}
function empty(parent, text) {
  parent.append(node("p", text, "empty"));
}
function decisionColor(decision) {
  if (decision === "Rejection candidate") return "bad";
  if (decision === "Dry run") return "dry";
  return ["Cleaned", "Validated"].includes(decision) ? "good" : "warn";
}
function renderMetrics(history) {
  const counts = history?.available ? history.windows[hours] : null;
  $("metrics").replaceChildren(
    ...[
      ["Cleaned", "cleaned", "good"],
      ["Rejection candidates", "rejected", "warn"],
      ["Warnings", "warnings", "warn"],
      ["Errors", "errors", "bad"],
    ].map(([label, key, color]) => {
      const metric = node("div", undefined, "metric");
      metric.append(
        node("span", label),
        node(
          "strong",
          counts ? String(counts[key]) : "—",
          counts?.[key] ? color : "",
        ),
      );
      return metric;
    }),
  );
  $("coverage").textContent = !counts
    ? `History unavailable${history?.detail ? ": " + history.detail : "; waiting for first collection."}`
    : `${counts.partial ? "Partial window · " : ""}Recorded since ${fullTimestamp(new Date(history.since * 1000))}. Event counts overlap; candidates include dry run. Downtime is not observed.${history.gap ? " Recording failures may leave gaps." : ""}`;
}
function renderRemovalRules(reference) {
  // Policy comes from the daemon's runtime constants. Browser reads only display
  // it; neither expanding this panel nor refreshing can initiate deletion.
  const body = $("removal-rule-rows");
  body.replaceChildren();
  for (const rule of reference?.rows || []) {
    const row = node("tr");
    row.append(node("td", rule.condition), node("td", rule.action));
    body.append(row);
  }
  $("removal-rule-notes").replaceChildren(
    ...(reference?.notes || []).map((note) => node("li", note)),
  );
  if (!reference)
    $("removal-rule-notes").append(node("li", "Removal rules will appear after the next successful status response."));
}

function renderRecentDownloads(history) {
  // Navigation only slices the bounded cached history. It never polls services.
  const items = (history?.activity || []).slice(0, ACTIVITY_LIMIT);
  const pageCount = Math.max(1, Math.ceil(items.length / ACTIVITY_PAGE_SIZE));
  activityPage = Math.max(0, Math.min(activityPage, pageCount - 1));
  $("activity").replaceChildren();
  for (const item of items.slice(activityPage * ACTIVITY_PAGE_SIZE, (activityPage + 1) * ACTIVITY_PAGE_SIZE)) {
    const row = node("tr");
    const name = node("td");
    const details = node("details");
    details.append(node("summary", item.name), node("p", item.detail));
    name.append(details);
    const decision = node("td");
    decision.append(stateText(item.decision, decisionColor(item.decision)));
    const at = node("td");
    at.append(stamp(item.at));
    row.append(name, decision, at);
    $("activity").append(row);
  }
  if (!$("activity").children.length) {
    const row = node("tr");
    const cell = node("td", history?.available ? "No download decisions recorded yet." : "Recent download history is unavailable.", "empty");
    cell.colSpan = 3;
    row.append(cell);
    $("activity").append(row);
  }
  $("activity-first").disabled = $("activity-backward").disabled = activityPage === 0;
  $("activity-forward").disabled = $("activity-last").disabled = activityPage === pageCount - 1;
  const first = items.length ? activityPage * ACTIVITY_PAGE_SIZE + 1 : 0;
  const last = Math.min((activityPage + 1) * ACTIVITY_PAGE_SIZE, items.length);
  $("activity-page").textContent = history?.available
    ? `Page ${activityPage + 1} of ${pageCount} · ${first}–${last} of ${items.length} entries`
    : "Recent history unavailable";
}

function render(data) {
  current = data;
  renderRecovery(data.record_actions);
  renderRemovalRules(data.removal_rules);
  $("version").textContent = data.version ? "v" + data.version : "—";
  const cacheAge = data.cache_age_seconds;
  $("updated").textContent =
    cacheAge == null
      ? "First collection running…"
      : `Checked ${duration(cacheAge)} ago`;
  const notices = [];
  if (!data.ready)
    notices.push(
      "Collecting the first status snapshot. Torrent polling continues independently.",
    );
  else if (data.stale)
    notices.push(
      "Diagnostics are stale. Connection and drive details below are the last observed results.",
    );
  if (data.collection_error) notices.push(data.collection_error);
  $("notice").hidden = notices.length === 0;
  $("notice").textContent = notices.join(" ");
  $("poll-health").replaceChildren(
    stateText(
      data.poll?.healthy ? "Polling active" : "Polling not current",
      data.poll?.healthy ? "good" : "warn",
    ),
  );
  $("mode").textContent = data.settings
    ? data.settings.dry_run
      ? "Dry-run mode"
      : "Live mode"
    : "Mode —";
  $("mode").className = data.settings?.dry_run ? "dry" : "";
  $("last-poll").textContent =
    `Last poll: ${data.poll?.age_seconds == null ? "unavailable" : duration(data.poll.age_seconds) + " ago"}`;
  $("uptime").textContent =
    "Uptime: " +
    (data.history?.started
      ? duration(data.served_at - data.history.started)
      : "—");
  const services =
    data.services ||
    ["qBittorrent", "Sonarr", "Radarr"].map((name) => ({
      name,
      ok: null,
      detail: "Waiting for collection",
    }));
  const drives = data.drives || [];
  const storageOK = drives.length > 0 && drives.every((drive) => drive.ok);
  const items = [
    ...services,
    {
      name: "Storage",
      ok: drives.length ? storageOK : null,
      detail: drives.length
        ? `${drives.filter((d) => d.ok).length} of ${drives.length} paths checked`
        : "Waiting for collection",
    },
  ];
  $("services").replaceChildren(
    ...items.map((service) => {
      const box = node("article", undefined, "service");
      const status =
        service.ok === true
          ? service.name === "Storage"
            ? "Accessible"
            : "Connected"
          : service.ok === false
            ? "Needs attention"
            : "Not checked";
      const heading = node("p", undefined, "service-state");
      heading.append(
        stateText(
          status,
          service.ok === true ? "good" : service.ok === false ? "bad" : "warn",
        ),
      );
      box.append(
        node("h3", service.name),
        heading,
        node("p", service.detail, "note"),
      );
      return box;
    }),
  );
  renderMetrics(data.history);
  renderRecentDownloads(data.history);
  const snapshot = data.history?.snapshot;
  const persisted = data.journal?.available ? data.journal.counts : {};
  const observed = snapshot?.counts || {};
  const counters = [
    ["Pending deletions", persisted.pending],
    ["Awaiting import", observed.waiting_for_import],
    ["Import grace", observed.import_grace],
    ["Filesystem holds", persisted.filesystem_retry],
    ["Cleanup retries", persisted.cleanup_retry],
    ["Metadata waits (persisted)", persisted.metadata_wait],
    ["Stalled timers (persisted)", persisted.stalled_wait],
  ];
  $("counters").replaceChildren(
    ...counters.map(([label, value]) => {
      const row = node("div");
      row.append(
        node("dt", label),
        node(
          "dd",
          value == null ? "—" : String(value),
          Number(value) > 0 ? "warn" : "good",
        ),
      );
      return row;
    }),
  );
  $("snapshot-age").textContent = snapshot
    ? `Import counts last observed ${fullTimestamp(new Date(snapshot.at * 1000))}. Counters overlap; timers are memory-only in dry run.`
    : "No completed-poll counts recorded yet. Persisted counts exclude dry-run timers.";
  const qbit = services.find((service) => service.name === "qBittorrent");
  $("torrent-states").textContent = qbit?.states
    ? "Managed states: " +
      (Object.entries(qbit.states)
        .map(([state, count]) => `${state} ${count}`)
        .join(" · ") || "none")
    : "";
  $("issues").replaceChildren();
  const issueNow = Date.now() / 1000;
  const recentIssues = (data.history?.issues || []).filter(
    (issue) => Number.isFinite(issue.at) && issue.at >= issueNow - 24 * 3600 && issue.at <= issueNow,
  );
  for (const issue of recentIssues.slice(0, 8)) {
    const event = node("article", undefined, "event");
    const head = node("div", undefined, "event-head");
    const color = issue.level === "WARNING" ? "warn" : "bad";
    head.append(stateText(issue.level, color), stamp(issue.at));
    event.append(head, node("p", issue.message));
    $("issues").append(event);
  }
  if (!$("issues").children.length)
    empty(
      $("issues"),
      data.history?.available
        ? "No warnings or errors recorded in the last 24 hours."
        : "Warning and error history is not available yet.",
    );
  $("drives").replaceChildren();
  for (const drive of drives) {
    const section = node("div", undefined, "drive");
    const head = node("div", undefined, "drive-head");
    head.append(
      node("span", drive.name),
      node(
        "strong",
        drive.ok
          ? drive.write_tested
            ? "Write verified"
            : "Read-only check"
          : "Check failed",
        drive.ok ? (drive.write_tested ? "good" : "info") : "bad",
      ),
    );
    section.append(head, node("p", `${drive.path} · ${drive.detail}`, "note"));
    if (drive.ok && drive.total_bytes > 0) {
      const label = node("div", undefined, "capacity-label");
      label.append(
        node("span", "Available space"),
        node(
          "span",
          `${size(drive.free_bytes)} free / ${size(drive.total_bytes)}`,
        ),
      );
      const meter = node("meter");
      meter.min = 0;
      meter.max = drive.total_bytes;
      meter.value = drive.free_bytes;
      meter.setAttribute("aria-label", `${drive.name} available space`);
      section.append(label, meter);
    }
    $("drives").append(section);
  }
  if (!drives.length) empty($("drives"), "Drive checks are being collected.");
  $("work-details").replaceChildren();
  if (!data.journal?.available)
    empty(
      $("work-details"),
      "Journal unavailable" +
        (data.journal?.detail ? ": " + data.journal.detail : ""),
    );
  else {
    for (const item of data.journal.pending) {
      const work = node("article", undefined, "work");
      const head = node("div", undefined, "work-head");
      head.append(
        node("strong", item.name),
        node("span", `${item.app} · ${item.kind} · ${item.phase}`, "warn"),
      );
      work.append(head, node("p", item.reason, "note"));
      $("work-details").append(work);
    }
    for (const item of data.journal.holds) {
      const work = node("article", undefined, "work");
      const head = node("div", undefined, "work-head");
      head.append(
        node("strong", item.name),
        node("span", `${item.type} · attempt ${item.attempt}`, "warn"),
      );
      work.append(
        head,
        node(
          "p",
          `${item.reason} · Next eligible check: ${fullTimestamp(new Date(item.next_at * 1000))}`,
          "note",
        ),
      );
      $("work-details").append(work);
    }
    if (!$("work-details").children.length)
      empty($("work-details"), "No pending deletions or retry holds.");
  }
  const settings = data.settings;
  $("settings").textContent = settings
    ? `Poll interval ${settings.poll_seconds}s · Import grace ${settings.import_grace_minutes}m · Video validation ${settings.verify_video_content ? "enabled" : "disabled"} · Completed cleanup ${settings.cleanup_leftovers ? "enabled" : "disabled"} · Orphan cleanup ${settings.orphan_cleanup ? "enabled (15m absence)" : "disabled"} · UID:GID ${settings.uid}:${settings.gid}`
    : "";
}

async function refresh() {
  if (busy) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("api/status", {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error("Status request failed");
    const data = await response.json();
    if (typeof data.ready !== "boolean")
      throw new Error("Invalid status response");
    lastFetch = Date.now();
    connectionError = false;
    render(data);
  } catch {
    connectionError = true;
    $("notice").hidden = false;
    $("notice").textContent =
      "Status page connection lost. Displayed values are older observations; retrying automatically.";
    $("poll-health").replaceChildren(
      stateText("Polling status unavailable", "warn"),
    );
    $("updated").textContent = "Connection unavailable";
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
document.querySelectorAll("[data-hours]").forEach((button) =>
  button.addEventListener("click", () => {
    hours = button.dataset.hours;
    document
      .querySelectorAll("[data-hours]")
      .forEach((item) =>
        item.setAttribute("aria-pressed", String(item === button)),
      );
    renderMetrics(current?.history);
  }),
);
for (const [id, destination] of [
  ["activity-first", () => 0],
  ["activity-backward", () => activityPage - 1],
  ["activity-forward", () => activityPage + 1],
  ["activity-last", () => Math.max(0, Math.ceil(Math.min(current?.history?.activity?.length || 0, ACTIVITY_LIMIT) / ACTIVITY_PAGE_SIZE) - 1)],
]) {
  $(id).addEventListener("click", () => {
    activityPage = destination();
    renderRecentDownloads(current?.history);
  });
}
$("refresh").addEventListener("click", refresh);
$("forget-cancel").addEventListener("click", () => $("forget-dialog").close());
$("forget-confirm").addEventListener("click", forgetRecord);
render({ ready: false, poll: { healthy: false } });
refresh();
setInterval(refresh, 10000);
setInterval(() => {
  if (!connectionError && lastFetch && Date.now() - lastFetch > 15000) {
    $("notice").hidden = false;
    $("notice").textContent =
      "Page updates are delayed. Displayed values may be stale.";
    $("poll-health").replaceChildren(
      stateText("Polling status unavailable", "warn"),
    );
  }
}, 5000);
