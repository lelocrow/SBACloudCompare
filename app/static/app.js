const tabButtons = document.querySelectorAll(".provider-btn");
const forms = {
  aws: document.getElementById("form-aws"),
  azure: document.getElementById("form-azure"),
};
const statusText = document.getElementById("status-text");
const resultBox = document.getElementById("result-box");
const resultFile = document.getElementById("result-file");
const sheetList = document.getElementById("sheet-list");
const downloadBtn = document.getElementById("download-btn");
const mappingSourceLink = document.getElementById("mapping-source-link");
const mappingDatasetLink = document.getElementById("mapping-dataset-link");

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
  button.addEventListener("click", () => {
    setProvider(button.dataset.provider);
  });
});

function setRunning(form, running) {
  form.querySelectorAll("input, button").forEach((el) => {
    el.disabled = running;
  });
}

function renderSheetCounts(sheetCounts) {
  sheetList.innerHTML = "";
  Object.entries(sheetCounts || {})
    .sort((a, b) => a[0].localeCompare(b[0]))
    .forEach(([name, count]) => {
      const item = document.createElement("li");
      item.textContent = `${name}: ${count} registros`;
      sheetList.appendChild(item);
    });
}

async function runScan(provider, form) {
  statusText.textContent = `Executando varredura ${provider.toUpperCase()}... isso pode levar alguns minutos.`;
  resultBox.classList.add("hidden");
  setRunning(form, true);

  try {
    const response = await fetch(`/api/scan/${provider}`, {
      method: "POST",
      body: new FormData(form),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Falha na execução.");
    }

    statusText.textContent = `Leitura ${provider.toUpperCase()} concluída com sucesso.`;
    if (data.mapping_last_error && data.mapping_data_source && data.mapping_data_source !== data.mapping_data_url) {
      statusText.textContent += " Fonte remota indisponível; snapshot local aplicado.";
    }
    resultFile.textContent = data.filename;
    if (data.mapping_source) {
      mappingSourceLink.href = data.mapping_source;
      mappingSourceLink.textContent = data.mapping_source;
    }
    if (data.mapping_data_url) {
      mappingDatasetLink.href = data.mapping_data_url;
      mappingDatasetLink.textContent = data.mapping_data_url;
    }
    renderSheetCounts(data.sheet_counts);
    downloadBtn.onclick = () => {
      window.location.href = `/api/download/${data.scan_id}`;
    };
    resultBox.classList.remove("hidden");
  } catch (error) {
    statusText.textContent = `Erro: ${error.message}`;
  } finally {
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
