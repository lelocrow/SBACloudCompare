const tabButtons = document.querySelectorAll(".provider-btn");
const forms = {
  aws: document.getElementById("form-aws"),
  azure: document.getElementById("form-azure"),
};

const statusText = document.getElementById("status-text");
const resultBox = document.getElementById("result-box");
const resultFile = document.getElementById("result-file");
const resultProvider = document.getElementById("result-provider");
const resultSheets = document.getElementById("result-sheets");
const resultRows = document.getElementById("result-rows");
const resultWarnings = document.getElementById("result-warnings");
const mappingSourceLink = document.getElementById("mapping-source-link");
const mappingDatasetSource = document.getElementById("mapping-dataset-source");

function toErrorMessage(detail) {
  const fallback = "Falha na execução.";

  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }

  if (detail instanceof Error) {
    return toErrorMessage(detail.message);
  }

  if (Array.isArray(detail)) {
    const messages = detail.map((item) => toErrorMessage(item)).filter(Boolean);
    return messages.length ? messages.join(" | ") : fallback;
  }

  if (detail && typeof detail === "object") {
    const loc = Array.isArray(detail.loc) ? detail.loc.join(".") : "";
    const msg = detail.msg || detail.message || "";
    if (loc && msg) {
      return `${loc}: ${msg}`;
    }
    if (msg) {
      return msg;
    }
    if ("detail" in detail) {
      return toErrorMessage(detail.detail);
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }

  return fallback;
}

function setProvider(provider) {
  tabButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.provider === provider);
  });
  Object.entries(forms).forEach(([key, form]) => {
    form.classList.toggle("active", key === provider);
  });
  statusText.textContent = `Pronto para iniciar a coleta ${provider.toUpperCase()}.`;
}

tabButtons.forEach((button) => {
  button.addEventListener("click", () => setProvider(button.dataset.provider));
});

function setRunning(form, running) {
  form.querySelectorAll("input, button").forEach((el) => {
    el.disabled = running;
  });
}

function parseFilenameFromContentDisposition(headerValue) {
  if (!headerValue) {
    return "relatorio.xlsx";
  }
  const quoted = /filename="([^"]+)"/i.exec(headerValue);
  if (quoted && quoted[1]) {
    return quoted[1];
  }
  const plain = /filename=([^;]+)/i.exec(headerValue);
  if (plain && plain[1]) {
    return plain[1].trim();
  }
  return "relatorio.xlsx";
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function parseErrorResponse(response) {
  try {
    const data = await response.clone().json();
    return toErrorMessage(data?.detail ?? data);
  } catch {
    try {
      const text = (await response.text()).trim();
      return text || "Falha na execução.";
    } catch {
      return "Falha na execução.";
    }
  }
}

async function runScan(provider, form) {
  resultBox.classList.add("hidden");
  const formData = new FormData(form);
  setRunning(form, true);

  const startedAt = Date.now();
  const progressTimer = setInterval(() => {
    const elapsedSec = Math.floor((Date.now() - startedAt) / 1000);
    statusText.textContent = `Executando varredura ${provider.toUpperCase()}... ${elapsedSec}s decorridos.`;
  }, 1000);

  try {
    const response = await fetch(`/api/scan/${provider}`, {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const detail = await parseErrorResponse(response);
      throw new Error(toErrorMessage(detail));
    }

    const blob = await response.blob();
    const filename = parseFilenameFromContentDisposition(response.headers.get("content-disposition"));
    triggerDownload(blob, filename);

    const warningsCount = response.headers.get("x-sba-warnings-count") || "0";
    const sheetCount = response.headers.get("x-sba-sheet-count") || "-";
    const totalRows = response.headers.get("x-sba-total-rows") || "-";
    const mappingSource = response.headers.get("x-sba-mapping-source");
    const mappingDataSource = response.headers.get("x-sba-mapping-data-source") || "remote";
    const usedProvider = (response.headers.get("x-sba-provider") || provider).toUpperCase();

    resultFile.textContent = filename;
    resultProvider.textContent = usedProvider;
    resultSheets.textContent = sheetCount;
    resultRows.textContent = totalRows;
    resultWarnings.textContent = warningsCount;
    mappingDatasetSource.textContent = mappingDataSource === "snapshot" ? "snapshot local" : "remoto";
    if (mappingSource) {
      mappingSourceLink.href = mappingSource;
      mappingSourceLink.textContent = mappingSource;
    }

    statusText.textContent = `Leitura ${usedProvider} concluída e download iniciado.`;
    if (warningsCount !== "0") {
      statusText.textContent += ` Foram capturados ${warningsCount} warning(s) no relatório.`;
    }
    if (mappingDataSource === "snapshot") {
      statusText.textContent += " A fonte remota do CompareCloud estava indisponível e o snapshot local foi aplicado.";
    }
    resultBox.classList.remove("hidden");
  } catch (error) {
    statusText.textContent = `Erro: ${toErrorMessage(error)}`;
  } finally {
    clearInterval(progressTimer);
    setRunning(form, false);
  }
}

forms.aws.addEventListener("submit", (event) => {
  event.preventDefault();
  runScan("aws", forms.aws);
});

forms.azure.addEventListener("submit", (event) => {
  event.preventDefault();
  runScan("azure", forms.azure);
});

setProvider("aws");
