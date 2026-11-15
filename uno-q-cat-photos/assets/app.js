// Labelling UI for the cat photo collector. Live view is the iframe in index.html.

const errorContainer = document.getElementById("error-container");
const liveStatus = document.getElementById("live-status");
const captureMeta = document.getElementById("capture-meta");
const captureImage = document.getElementById("capture-image");
const debugImage = document.getElementById("debug-image");
const statusLine = document.getElementById("status");
const countsBody = document.querySelector("#counts tbody");
const labelButtons = document.querySelectorAll("button[data-class]");

const LIGHTING = ["day", "lamp", "low", "other"];
const CLASSES = ["grey", "black", "negative", "uncertain"];
const CLASS_LABELS = {
  grey: "Grey",
  black: "Black",
  negative: "Negative",
  uncertain: "Unlabelled",
};

let currentId = null;

const ui = new WebUI();
ui.on_connect(onConnected);
ui.on_disconnect(onDisconnected);
ui.on_message("capture", showCapture);
ui.on_message("summary", showSummary);
ui.on_message("lighting", showLighting);
ui.on_message("detection", showDetections);
ui.on_message("status", showStatus);

document.querySelectorAll('input[name="lighting"]').forEach((input) => {
  input.addEventListener("change", () => {
    if (input.checked) {
      ui.send_message("set_lighting", { lighting: input.value });
    }
  });
});

labelButtons.forEach((button) => {
  button.addEventListener("click", () => {
    if (!currentId) {
      return;
    }
    ui.send_message("label_capture", {
      id: currentId,
      class: button.dataset.class,
      lighting: selectedLighting(),
    });
  });
});

document.getElementById("capture-negative").addEventListener("click", () => {
  ui.send_message("capture_negative", { lighting: selectedLighting() });
});

showSummary({ counts: emptyCounts(), labelled: 0, unlabelled: 0 });

function onConnected() {
  errorContainer.style.display = "none";
  errorContainer.textContent = "";
  ui.send_message("hello", {});
}

function onDisconnected() {
  errorContainer.textContent = "Connection to the board lost. Please check the connection.";
  errorContainer.style.display = "block";
}

function selectedLighting() {
  const picked = document.querySelector('input[name="lighting"]:checked');
  return picked ? picked.value : "day";
}

function showLighting(tag) {
  const input = document.querySelector(`input[name="lighting"][value="${tag}"]`);
  if (input) {
    input.checked = true;
  }
}

function showStatus(text) {
  statusLine.textContent = typeof text === "string" ? text : "";
}

function showDetections(entries) {
  if (!Array.isArray(entries) || entries.length === 0) {
    liveStatus.textContent = "No detection in the last result.";
    return;
  }
  const parts = entries.slice(0, 4).map((entry) => {
    const confidence = Number(entry.confidence);
    const percent = Number.isFinite(confidence) ? ` ${(confidence * 100).toFixed(0)}%` : "";
    return `${entry.label}${percent}`;
  });
  liveStatus.textContent = parts.join(", ");
}

function showCapture(record) {
  if (!record || !record.id) {
    currentId = null;
    setLabelButtons(false);
    captureMeta.textContent = "No capture yet.";
    captureImage.hidden = true;
    debugImage.hidden = true;
    return;
  }
  currentId = record.id;
  setLabelButtons(true);
  const confidence = Number(record.confidence);
  const percent = Number.isFinite(confidence) ? `${(confidence * 100).toFixed(0)}%` : "manual";
  const stored = record.class ? record.class : "unlabelled";
  const box = Array.isArray(record.bbox) ? record.bbox.map((value) => Math.round(Number(value))).join(", ") : "none";
  captureMeta.textContent = `${record.timestamp} · ${record.label} ${percent} · ${stored} · ${record.lighting} · box ${box}`;
  setImage(captureImage, record.has_crop ? "crop" : "full", record.has_crop || record.has_full);
  setImage(debugImage, "debug", record.has_debug);
}

function setImage(image, kind, show) {
  if (!show || !currentId) {
    image.hidden = true;
    image.removeAttribute("src");
    return;
  }
  image.hidden = false;
  image.src = `/photo/${encodeURIComponent(currentId)}/${kind}?t=${Date.now()}`;
}

function setLabelButtons(enabled) {
  labelButtons.forEach((button) => {
    button.disabled = !enabled;
  });
}

function showSummary(summary) {
  const counts = summary && summary.counts ? summary.counts : emptyCounts();
  countsBody.replaceChildren();
  CLASSES.forEach((name) => {
    const row = document.createElement("tr");
    const heading = document.createElement("th");
    heading.textContent = CLASS_LABELS[name];
    row.appendChild(heading);
    LIGHTING.forEach((light) => {
      const cell = document.createElement("td");
      const value = counts[name] && counts[name][light];
      cell.textContent = String(value || 0);
      row.appendChild(cell);
    });
    countsBody.appendChild(row);
  });
}

function emptyCounts() {
  const counts = {};
  CLASSES.forEach((name) => {
    counts[name] = { day: 0, lamp: 0, low: 0, other: 0 };
  });
  return counts;
}
